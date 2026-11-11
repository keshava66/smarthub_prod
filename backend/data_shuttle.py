"""Data Studio, Data Shuttle pane.

Copies tables between Postgres and Snowflake, either way round. Built for
tables of 1 to 50M+ rows, so nothing goes row by row through Python:

  Postgres -> Snowflake
    The source is read inside one exported snapshot (pg_export_snapshot), so
    every parallel reader sees the same moment in time. Tables are split into
    ctid page ranges, each range is COPY ... TO STDOUT as gzip CSV, PUT to the
    user stage (@~) and COPY INTO a staging table.

  Snowflake -> Postgres
    One COPY INTO @~ unload (Snowflake splits it into files itself, and one
    statement is one consistent read), then each file is GET and COPY FROM
    STDIN into a staging table, several at a time.

Either way the target table is only touched once the staging table holds
exactly as many rows as the source had. Then it is a rename (new table), a
replace of the rows (overwrite) or one INSERT ... SELECT (append). A failed or
cancelled run leaves the target as it was.

Snowflake logins come from snowflake_query_runner (read profile reads, write
profile writes). Postgres logins come from postgres_studio (conn_id).
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import math
import os
import pathlib
import queue
import re
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import postgres_studio as pgs
import snowflake_query_runner as sfqr
from auth import require_permission

logger = logging.getLogger(__name__)

shuttle_router = APIRouter(prefix="/api/shuttle", tags=["Data Studio - Data Shuttle"])
PERM = "runDataShuttle"

_config: Dict[str, Any] = {}

DEFAULT_WORKERS = 4
MAX_WORKERS = 8
CHUNK_BYTES = 128 * 1024 * 1024        # Postgres heap per ctid chunk (one file each)
SMALL_TABLE_BYTES = 32 * 1024 * 1024   # below this, one reader is quicker than splitting
MAX_CHUNKS = 2000
UNLOAD_FILE_BYTES = 256 * 1024 * 1024  # Snowflake unload file size
MAX_OBJECTS = 200
JOB_TTL_SECONDS = 6 * 60 * 60
STAGE_ROOT = "data_shuttle"            # under the user stage, @~/data_shuttle/<job>/<n>/
RETRIES = 3

# CSV both sides agree on. NULL is \N unquoted, every real value is quoted, no
# backslash escaping (Postgres CSV has none), quotes inside values are doubled.
PG_CSV_OUT = "FORMAT csv, NULL '\\N', FORCE_QUOTE *, ENCODING 'UTF8'"
PG_CSV_IN = "FORMAT csv, NULL '\\N', ENCODING 'UTF8'"
SF_CSV = ("TYPE = CSV COMPRESSION = GZIP FIELD_DELIMITER = ',' FIELD_OPTIONALLY_ENCLOSED_BY = '\"' "
          "NULL_IF = ('\\\\N') EMPTY_FIELD_AS_NULL = FALSE ESCAPE = NONE ESCAPE_UNENCLOSED_FIELD = NONE")

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_job_pool: Optional[ThreadPoolExecutor] = None


class _Budget:
    """Hard caps on open connections, Postgres and Snowflake counted apart.

    A table takes everything it needs from both caps in one step, or waits.
    Taking them one at a time can deadlock: job A's readers hold the Postgres
    slots waiting on loaders, job B's loaders hold the Snowflake slots waiting
    on readers, and neither moves."""

    def __init__(self, pg: int, sf: int):
        self.cap = {"pg": max(2, pg), "sf": max(2, sf)}
        self.used = {"pg": 0, "sf": 0}
        self.cond = threading.Condition()

    def _fits(self, pg, sf):
        return self.used["pg"] + pg <= self.cap["pg"] and self.used["sf"] + sf <= self.cap["sf"]

    def take(self, pg: int, sf: int, job: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.time() + timeout
        logged = False
        with self.cond:
            while not self._fits(pg, sf):
                if job is not None:
                    _check(job)
                    if not logged:
                        _log(job, "info", f"Waiting for free connections (Postgres {self.used['pg']}/{self.cap['pg']}, "
                                          f"Snowflake {self.used['sf']}/{self.cap['sf']} in use)")
                        logged = True
                if deadline is not None and time.time() >= deadline:
                    return False
                self.cond.wait(1.0)
            self.used["pg"] += pg
            self.used["sf"] += sf
            return True

    def give(self, pg: int, sf: int):
        with self.cond:
            self.used["pg"] -= pg
            self.used["sf"] -= sf
            self.cond.notify_all()


_budget = _Budget(16, 16)


def set_data_shuttle_dependencies(config: Optional[Dict[str, Any]]):
    """Optional 'data_shuttle' section: work_dir, max_concurrent_jobs, min_free_disk_gb,
    max_pg_connections, max_sf_connections."""
    global _config, _job_pool, _budget
    _config = config or {}
    _job_pool = ThreadPoolExecutor(max_workers=int(_config.get("max_concurrent_jobs", 2)),
                                   thread_name_prefix="shuttle")
    _budget = _Budget(int(_config.get("max_pg_connections", 16)), int(_config.get("max_sf_connections", 16)))
    try:
        _history.start()
    except Exception as e:
        logger.warning("Data Shuttle history saver did not start: %s", e)


def _file_uri(path: str) -> str:
    """file:// URI for PUT / GET. Snowflake reads backslashes in the quoted path
    as escapes, so a Windows path like C:\\Users\\x must go as C:/Users/x."""
    return "file://" + pathlib.Path(path).as_posix().replace("'", "\\'")


def _work_root() -> str:
    root = _config.get("work_dir") or os.path.join(tempfile.gettempdir(), "smarthub_shuttle")
    os.makedirs(root, exist_ok=True)
    return root


class _Cancelled(Exception):
    pass


class _DataError(Exception):
    """Something retrying will not fix: bad value, missing column, permissions."""


# ============================================================================
# Identifiers
# ============================================================================

def pq(name: str) -> str:
    """Postgres identifier. The 63 limit is bytes, and Postgres would silently
    cut a longer name rather than fail."""
    if not name or "\x00" in name or len(name.encode("utf-8")) > 63:
        raise HTTPException(status_code=400, detail=f"Invalid Postgres identifier: {name!r}")
    return '"' + name.replace('"', '""') + '"'


sq = sfqr.quote_ident


def _simple(name: str) -> bool:
    return name.replace("_", "a").isalnum() and not name[0].isdigit()


def sf_name_for(pg_name: str) -> str:
    """lower_case in Postgres is UPPER_CASE in Snowflake; anything odd keeps its spelling."""
    return pg_name.upper() if _simple(pg_name) and pg_name == pg_name.lower() else pg_name


def pg_name_for(sf_name: str) -> str:
    return sf_name.lower() if _simple(sf_name) and sf_name == sf_name.upper() else sf_name


def _staging_name(target: str, job_id: str, n: int, upper: bool) -> str:
    tag = f"__shuttle_{job_id[:8]}_{n}"
    # Cut by bytes, not characters, so a non-ASCII name still fits Postgres.
    name = target.encode("utf-8")[:63 - len(tag)].decode("utf-8", "ignore") + tag
    return name.upper() if upper else name


# ============================================================================
# Type mapping
# ============================================================================

def pg_select_expr(col: Dict[str, Any]) -> str:
    """How a Postgres column is written into the CSV Snowflake reads."""
    q, t = pq(col["name"]), col["typname"]
    if col["is_array"]:
        return f"to_json({q})::text"
    if t in ("json", "jsonb", "vector"):
        return f"{q}::text"
    if t == "bytea":
        return f"encode({q}, 'hex')"
    if t == "timestamptz":
        return f"CASE WHEN isfinite({q}) THEN to_char({q} AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.US') || '+00:00' END"
    if t == "timestamp":
        return f"CASE WHEN isfinite({q}) THEN to_char({q}, 'YYYY-MM-DD HH24:MI:SS.US') END"
    if t == "date":
        return f"CASE WHEN isfinite({q}) THEN to_char({q}, 'YYYY-MM-DD') END"
    if t == "money":
        return f"{q}::numeric"
    if t in ("float4", "float8"):
        # Postgres writes Infinity, Snowflake documents inf.
        return f"CASE WHEN {q} = 'Infinity' THEN 'inf' WHEN {q} = '-Infinity' THEN '-inf' ELSE {q}::text END"
    return q


def sf_type_for(col: Dict[str, Any], warnings: List[str]) -> str:
    """Snowflake type for a new table built from a Postgres column."""
    t, mod = col["typname"], col["typmod"]
    if col["is_array"] or t in ("json", "jsonb", "vector"):
        return "VARIANT"
    if t in ("int2", "int4", "int8"):
        return "NUMBER(38,0)"
    if t == "numeric":
        if mod and mod > 4:
            p, s = ((mod - 4) >> 16) & 0xFFFF, (mod - 4) & 0xFFFF
            # Postgres 15+ allows a scale above the precision or below zero; Snowflake does not.
            if p <= 38 and s <= min(p, 37):
                return f"NUMBER({p},{s})"
            warnings.append(f"{col['name']} is numeric with precision/scale Snowflake cannot hold - copied as text")
            return "VARCHAR"
        warnings.append(f"{col['name']} is numeric with no precision - lands as NUMBER(38,10), "
                        f"so values with more than 10 decimals are rounded")
        return "NUMBER(38,10)"
    if t in ("float4", "float8"):
        return "FLOAT"
    if t == "money":
        return "NUMBER(19,2)"
    if t in ("varchar", "bpchar") and mod and mod > 4:
        return f"VARCHAR({mod - 4})"
    if t == "bool":
        return "BOOLEAN"
    if t == "date":
        return "DATE"
    if t == "timestamp":
        return "TIMESTAMP_NTZ(6)"
    if t == "timestamptz":
        return "TIMESTAMP_TZ(6)"
    if t == "time":
        return "TIME(6)"
    if t == "bytea":
        return "BINARY"
    if t == "uuid":
        return "VARCHAR(36)"
    return "VARCHAR"


def sf_load_expr(i: int, sf_type: str) -> str:
    """The COPY INTO transform for CSV field i landing in a column of sf_type."""
    base = sf_type.split("(")[0].strip().upper()
    if base in ("VARIANT", "OBJECT", "ARRAY"):
        return f"PARSE_JSON(${i})"
    if base == "GEOGRAPHY":
        return f"TO_GEOGRAPHY(${i})"
    if base == "GEOMETRY":
        return f"TO_GEOMETRY(${i})"
    return f"${i}"


def sf_select_expr(col: Dict[str, Any]) -> str:
    """How a Snowflake column is written into the CSV Postgres reads."""
    q, t = sq(col["name"]), col["data_type"]
    if t == "TEXT":
        return f"REPLACE({q}, CHR(0), '')"                       # Postgres text cannot hold NUL
    if t in ("VARIANT", "OBJECT", "ARRAY"):
        return f"REPLACE(TO_JSON({q}), '\\\\u0000', '')"        # nor can jsonb
    if t == "BINARY":
        return f"'\\\\x' || HEX_ENCODE({q})"
    if t in ("TIMESTAMP_TZ", "TIMESTAMP_LTZ"):
        return f"TO_VARCHAR({q}, 'YYYY-MM-DD HH24:MI:SS.FF6TZH:TZM')"
    if t == "TIMESTAMP_NTZ":
        return f"TO_VARCHAR({q}, 'YYYY-MM-DD HH24:MI:SS.FF6')"
    if t == "DATE":
        return f"TO_VARCHAR({q}, 'YYYY-MM-DD')"
    if t == "TIME":
        return f"TO_VARCHAR({q}, 'HH24:MI:SS.FF6')"
    if t in ("GEOGRAPHY", "GEOMETRY"):
        return f"ST_ASWKT({q})"
    if t == "VECTOR":
        return f"TO_JSON({q}::ARRAY)"
    return q


def pg_type_for(col: Dict[str, Any]) -> str:
    """Postgres type for a new table built from a Snowflake column."""
    t, p, s, n = col["data_type"], col["precision"], col["scale"], col["char_len"]
    if t == "NUMBER":
        p, s = p or 38, s or 0
        if s == 0:
            if p <= 9:
                return "integer"
            if p <= 18 or col.get("fits_bigint"):
                return "bigint"
        return f"numeric({p},{s})"
    if t == "FLOAT":
        return "double precision"
    if t == "TEXT":
        return f"varchar({n})" if n and n <= 10_485_760 and n != 16_777_216 else "text"
    if t == "BOOLEAN":
        return "boolean"
    if t == "DATE":
        return "date"
    if t == "TIME":
        return "time"
    if t == "TIMESTAMP_NTZ":
        return "timestamp"
    if t in ("TIMESTAMP_TZ", "TIMESTAMP_LTZ"):
        return "timestamptz"
    if t in ("VARIANT", "OBJECT", "ARRAY", "VECTOR"):
        return "jsonb"
    if t == "BINARY":
        return "bytea"
    return "text"


# ============================================================================
# Connections. Fresh ones, never the pooled ones the worksheets use - a copy
# holds a connection for minutes or hours.
# ============================================================================

def _pg(database: str, conn_id: Optional[str]):
    """Same login as the Postgres tab, plus what a long copy needs: TCP
    keepalives, so a connection that sits idle while others work is not
    silently dropped by a VPN or load balancer, and a longer connect timeout
    for a busy network. A failed connect is retryable."""
    import psycopg2
    cfg = pgs._cfg(conn_id)
    try:
        conn = psycopg2.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
            dbname=database or cfg["default_database"], sslmode=cfg["sslmode"],
            connect_timeout=max(int(cfg.get("connect_timeout") or 10), 30),
            application_name="SmartHub_DataShuttle",
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=6,
        )
    except psycopg2.OperationalError as e:
        raise ConnectionError(f"Could not connect to Postgres: {e}".strip())
    conn.autocommit = True
    with conn.cursor() as cur:
        # extra_float_digits = 3: exact floats on Postgres < 12 too (what pg_dump does).
        cur.execute("SET statement_timeout = 0; SET idle_in_transaction_session_timeout = 0; "
                    "SET client_encoding = 'UTF8'; SET DateStyle = 'ISO, YMD'; SET extra_float_digits = 3")
    return conn


def _sf(profile: str, database: Optional[str] = None):
    try:
        conn = sfqr._connect(database=database, profile=profile)
    except HTTPException as e:
        if e.status_code == 502:                    # network / login timeout - worth another go
            raise ConnectionError(e.detail)
        raise
    timeout = sfqr._require_config()["copy_timeout_seconds"]
    cur = conn.cursor()
    cur.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {int(timeout)}, "
                "TIMESTAMP_INPUT_FORMAT = 'AUTO', DATE_INPUT_FORMAT = 'AUTO', TIME_INPUT_FORMAT = 'AUTO', "
                "BINARY_INPUT_FORMAT = 'HEX', TIMEZONE = 'UTC'")
    cur.close()
    return conn


def _close(conn):
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _retryable(e: Exception) -> bool:
    if isinstance(e, (_Cancelled, _DataError, HTTPException)):
        return False
    import psycopg2
    if isinstance(e, psycopg2.Error):
        return isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError))
    try:
        from snowflake.connector import errors as sfe
        if isinstance(e, sfe.ProgrammingError):        # SQL / data problem, same again next time
            return False
    except ImportError:
        pass
    return True


# ============================================================================
# Catalog
# ============================================================================

def _pg_columns(cur, schema: str, table: str) -> List[Dict[str, Any]]:
    cur.execute(
        """
        SELECT a.attname, t.typname, a.atttypmod, a.attnotnull, a.atthasdef,
               t.typcategory = 'A', COALESCE(et.typname, t.typname)
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_type t ON t.oid = a.atttypid
        LEFT JOIN pg_type et ON et.oid = t.typelem AND t.typcategory = 'A'
        WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (schema, table),
    )
    return [{"name": r[0], "typname": r[6] if r[5] else r[1], "typmod": r[2], "not_null": r[3],
             "has_default": r[4], "is_array": r[5]} for r in cur.fetchall()]


def _pg_relation(cur, schema: str, table: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        """
        SELECT c.relkind, GREATEST(c.reltuples, 0)::bigint,
               CASE WHEN c.relkind IN ('r', 'm', 'p') THEN pg_total_relation_size(c.oid) END
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relname = %s
        """,
        (schema, table),
    )
    r = cur.fetchone()
    return {"kind": r[0], "est_rows": int(r[1] or 0), "bytes": r[2]} if r else None


def _pg_objects(cur, schema: str) -> List[str]:
    cur.execute(
        """
        SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition
        ORDER BY c.relname
        """,
        (schema,),
    )
    return [r[0] for r in cur.fetchall()]


def _sf_columns(cur, database: str, schema: str, table: str) -> List[Dict[str, Any]]:
    cur.execute(
        f"""
        SELECT COLUMN_NAME, DATA_TYPE, NUMERIC_PRECISION, NUMERIC_SCALE, CHARACTER_MAXIMUM_LENGTH,
               IS_NULLABLE, COLUMN_DEFAULT
        FROM {sq(database)}.INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
        ORDER BY ORDINAL_POSITION
        """,
        (schema, table),
    )
    return [{"name": r[0], "data_type": (r[1] or "").upper(), "precision": r[2], "scale": r[3],
             "char_len": r[4], "not_null": r[5] == "NO", "has_default": r[6] is not None}
            for r in cur.fetchall()]


def _sf_relation(cur, database: str, schema: str, table: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        f"SELECT TABLE_TYPE, ROW_COUNT, BYTES FROM {sq(database)}.INFORMATION_SCHEMA.TABLES "
        f"WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?",
        (schema, table),
    )
    r = cur.fetchone()
    if not r:
        return None
    kind = "view" if "VIEW" in (r[0] or "").upper() else "external" if "EXTERNAL" in (r[0] or "").upper() else "table"
    return {"kind": kind, "est_rows": int(r[1] or 0), "bytes": r[2]}


def _sf_objects(cur, database: str, schema: str) -> List[str]:
    cur.execute(
        f"SELECT TABLE_NAME FROM {sq(database)}.INFORMATION_SCHEMA.TABLES "
        f"WHERE TABLE_SCHEMA = ? AND TABLE_TYPE NOT ILIKE '%EXTERNAL%' ORDER BY TABLE_NAME",
        (schema,),
    )
    return [r[0] for r in cur.fetchall()]


def _sf_schema_exists(cur, database: str, schema: str) -> bool:
    cur.execute(f"SELECT 1 FROM {sq(database)}.INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = ?", (schema,))
    return cur.fetchone() is not None


def _match_columns(src: List[Dict[str, Any]], tgt: List[Dict[str, Any]], where: str) -> List[Dict[str, Any]]:
    """Pair each source column with a target column by name, case-insensitive.
    Returns the target columns in source order. Raises if they cannot line up."""
    by_name = {c["name"].lower(): c for c in tgt}
    missing = [c["name"] for c in src if c["name"].lower() not in by_name]
    if missing:
        raise _DataError(f"{where} has no column for: {', '.join(missing[:10])}"
                         + (f" and {len(missing) - 10} more" if len(missing) > 10 else ""))
    src_names = {c["name"].lower() for c in src}
    required = [c["name"] for c in tgt if c["not_null"] and not c["has_default"] and c["name"].lower() not in src_names]
    if required:
        raise _DataError(f"{where} has NOT NULL columns the source does not have: {', '.join(required)}")
    return [by_name[c["name"].lower()] for c in src]


# ============================================================================
# Request + plan
# ============================================================================

class ShuttleRequest(BaseModel):
    direction: str                          # "pg_to_sf" | "sf_to_pg"
    copy_type: str = "table"                # "table" | "tables" | "schema"
    if_exists: str = "fail"                 # "fail" | "overwrite" | "append"
    pg_conn_id: Optional[str] = None
    source_database: str
    source_schema: str
    source_table: Optional[str] = None
    source_tables: Optional[List[str]] = None
    target_database: str
    target_schema: str
    target_table: Optional[str] = None
    source_workers: Optional[int] = None    # None = sized per table
    target_workers: Optional[int] = None
    confirm: bool = False


def _validate(body: ShuttleRequest):
    if body.direction not in ("pg_to_sf", "sf_to_pg"):
        raise HTTPException(status_code=400, detail="direction must be 'pg_to_sf' or 'sf_to_pg'")
    if body.copy_type not in ("table", "tables", "schema"):
        raise HTTPException(status_code=400, detail="copy_type must be 'table', 'tables' or 'schema'")
    if body.if_exists not in ("fail", "overwrite", "append"):
        raise HTTPException(status_code=400, detail="if_exists must be 'fail', 'overwrite' or 'append'")
    for field in ("source_database", "source_schema", "target_database", "target_schema"):
        if not (getattr(body, field) or "").strip():
            raise HTTPException(status_code=400, detail=f"{field.replace('_', ' ')} is required")
    if body.copy_type == "table" and not (body.source_table or "").strip():
        raise HTTPException(status_code=400, detail="Pick a source table")
    if body.copy_type == "tables" and not body.source_tables:
        raise HTTPException(status_code=400, detail="Pick at least one table")
    src_engine, tgt_engine = _engines(body.direction)
    for side, value, engine in (("Source", body.source_workers, src_engine), ("Target", body.target_workers, tgt_engine)):
        top = _max_workers(engine)
        if value is not None and not 1 <= value <= top:
            raise HTTPException(status_code=400, detail=f"{side} workers must be 1 to {top} on this server "
                                                        f"(limited by the {ENGINE_LABEL[engine]} connection cap). "
                                                        f"Leave it blank for auto.")
    if not sfqr._require_config()["allow_writes"] and body.direction == "pg_to_sf":
        raise HTTPException(status_code=403, detail="Snowflake is configured read-only on this deployment")
    if not pgs._cfg(body.pg_conn_id)["allow_writes"] and body.direction == "sf_to_pg":
        raise HTTPException(status_code=403, detail="This Postgres connection is configured read-only")


def _build_plan(body: ShuttleRequest) -> Dict[str, Any]:
    """What would be copied, where it lands and what to watch out for.
    Reads catalogs only - no row counts, no scans."""
    to_sf = body.direction == "pg_to_sf"
    pg_db = body.source_database if to_sf else body.target_database
    pg_conn = sf_conn = None
    if not _budget.take(1, 1, timeout=30):
        raise HTTPException(status_code=503, detail="Data Shuttle is using all its connections right now - "
                                                    "try the preview again in a minute")
    try:
        pg_conn = _pg(pg_db, body.pg_conn_id)
        sf_conn = _sf(sfqr.WRITE_PROFILE if to_sf else sfqr.READ_PROFILE)
        pcur, scur = pg_conn.cursor(), sf_conn.cursor()

        if body.copy_type == "table":
            names = [body.source_table.strip()]
        elif body.copy_type == "tables":
            names = list(dict.fromkeys(n.strip() for n in body.source_tables if n and n.strip()))
        else:
            names = (_pg_objects(pcur, body.source_schema) if to_sf
                     else _sf_objects(scur, body.source_database, body.source_schema))
        if not names:
            raise HTTPException(status_code=400, detail="Nothing to copy - the source schema is empty")
        if len(names) > MAX_OBJECTS:
            raise HTTPException(status_code=400, detail=f"{len(names)} objects - one job takes {MAX_OBJECTS} at most. "
                                                        f"Copy in smaller batches.")

        if to_sf:
            target_schema_exists = _sf_schema_exists(scur, body.target_database, body.target_schema)
            warnings_global = [] if pg_conn.server_version >= 140000 else [
                "Postgres is older than 14, so each table is read by one connection "
                "(no parallel ctid ranges). It works, just slower."]
        else:
            pcur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (body.target_schema,))
            target_schema_exists = pcur.fetchone() is not None
            warnings_global = []

        items = []
        for n, name in enumerate(names):
            warnings: List[str] = []
            if body.copy_type == "table" and body.target_table and body.target_table.strip():
                tgt_name = body.target_table.strip()
            else:
                tgt_name = sf_name_for(name) if to_sf else pg_name_for(name)

            if to_sf:
                rel = _pg_relation(pcur, body.source_schema, name)
                if not rel:
                    raise HTTPException(status_code=404, detail=f"{body.source_schema}.{name} not found in Postgres")
                cols = _pg_columns(pcur, body.source_schema, name)
                if rel["kind"] in ("v", "f"):
                    warnings.append("view or foreign table - read by one connection, it cannot be split")
                tgt_cols = (_sf_columns(scur, body.target_database, body.target_schema, tgt_name)
                            if target_schema_exists else [])
                mapped = {c["name"]: sf_type_for(c, warnings) for c in cols}
            else:
                rel = _sf_relation(scur, body.source_database, body.source_schema, name)
                if not rel:
                    raise HTTPException(status_code=404, detail=f"{body.source_schema}.{name} not found in Snowflake")
                if rel["kind"] == "external":
                    raise HTTPException(status_code=400, detail=f"{name} is an external table - not copyable")
                cols = _sf_columns(scur, body.source_database, body.source_schema, name)
                tgt_cols = _pg_columns(pcur, body.target_schema, tgt_name) if target_schema_exists else []
                mapped = None   # worked out at run time, the NUMBER probe reads the data

            if not cols:
                raise HTTPException(status_code=400, detail=f"{name} has no columns")

            exists = bool(tgt_cols)
            if exists and body.if_exists == "fail":
                raise HTTPException(
                    status_code=409,
                    detail=f"{body.target_schema}.{tgt_name} already exists. Pick 'Replace rows' or 'Append rows', "
                           f"or another target name.",
                )
            if exists:
                try:
                    _match_columns(cols, tgt_cols, f"Target {tgt_name}")
                except _DataError as e:
                    raise HTTPException(status_code=400, detail=str(e))

            items.append({
                "n": n, "name": name, "target": tgt_name, "target_exists": exists,
                "est_rows": rel["est_rows"], "bytes": rel["bytes"], "kind": rel["kind"],
                "columns": len(cols), "types": mapped if not exists else None,
                "warnings": warnings,
            })
        return {"items": items, "warnings": warnings_global, "target_schema_exists": target_schema_exists}
    finally:
        _close(pg_conn)
        _close(sf_conn)
        _budget.give(1, 1)


# ============================================================================
# Jobs
# ============================================================================

_LEVELS = {"info": logging.INFO, "success": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}


def _log(job: Dict[str, Any], level: str, message: str):
    # Every line also goes to the server log, so it outlives the job and a restart.
    logger.log(_LEVELS.get(level, logging.INFO), "[shuttle %s %s] %s", job["job_id"][:8], job["owner"], message)
    with job["lock"]:
        job["logs"].append({"ts": time.time(), "level": level, "message": message})
        if len(job["logs"]) > 2000:
            job["logs"] = job["logs"][-2000:]
    _touch(job)


def _touch(job: Dict[str, Any]):
    job["updated_at"] = time.time()
    job["pulse"].set()


def _check(job: Dict[str, Any]):
    if job["cancel_requested"]:
        raise _Cancelled()


def _track(job: Dict[str, Any], kind: str, conn):
    with job["lock"]:
        job["active"].append((kind, conn))


def _untrack(job: Dict[str, Any], conn):
    with job["lock"]:
        job["active"] = [a for a in job["active"] if a[1] is not conn]


def _retry(job: Dict[str, Any], what: str, fn):
    for attempt in range(1, RETRIES + 1):
        _check(job)
        try:
            return fn()
        except Exception as e:
            if job["cancel_requested"]:
                raise _Cancelled()
            if attempt == RETRIES or not _retryable(e):
                raise
            _log(job, "warn", f"{what} failed ({e}), retrying ({attempt}/{RETRIES - 1})")
            time.sleep(3 * attempt)


ENGINE_LABEL = {"pg": "Postgres", "sf": "Snowflake"}


def _engines(direction: str):
    """(source engine, target engine) as budget keys."""
    return ("pg", "sf") if direction == "pg_to_sf" else ("sf", "pg")


def _max_workers(engine: str) -> int:
    """Most workers one side of a table can get: MAX_WORKERS, or less when that
    database's connection cap is lower (one connection per worker plus a main one)."""
    return max(1, min(MAX_WORKERS, _budget.cap[engine] - 1))


def _auto_workers(item: Dict[str, Any]) -> int:
    """Workers for one table when the user did not say. About one per 512 MB,
    1 for small tables, MAX_WORKERS for big ones. Size comes from the catalog
    (Postgres: on disk, Snowflake: compressed), so it is a rough guide - the
    reader count is cut to the number of chunks anyway."""
    size = item.get("bytes")
    if not size:
        # Views have no size. Go by rows, ~200 bytes each, else a middle value.
        rows = item.get("est_rows") or 0
        size = rows * 200 if rows else None
    if size is None:
        return DEFAULT_WORKERS
    return max(1, min(MAX_WORKERS, math.ceil(size / (512 * 1024 * 1024))))


def _label(db: str, schema: str, name: Optional[str] = None) -> str:
    return f"{db}.{schema}" + (f".{name}" if name else "")


def _new_job(body: ShuttleRequest, plan: Dict[str, Any], owner: str) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    one = body.copy_type == "table"
    job = {
        "job_id": job_id, "owner": (owner or "").strip().lower(), "body": body,
        "direction": body.direction, "copy_type": body.copy_type, "if_exists": body.if_exists,
        "source": ("Postgres " if body.direction == "pg_to_sf" else "Snowflake ")
                  + _label(body.source_database, body.source_schema, plan["items"][0]["name"] if one else None),
        "target": ("Snowflake " if body.direction == "pg_to_sf" else "Postgres ")
                  + _label(body.target_database, body.target_schema, plan["items"][0]["target"] if one else None),
        "status": "running",
        "tables": {it["n"]: {"name": it["name"], "target": it["target"], "status": "pending", "phase": "queued",
                             "est_rows": it["est_rows"], "rows_copied": 0, "chunks_total": 0, "chunks_done": 0,
                             "chunks_fetched": 0, "stages": {}, "phase_at": None,
                             "error": None} for it in plan["items"]},
        "items": plan["items"],
        "source_workers": body.source_workers,   # None = sized per table
        "target_workers": body.target_workers,
        "logs": [], "lock": threading.Lock(), "cancel_requested": False, "active": [], "_progress": {},
        "created_at": time.time(), "started_at": time.time(), "finished_at": None, "updated_at": time.time(),
        "done": threading.Event(), "pulse": threading.Event(),
        "target_keys": _target_keys(body, plan),
    }
    with _jobs_lock:
        busy = _busy_targets(job["target_keys"])
        if busy:
            raise HTTPException(status_code=409, detail="Another copy is writing to " + ", ".join(busy[:5])
                                + (f" and {len(busy) - 5} more" if len(busy) > 5 else "")
                                + " right now. Wait for it to finish or cancel it.")
        _jobs[job_id] = job
    return job


_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def _valid_id(job_id: str) -> str:
    """Job ids end up in file paths (history_pending/<id>.json) - only ever a uuid4 hex."""
    if not _JOB_ID.match(job_id or ""):
        raise HTTPException(status_code=404, detail="Unknown shuttle job")
    return job_id


def _target_keys(body: ShuttleRequest, plan: Dict[str, Any]) -> set:
    engine = "sf" if body.direction == "pg_to_sf" else f"pg:{body.pg_conn_id or ''}"
    return {(engine, body.target_database, body.target_schema, it["target"]) for it in plan["items"]}


def _busy_targets(keys: set) -> List[str]:
    """Targets a running job is already writing. Two copies landing in one
    table at once would race on the rename or double-append. Caller holds _jobs_lock."""
    hit = set()
    for j in _jobs.values():
        if j["status"] == "running":
            hit |= keys & j["target_keys"]
    return sorted(f"{k[2]}.{k[3]}" for k in hit)


def _get_job(job_id: str, owner: str) -> Dict[str, Any]:
    _valid_id(job_id)
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or job["owner"] != (owner or "").strip().lower():
        raise HTTPException(status_code=404, detail="Unknown or expired shuttle job")
    return job


def _cleanup_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with _jobs_lock:
        for jid in [j for j, v in _jobs.items() if v["status"] != "running" and v["created_at"] < cutoff]:
            _jobs.pop(jid, None)


def _set(job, n, **kw):
    with job["lock"]:
        t = job["tables"][n]
        if "phase" in kw and kw["phase"] != t["phase"]:
            # How long each step took, for the summary line at the end.
            now = time.time()
            if t.get("phase_at") and t["phase"] not in ("queued", "starting"):
                t["stages"][t["phase"]] = t["stages"].get(t["phase"], 0) + now - t["phase_at"]
            t["phase_at"] = now
        t.update(kw)
    _touch(job)


def _mb(b: float) -> str:
    return f"{b / 1024 ** 3:.1f} GB" if b >= 1024 ** 3 else f"{b / 1024 ** 2:.0f} MB"


def _dur(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}h {sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m {sec % 60:02d}s" if sec >= 60 else f"{sec}s"


class _Slot:
    """What one worker is doing right now. The hot loop bumps plain ints and
    the status reader takes them without a lock - a number a moment stale is
    fine for a progress line, a lock per row is not."""
    __slots__ = ("role", "step", "file", "since", "done", "total", "rows", "est_rows", "watch")

    def __init__(self, role: str):
        self.role = role
        self.watch = None           # a folder whose size is this worker's progress (Snowflake GET reports none)
        self.idle()

    def start(self, file: str, step: str, total: Optional[int] = None, est_rows: Optional[int] = None):
        self.file, self.step, self.since = file, step, time.time()
        self.done, self.total, self.rows, self.est_rows = 0, total, 0, est_rows

    def idle(self):
        self.file, self.step, self.since = None, "waiting", time.time()
        self.done, self.total, self.rows, self.est_rows = 0, None, 0, None

    def view(self) -> Dict[str, Any]:
        if self.watch and self.file:
            self.done = _dir_bytes(self.watch)
        pct = None
        if self.total:
            pct = min(99, int(self.done * 100 / self.total))
        elif self.est_rows:
            pct = min(99, int(self.rows * 100 / self.est_rows))
        return {"role": self.role, "step": self.step, "file": self.file, "secs": int(time.time() - self.since),
                "percent": pct, "rows": self.rows or None}


def _dir_bytes(path: str) -> int:
    total = 0
    try:
        for e in os.scandir(path):
            if e.is_file():
                total += e.stat().st_size
            elif e.is_dir():
                total += _dir_bytes(e.path)
    except OSError:
        pass
    return total


class _CountingWriter:
    """Postgres COPY TO calls write() once per row, so this counts rows live."""

    def __init__(self, out, slot: _Slot):
        self.out, self.slot = out, slot

    def write(self, b):
        self.slot.rows += 1
        self.slot.done += len(b)
        return self.out.write(b)


class _CountingReader:
    """Wraps the compressed file under gzip, so the slot knows how far in it is."""

    def __init__(self, raw, slot: _Slot):
        self.raw, self.slot = raw, slot

    def read(self, n=-1):
        b = self.raw.read(n)
        self.slot.done += len(b)
        return b

    def __getattr__(self, name):
        return getattr(self.raw, name)


class _Progress:
    """The live picture of one table: both stages, throughput, time left and
    what every worker is doing. Shown on the page as it changes and written to
    the log every 15s, plus one line per finished file.
    'fetch' reads from the source (Postgres chunk / Snowflake download),
    'load' writes into the target."""

    EVERY = 15
    WINDOW = 6                      # beats kept for the current rate (~90s)

    def __init__(self, job, item, total_files: int, fetch_word: str, load_word: str,
                 total_bytes: Optional[int] = None, total_rows: Optional[int] = None,
                 file_rows: Optional[Dict[str, int]] = None):
        self.job, self.item, self.total = job, item, total_files
        self.fetch_word, self.load_word = fetch_word, load_word
        self.total_bytes, self.total_rows = total_bytes, total_rows or None
        self.file_rows = file_rows or {}
        self.fetched = self.loaded = 0
        self.fetched_bytes = self.loaded_bytes = self.read_rows = self.rows = 0
        self.slots: List[_Slot] = []
        self.lock = threading.Lock()
        self.started = time.time()
        self.samples: List[tuple] = []
        self.stop_evt = threading.Event()
        job["_progress"][item["n"]] = self
        threading.Thread(target=self._beat_loop, daemon=True).start()

    def slot(self, role: str) -> _Slot:
        s = _Slot(role)
        with self.lock:
            self.slots.append(s)
        return s

    def fetched_file(self, name: str, size: int, secs: float, rows: Optional[int] = None):
        with self.lock:
            self.fetched += 1
            self.fetched_bytes += size
            self.read_rows += rows or 0
            k = self.fetched
        _set(self.job, self.item["n"], chunks_fetched=k)
        _log(self.job, "info", f"{self.item['name']}: {self.fetch_word} {name} ({k}/{self.total}) - "
                               + (f"{rows:,} rows, " if rows is not None else "")
                               + f"{_mb(size)} in {_dur(secs)}, {size / 1024 ** 2 / max(secs, 0.001):.1f} MB/s")

    def loaded_file(self, name: str, rows: int, secs: float, size: int = 0):
        with self.lock:
            self.loaded += 1
            self.rows += rows
            self.loaded_bytes += size
            k = self.loaded
        _log(self.job, "info", f"{self.item['name']}: {self.load_word} {name} ({k}/{self.total}) - "
                               f"{rows:,} rows in {_dur(secs)}")

    def live(self) -> Dict[str, Any]:
        """Totals including the files still in flight."""
        with self.lock:
            slots = [s.view() for s in self.slots]
            fetched, loaded = self.fetched, self.loaded
            fetched_bytes, loaded_bytes, rows, read_rows = self.fetched_bytes, self.loaded_bytes, self.rows, self.read_rows
            in_flight = list(self.slots)
        partial_rows = partial_bytes = read_partial = fetch_partial = 0
        for s in in_flight:
            if s.role == "load" and s.file and s.total:
                partial_bytes += s.done
                partial_rows += int(self.file_rows.get(s.file, 0) * s.done / s.total)
            if s.role == "read" and s.file:
                read_partial += s.rows
            if s.role == "fetch" and s.file and s.watch:
                fetch_partial += s.done
        rows_live = rows + partial_rows
        loaded_live = loaded_bytes + partial_bytes
        elapsed = time.time() - self.started

        if self.total_bytes:                       # Snowflake -> Postgres: exact sizes from the unload
            percent = loaded_live / self.total_bytes * 100
        elif self.total_rows:
            percent = rows_live / self.total_rows * 100
        else:
            percent = loaded / self.total * 100 if self.total else 0

        # Current rate over the last ~90s; overall average until there is a window.
        now = time.time()
        rate_rows = rows_live / elapsed if elapsed else 0
        rate_pct = percent / elapsed if elapsed else 0
        if len(self.samples) >= 2:
            t0, p0, r0 = self.samples[0]
            if now - t0 > 1:
                rate_pct = (percent - p0) / (now - t0)
                rate_rows = (rows_live - r0) / (now - t0)
        eta = (100 - percent) / rate_pct if rate_pct > 0 else None

        return {
            "files_total": self.total, "fetched": fetched, "loaded": loaded,
            "fetched_bytes": fetched_bytes + fetch_partial, "loaded_bytes": loaded_live, "total_bytes": self.total_bytes,
            "read_rows": read_rows + read_partial, "rows": rows_live, "total_rows": self.total_rows,
            "percent": round(min(99.9, percent), 1),
            "fetch_mb_s": round((fetched_bytes + fetch_partial) / 1024 ** 2 / elapsed, 2) if elapsed else 0,
            "rows_s": int(max(rate_rows, 0)), "eta_secs": int(eta) if eta is not None else None,
            "elapsed_secs": int(elapsed), "workers": slots,
            "fetch_word": self.fetch_word, "load_word": self.load_word,
        }

    def summary(self, final: bool = False) -> str:
        v = self.live()
        fetch = f"{self.fetch_word} {v['fetched']}/{v['files_total']}"
        if v["total_bytes"]:
            fetch += f" ({_mb(v['fetched_bytes'])} of {_mb(v['total_bytes'])})"
        elif v["read_rows"]:
            fetch += f" ({v['read_rows']:,} rows read)"
        fetch += f", {v['fetch_mb_s']} MB/s"
        load = f"{self.load_word} {v['loaded']}/{v['files_total']}, {v['rows']:,}" \
               + (f" of {v['total_rows']:,}" if v["total_rows"] else "") + " rows"
        if final:
            return f"{self.item['name']}: {fetch} · {load} · {_dur(v['elapsed_secs'])}"
        line = f"{self.item['name']}: {v['percent']}% · {fetch} · {load} · {v['rows_s']:,} rows/s · " \
               f"{_dur(v['elapsed_secs'])} so far" + (f" · ~{_dur(v['eta_secs'])} left" if v["eta_secs"] is not None else "")
        busy = [w for w in v["workers"] if w["file"]]
        idle = len(v["workers"]) - len(busy)
        if busy:
            line += " · now: " + ", ".join(
                f"{w['step']} {w['file']} ({_dur(w['secs'])}" + (f", {w['percent']}%" if w["percent"] is not None else "") + ")"
                for w in sorted(busy, key=lambda w: -w["secs"])[:8])
            if len(busy) > 8:
                line += f" +{len(busy) - 8} more"
        if idle:
            line += f" · {idle} worker(s) waiting"
        return line

    def _beat_loop(self):
        while not self.stop_evt.wait(self.EVERY):
            if self.job["cancel_requested"]:
                return
            v = self.live()
            self.samples = (self.samples + [(time.time(), v["percent"], v["rows"])])[-self.WINDOW:]
            _log(self.job, "info", "Status - " + self.summary())

    def stop(self):
        self.stop_evt.set()
        self.job["_progress"].pop(self.item["n"], None)


def _add(job, n, **kw):
    with job["lock"]:
        t = job["tables"][n]
        for k, v in kw.items():
            t[k] += v
    _touch(job)


def _run_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        return
    body: ShuttleRequest = job["body"]
    work_dir = None
    _history.save(job)
    threading.Thread(target=_heartbeat_loop, args=(job,), daemon=True).start()
    try:
        # Inside the try: a full or unwritable disk must fail the job, not
        # leave it 'running' forever.
        work_dir = tempfile.mkdtemp(prefix=f"{job_id[:8]}_", dir=_work_root())
        _log(job, "info", f"Started: {job['source']} -> {job['target']}, {len(job['items'])} object(s), "
                          + f"workers: source {job['source_workers'] or 'auto'}, target {job['target_workers'] or 'auto'}")
        need = (0, 1) if body.direction == "pg_to_sf" else (1, 0)
        _budget.take(*need, job=job)
        try:
            (_ensure_sf_schema if body.direction == "pg_to_sf" else _ensure_pg_schema)(job, body)
        finally:
            _budget.give(*need)
        for item in job["items"]:
            if job["cancel_requested"]:
                break
            n = item["n"]
            _set(job, n, status="running", phase="starting")
            started = time.time()
            # Each side holds one main connection plus one per worker on that side.
            src_engine, tgt_engine = _engines(body.direction)
            auto = _auto_workers(item)
            notes = []
            for side, engine in (("source", src_engine), ("target", tgt_engine)):
                wanted = job[f"{side}_workers"] or auto
                got = min(wanted, _max_workers(engine))
                item[f"{side}_workers"] = got
                notes.append(f"{side} {got}" + ("" if job[f"{side}_workers"] else " (auto)")
                             + (f", cut from {wanted} by the {ENGINE_LABEL[engine]} cap" if got < wanted else ""))
            need = {src_engine: item["source_workers"] + 1, tgt_engine: item["target_workers"] + 1}
            _log(job, "info", f"{item['name']}: workers {'; '.join(notes)}")
            taken = False
            try:
                _set(job, n, phase="waiting for connections")
                _budget.take(need["pg"], need["sf"], job=job)
                taken = True
                _set(job, n, phase="starting")
                run = _pg_to_sf if body.direction == "pg_to_sf" else _sf_to_pg
                rows = run(job, item, os.path.join(work_dir, str(n)))
                _set(job, n, status="success", phase="done", rows_copied=rows, est_rows=rows)
                stages = ", ".join(f"{k} {_dur(v)}" for k, v in job["tables"][n]["stages"].items())
                _log(job, "success", f"{item['name']} -> {item['target']}: {rows:,} rows in "
                                     f"{_dur(time.time() - started)} ({stages})")
            except _Cancelled:
                # Staged rows are dropped with the staging table: nothing landed.
                _set(job, n, status="failed", phase="cancelled", error="Cancelled", rows_copied=0)
                break
            except Exception as e:
                msg = e.detail if isinstance(e, HTTPException) else str(e)
                logger.exception("Shuttle %s: %s failed", job_id, item["name"])
                _set(job, n, status="failed", phase="failed", error=msg, rows_copied=0)
                _log(job, "error", f"{item['name']} failed, target left as it was: {msg}")
            finally:
                if taken:
                    _budget.give(need["pg"], need["sf"])
                _history.save(job)
        _skip_rest(job)
        with job["lock"]:
            statuses = {t["status"] for t in job["tables"].values()}
            if job["cancel_requested"]:
                job["status"] = "cancelled"
            elif statuses == {"success"}:
                job["status"] = "success"
            elif "success" in statuses:
                job["status"] = "partial"
            else:
                job["status"] = "failed"
        _log(job, "success" if job["status"] == "success" else "warn", f"Shuttle {job['status']}")
    except Exception as e:
        with job["lock"]:
            job["status"] = "cancelled" if isinstance(e, _Cancelled) else "failed"
        if not isinstance(e, _Cancelled):
            logger.exception("Shuttle %s failed", job_id)
        _log(job, "error", f"Shuttle failed: {e.detail if isinstance(e, HTTPException) else e}")
    finally:
        _skip_rest(job)
        if work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        job["finished_at"] = time.time()
        job["done"].set()
        _touch(job)
        _history.save(job, with_logs=True)
        try:
            from audit import write_audit_event
            with job["lock"]:
                tables = list(job["tables"].values())
            rows = sum(t["rows_copied"] for t in tables)
            errors = "; ".join(f"{t['name']}: {t['error']}" for t in tables if t.get("error"))
            write_audit_event("data_shuttle", job["owner"], job["target"],
                              f"{job['status']}: {job['source']} -> {job['target']} ({job['if_exists']}), "
                              f"{rows:,} rows, {_dur(job['finished_at'] - job['started_at'])}"
                              + (f". Errors: {errors[:500]}" if errors else ""))
        except Exception:
            logger.warning("Shuttle %s: audit event not written", job_id[:8], exc_info=True)


def _skip_rest(job):
    """Tables a cancel or a job-level failure never got to: say so, rather
    than leave them 'queued' forever in the page and the history."""
    with job["lock"]:
        for t in job["tables"].values():
            if t["status"] == "pending":
                t.update(status="skipped", phase="skipped")


def _ensure_sf_schema(job, body):
    conn = _sf(sfqr.WRITE_PROFILE)
    try:
        cur = conn.cursor()
        if not _sf_schema_exists(cur, body.target_database, body.target_schema):
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {sq(body.target_database)}.{sq(body.target_schema)}")
            _log(job, "info", f"Created schema {body.target_database}.{body.target_schema}")
    finally:
        _close(conn)


def _ensure_pg_schema(job, body):
    conn = _pg(body.target_database, body.pg_conn_id)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (body.target_schema,))
            if not cur.fetchone():
                cur.execute(f"CREATE SCHEMA IF NOT EXISTS {pq(body.target_schema)}")
                _log(job, "info", f"Created schema {body.target_schema}")
    finally:
        _close(conn)


# ----------------------------------------------------------------------------
# Postgres -> Snowflake
# ----------------------------------------------------------------------------

def _pg_chunks(cur, schema: str, table: str, kind: str, workers: int, ctid_ok: bool) -> List[Dict[str, Any]]:
    """ctid page ranges. Partitioned tables are split per leaf partition; views
    and foreign tables have no ctid, so they are one chunk."""
    if kind == "p":
        cur.execute(
            """
            SELECT n.nspname, c.relname,
                   CASE WHEN c.relkind = 'f' THEN NULL
                        ELSE pg_relation_size(c.oid) / current_setting('block_size')::bigint END
            FROM pg_partition_tree(%s::regclass) pt
            JOIN pg_class c ON c.oid = pt.relid JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE pt.isleaf AND c.relkind IN ('r', 'm', 'f')
            """,
            (f"{pq(schema)}.{pq(table)}",),
        )
        # Foreign partitions hold rows too. Leaving them out loses data, and
        # the row count check would not notice (it only counts what was read).
        rows = cur.fetchall()
        foreign = [{"schema": s, "table": t, "start": None, "end": None} for s, t, pages in rows if pages is None]
        leaves = [r for r in rows if r[2] is not None]
    elif kind in ("r", "m"):
        cur.execute("SELECT pg_relation_size(%s::regclass) / current_setting('block_size')::bigint",
                    (f"{pq(schema)}.{pq(table)}",))
        leaves = [(schema, table, cur.fetchone()[0])]
        foreign = []
    else:
        return [{"schema": schema, "table": table, "start": None, "end": None}]

    cur.execute("SELECT current_setting('block_size')::bigint")
    block = cur.fetchone()[0]
    chunks = list(foreign)
    for s, t, pages in leaves:
        pages = int(pages or 0)
        size = pages * block
        if not ctid_ok or size < SMALL_TABLE_BYTES:
            chunks.append({"schema": s, "table": t, "start": None, "end": None})
            continue
        n = min(MAX_CHUNKS, max(workers, math.ceil(size / CHUNK_BYTES)))
        per = math.ceil(pages / n)
        for start in range(0, pages, per):
            chunks.append({"schema": s, "table": t, "start": start, "end": start + per})
        chunks[-1]["end"] = None          # open-ended, nothing past the last page is missed
    # A partitioned table with no partitions yet: one (empty) read of the parent.
    return chunks or [{"schema": schema, "table": table, "start": None, "end": None}]


def _pg_to_sf(job: Dict[str, Any], item: Dict[str, Any], work_dir: str) -> int:
    body: ShuttleRequest = job["body"]
    n, readers, loaders = item["n"], item["source_workers"], item["target_workers"]
    os.makedirs(work_dir, exist_ok=True)
    stage = f"@~/{STAGE_ROOT}/{job['job_id']}/{n}/"
    staging = _staging_name(item["target"], job["job_id"], n, upper=True)
    tgt_fq = f"{sq(body.target_database)}.{sq(body.target_schema)}.{sq(item['target'])}"
    stg_fq = f"{sq(body.target_database)}.{sq(body.target_schema)}.{sq(staging)}"

    control = _pg(body.source_database, body.pg_conn_id)
    _track(job, "pg", control)
    try:
        sf = _sf(sfqr.WRITE_PROFILE, body.target_database)
    except Exception:
        _untrack(job, control)                   # else it leaks for the life of the process
        _close(control)
        raise
    _track(job, "sf", sf)
    try:
        # One snapshot for every reader, held open by this connection until the
        # last chunk is read - same trick pg_dump uses for parallel dumps. Every
        # chunk then counts exactly what the snapshot holds.
        ccur = control.cursor()
        ccur.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        ccur.execute("SELECT pg_export_snapshot()")
        snapshot = ccur.fetchone()[0]

        cols = _pg_columns(ccur, body.source_schema, item["name"])
        rel = _pg_relation(ccur, body.source_schema, item["name"])
        chunks = _pg_chunks(ccur, body.source_schema, item["name"], rel["kind"], readers,
                            control.server_version >= 140000)
        _set(job, n, chunks_total=len(chunks), phase="preparing")

        scur = sf.cursor()
        tgt_cols = _sf_columns(scur, body.target_database, body.target_schema, item["target"])
        if tgt_cols and body.if_exists == "fail":
            raise _DataError(f"{item['target']} appeared in Snowflake after the preview")
        if tgt_cols:
            matched = _match_columns(cols, tgt_cols, f"Target {item['target']}")
            scur.execute(f"CREATE TRANSIENT TABLE {stg_fq} LIKE {tgt_fq}")
            by_name = {c["name"]: c for c in _sf_columns(scur, body.target_database, body.target_schema, staging)}
            load_cols = [(m["name"], by_name[m["name"]]["data_type"]) for m in matched]
        else:
            warns: List[str] = []
            ddl = [(sf_name_for(c["name"]), sf_type_for(c, warns), c["not_null"]) for c in cols]
            for w in warns:
                _log(job, "warn", f"{item['name']}: {w}")
            scur.execute(f"CREATE TABLE {stg_fq} (" + ", ".join(
                f"{sq(name)} {typ}{' NOT NULL' if nn else ''}" for name, typ, nn in ddl) + ")")
            load_cols = [(name, typ) for name, typ, _ in ddl]

        select_list = ", ".join(pg_select_expr(c) for c in cols)
        copy_sql = (f"COPY INTO {stg_fq} ({', '.join(sq(c) for c, _ in load_cols)}) FROM "
                    f"(SELECT {', '.join(sf_load_expr(i + 1, t) for i, (_, t) in enumerate(load_cols))} FROM {stage}) "
                    "FILES = ('{file}') FILE_FORMAT = (" + SF_CSV + " ENCODING = 'UTF8' ERROR_ON_COLUMN_COUNT_MISMATCH = TRUE) "
                    "ON_ERROR = ABORT_STATEMENT PURGE = TRUE")
        _set(job, n, phase="copying")
        _log(job, "info", f"{item['name']}: {len(chunks)} chunk(s)" + (f", ~{item['est_rows']:,} rows" if item["est_rows"] else ""))

        chunk_q: "queue.Queue" = queue.Queue()
        for i, ch in enumerate(chunks):
            chunk_q.put((i, ch))
        file_q: "queue.Queue" = queue.Queue(maxsize=loaders)   # caps files waiting on disk
        failure: List[Exception] = []
        extracted = [0]
        count_lock = threading.Lock()

        chunk_rows = max(1, item["est_rows"] // len(chunks)) if item["est_rows"] else None

        def extractor():
            box = {"conn": None}
            slot = prog.slot("read")
            try:
                while not failure:
                    try:
                        i, ch = chunk_q.get_nowait()
                    except queue.Empty:
                        return
                    path = os.path.join(work_dir, f"part_{i}.csv.gz")
                    cond = []
                    if ch["start"] is not None:
                        cond.append(f"ctid >= '({ch['start']},0)'::tid")
                    if ch["end"] is not None:
                        cond.append(f"ctid < '({ch['end']},0)'::tid")
                    sql = (f"COPY (SELECT {select_list} FROM {pq(ch['schema'])}.{pq(ch['table'])}"
                           + (f" WHERE {' AND '.join(cond)}" if cond else "") + f") TO STDOUT WITH ({PG_CSV_OUT})")

                    def read():
                        slot.start(f"chunk {i + 1}", "reading", est_rows=chunk_rows)
                        if box["conn"] is None or box["conn"].closed:
                            box["conn"] = _pg(body.source_database, body.pg_conn_id)
                            _track(job, "pg", box["conn"])
                        try:
                            with box["conn"].cursor() as c:
                                c.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
                                c.execute("SET TRANSACTION SNAPSHOT %s", (snapshot,))
                                with gzip.open(path, "wb", compresslevel=1) as gz, \
                                        io.BufferedWriter(gz, 1024 * 1024) as out:
                                    c.copy_expert(sql, _CountingWriter(out, slot))
                                rows = c.rowcount
                                c.execute("ROLLBACK")
                            return rows
                        except Exception:
                            _close(box["conn"])
                            raise
                    t0 = time.time()
                    rows = _retry(job, f"Reading chunk {i + 1}", read)
                    slot.idle()
                    prog.fetched_file(f"chunk {i + 1}", os.path.getsize(path), time.time() - t0, rows)
                    with count_lock:
                        extracted[0] += rows
                    file_q.put((i, path, rows))
            except Exception as e:
                failure.append(e)
            finally:
                if box["conn"] is not None:
                    _untrack(job, box["conn"])
                _close(box["conn"])

        def loader():
            conn = None
            slot = prog.slot("load")
            try:
                while True:
                    got = file_q.get()
                    if got is None:
                        return
                    i, path, rows = got
                    try:
                        if failure:
                            continue
                        if conn is None:
                            conn = _sf(sfqr.WRITE_PROFILE, body.target_database)
                            _track(job, "sf", conn)
                        fname = os.path.basename(path)

                        size = os.path.getsize(path)

                        def load():
                            c = conn.cursor()
                            slot.start(f"chunk {i + 1}", "uploading")
                            c.execute(f"PUT '{_file_uri(path)}' '{stage}' AUTO_COMPRESS = FALSE "
                                      f"SOURCE_COMPRESSION = GZIP OVERWRITE = TRUE PARALLEL = 1")
                            slot.step = "COPY INTO"
                            # A retry after a COPY that did land is skipped by Snowflake's
                            # load history, so a file is never loaded twice.
                            c.execute(copy_sql.replace("{file}", fname))
                            res = c.fetchall()
                            names = [d[0].lower() for d in c.description]
                            idx = names.index("rows_loaded") if "rows_loaded" in names else None
                            return sum(int(r[idx] or 0) for r in res) if idx is not None else rows
                        t0 = time.time()
                        loaded = _retry(job, f"Loading chunk {i + 1}", load)
                        _add(job, n, rows_copied=loaded, chunks_done=1)
                        slot.idle()
                        prog.loaded_file(f"chunk {i + 1}", loaded, time.time() - t0, size)
                    except Exception as e:
                        failure.append(e)
                    finally:
                        try:
                            os.remove(path)
                        except OSError:
                            pass
            finally:
                if conn is not None:
                    _untrack(job, conn)
                _close(conn)

        prog = _Progress(job, item, len(chunks), "read", "loaded into Snowflake", total_rows=item["est_rows"])
        ex = [threading.Thread(target=extractor, daemon=True) for _ in range(min(readers, len(chunks)))]
        ld = [threading.Thread(target=loader, daemon=True) for _ in range(loaders)]
        for t in ex + ld:
            t.start()
        for t in ex:
            t.join()
        for _ in ld:
            file_q.put(None)
        for t in ld:
            t.join()
        prog.stop()
        _log(job, "info", "Copy finished - " + prog.summary(final=True))
        _check(job)
        if failure:
            raise failure[0]

        _set(job, n, phase="verifying")
        scur.execute(f"SELECT COUNT(*) FROM {stg_fq}")
        loaded_rows = scur.fetchone()[0]
        if loaded_rows != extracted[0]:
            raise _DataError(f"Row count check failed: read {extracted[0]:,} from Postgres, Snowflake has "
                             f"{loaded_rows:,}. Target not changed.")
        _log(job, "info", f"{item['name']}: row count check passed - {loaded_rows:,} read, {loaded_rows:,} in staging")
        _check(job)

        _set(job, n, phase="finalizing")
        col_list = ", ".join(sq(c) for c, _ in load_cols)
        t0 = time.time()
        if not tgt_cols:
            scur.execute(f"ALTER TABLE {stg_fq} RENAME TO {tgt_fq}")
            done = f"created table {body.target_schema}.{item['target']}"
        elif body.if_exists == "overwrite":
            scur.execute(f"INSERT OVERWRITE INTO {tgt_fq} ({col_list}) SELECT {col_list} FROM {stg_fq}")
            done = f"replaced the rows of {body.target_schema}.{item['target']}"
        else:
            scur.execute(f"INSERT INTO {tgt_fq} ({col_list}) SELECT {col_list} FROM {stg_fq}")
            done = f"appended to {body.target_schema}.{item['target']}"
        _log(job, "info", f"{item['name']}: {done} in {_dur(time.time() - t0)}")
        return loaded_rows
    finally:
        if "prog" in locals():
            prog.stop()
        _untrack(job, control)
        _close(control)                     # ends the snapshot transaction
        try:
            c = sf.cursor()
            c.execute(f"DROP TABLE IF EXISTS {stg_fq}")     # gone already after a rename
            c.execute(f"REMOVE {stage}")
        except Exception as e:
            _log(job, "warn", f"Cleanup in Snowflake failed ({e}) - drop {staging} by hand if it is there")
        _untrack(job, sf)
        _close(sf)


# ----------------------------------------------------------------------------
# Snowflake -> Postgres
# ----------------------------------------------------------------------------

def _sf_to_pg(job: Dict[str, Any], item: Dict[str, Any], work_dir: str) -> int:
    body: ShuttleRequest = job["body"]
    n, readers, loaders = item["n"], item["source_workers"], item["target_workers"]
    os.makedirs(work_dir, exist_ok=True)
    stage = f"@~/{STAGE_ROOT}/{job['job_id']}/{n}/"
    staging = _staging_name(item["target"], job["job_id"], n, upper=False)
    src_fq = f"{sq(body.source_database)}.{sq(body.source_schema)}.{sq(item['name'])}"
    tgt_fq = f"{pq(body.target_schema)}.{pq(item['target'])}"
    stg_fq = f"{pq(body.target_schema)}.{pq(staging)}"

    sf = _sf(sfqr.READ_PROFILE, body.source_database)
    _track(job, "sf", sf)
    try:
        pg = _pg(body.target_database, body.pg_conn_id)
    except Exception:
        _untrack(job, sf)
        _close(sf)
        raise
    _track(job, "pg", pg)
    finalized = False
    try:
        scur = sf.cursor()
        cols = _sf_columns(scur, body.source_database, body.source_schema, item["name"])
        with pg.cursor() as c:
            tgt_cols = _pg_columns(c, body.target_schema, item["target"])
        if tgt_cols and body.if_exists == "fail":
            raise _DataError(f"{item['target']} appeared in Postgres after the preview")

        if tgt_cols:
            matched = _match_columns(cols, tgt_cols, f"Target {item['target']}")
            load_names = [m["name"] for m in matched]
            with pg.cursor() as c:
                # UNLOGGED: this table only lives until the final INSERT, so it skips the WAL.
                c.execute(f"CREATE UNLOGGED TABLE {stg_fq} (LIKE {tgt_fq} INCLUDING DEFAULTS)")
        else:
            # Integer columns declared NUMBER(38,0) (every INT in Snowflake) become
            # bigint when the data fits. MIN/MAX on a table is metadata, so it's instant.
            wide = [c for c in cols if c["data_type"] == "NUMBER" and (c["scale"] or 0) == 0 and (c["precision"] or 38) > 18]
            if wide and item["kind"] == "table":
                scur.execute("SELECT " + ", ".join(
                    f"MIN({sq(c['name'])}) >= -9223372036854775808 AND MAX({sq(c['name'])}) <= 9223372036854775807"
                    for c in wide) + f" FROM {src_fq}")
                for c, ok in zip(wide, scur.fetchone()):
                    c["fits_bigint"] = ok is not False
            load_names = [pg_name_for(c["name"]) for c in cols]
            with pg.cursor() as c:
                c.execute(f"CREATE TABLE {stg_fq} (" + ", ".join(
                    f"{pq(name)} {pg_type_for(col)}{' NOT NULL' if col['not_null'] else ''}"
                    for name, col in zip(load_names, cols)) + ")")

        _set(job, n, phase="unloading")
        _log(job, "info", f"{item['name']}: Snowflake is unloading ~{item['est_rows']:,} rows")
        t_unload = time.time()
        scur.execute(
            f"COPY INTO {stage}part FROM (SELECT {', '.join(sf_select_expr(c) for c in cols)} FROM {src_fq}) "
            f"FILE_FORMAT = ({SF_CSV}) HEADER = FALSE MAX_FILE_SIZE = {UNLOAD_FILE_BYTES} "
            f"OVERWRITE = TRUE DETAILED_OUTPUT = TRUE"
        )
        out = scur.fetchall()
        names = [d[0].lower() for d in scur.description]
        row_idx = next((names.index(k) for k in ("row_count", "rows_unloaded") if k in names), None)
        if row_idx is None and out:
            raise _DataError(f"Snowflake unload returned no row count (columns: {', '.join(names)})")
        total = sum(int(r[row_idx] or 0) for r in out) if row_idx is not None else 0
        # Rows per file, so a half-loaded file counts as half its rows.
        file_rows = ({os.path.basename(r[names.index("file_name")]): int(r[row_idx] or 0) for r in out}
                     if "file_name" in names and row_idx is not None else {})
        scur.execute(f"LIST {stage}")
        listed = scur.fetchall()
        files = [r[0] for r in listed]
        sizes = {os.path.basename(r[0]): int(r[1] or 0) if len(r) > 1 else 0 for r in listed}
        _set(job, n, est_rows=total, chunks_total=len(files), phase="copying")
        _log(job, "info", f"{item['name']}: Snowflake unloaded {total:,} rows into {len(files)} file(s), "
                          f"{_mb(sum(sizes.values()))} compressed, in {_dur(time.time() - t_unload)}")
        _log(job, "info", f"{item['name']}: downloading with {min(readers, max(1, len(files)))} worker(s), "
                          f"loading into Postgres with {min(loaders, max(1, len(files)))}")
        _check(job)

        copy_sql = f"COPY {stg_fq} ({', '.join(pq(x) for x in load_names)}) FROM STDIN WITH ({PG_CSV_IN})"
        todo: "queue.Queue" = queue.Queue()
        for f in files:
            todo.put(f)
        ready: "queue.Queue" = queue.Queue(maxsize=loaders)    # caps files waiting on disk
        failure: List[Exception] = []

        def downloader():
            conn = None
            slot = prog.slot("fetch")
            # Each worker downloads into its own folder, so the folder's size is
            # how far its file has got. The file moves up when it is complete.
            slot.watch = tempfile.mkdtemp(prefix="dl_", dir=work_dir)
            try:
                while not failure:
                    try:
                        f = todo.get_nowait()
                    except queue.Empty:
                        return

                    def get():
                        nonlocal conn
                        if conn is None:
                            conn = _sf(sfqr.READ_PROFILE)
                            _track(job, "sf", conn)
                        # LIST names are relative to the stage root (data_shuttle/...).
                        conn.cursor().execute(f"GET '@~/{f}' '{_file_uri(slot.watch)}/' PARALLEL = 1")
                        done = os.path.join(work_dir, os.path.basename(f))
                        os.replace(os.path.join(slot.watch, os.path.basename(f)), done)
                        return done
                    t0 = time.time()
                    slot.start(os.path.basename(f), "downloading", total=sizes.get(os.path.basename(f)))
                    local = _retry(job, f"Downloading {os.path.basename(f)}", get)
                    slot.idle()
                    prog.fetched_file(os.path.basename(f), os.path.getsize(local), time.time() - t0)
                    ready.put(local)
            except Exception as e:
                failure.append(e)
            finally:
                if conn is not None:
                    _untrack(job, conn)
                _close(conn)

        def open_loader_conn():
            conn = _pg(body.target_database, body.pg_conn_id)
            with conn.cursor() as c:
                c.execute("SET synchronous_commit = off")
            return conn

        # Every Postgres connection is opened before the first download starts.
        # Once downloads fill the network a new connection can time out, and a
        # bad login should fail here, not after an hour of downloading.
        n_loaders = min(loaders, max(1, len(files)))
        loader_conns = []
        try:
            for _ in range(n_loaders):
                loader_conns.append(_retry(job, "Connecting to Postgres", open_loader_conn))
                _track(job, "pg", loader_conns[-1])
        except Exception:
            for c in loader_conns:
                _untrack(job, c)
                _close(c)
            raise

        def loader(conn):
            slot = prog.slot("load")
            try:
                while True:
                    local = ready.get()
                    if local is None:
                        return
                    try:
                        if failure:
                            continue

                        def load():
                            nonlocal conn
                            if conn.closed:
                                _untrack(job, conn)
                                conn = open_loader_conn()
                                _track(job, "pg", conn)
                            slot.start(os.path.basename(local), "loading", total=os.path.getsize(local))
                            try:
                                with conn.cursor() as c, open(local, "rb") as raw, \
                                        gzip.GzipFile(fileobj=_CountingReader(raw, slot)) as gz:
                                    c.copy_expert(copy_sql, gz, size=1024 * 1024)
                                    return c.rowcount
                            except Exception:
                                _close(conn)
                                raise
                        t0 = time.time()
                        size = os.path.getsize(local)
                        rows = _retry(job, f"Loading {os.path.basename(local)}", load)
                        slot.idle()
                        _add(job, n, rows_copied=rows, chunks_done=1)
                        prog.loaded_file(os.path.basename(local), rows, time.time() - t0, size)
                    except Exception as e:
                        failure.append(e)
                    finally:
                        try:
                            os.remove(local)
                        except OSError:
                            pass
            finally:
                if conn is not None:
                    _untrack(job, conn)
                _close(conn)

        prog = _Progress(job, item, len(files), "downloaded", "loaded into Postgres",
                         total_bytes=sum(sizes.values()) or None, total_rows=total, file_rows=file_rows)
        dl = [threading.Thread(target=downloader, daemon=True) for _ in range(min(readers, max(1, len(files))))]
        ld = [threading.Thread(target=loader, args=(c,), daemon=True) for c in loader_conns]
        for t in dl + ld:
            t.start()
        for t in dl:
            t.join()
        for _ in ld:
            ready.put(None)
        for t in ld:
            t.join()
        prog.stop()
        _log(job, "info", "Copy finished - " + prog.summary(final=True))
        _check(job)
        if failure:
            raise failure[0]

        _set(job, n, phase="verifying")
        # The main connection sat idle for the whole load; start clean.
        _untrack(job, pg)
        _close(pg)
        pg = _retry(job, "Reconnecting to Postgres", lambda: _pg(body.target_database, body.pg_conn_id))
        _track(job, "pg", pg)
        with pg.cursor() as c:
            c.execute(f"SELECT count(*) FROM {stg_fq}")
            loaded = c.fetchone()[0]
        if loaded != total:
            raise _DataError(f"Row count check failed: Snowflake unloaded {total:,}, Postgres has {loaded:,}. "
                             f"Target not changed.")
        _log(job, "info", f"{item['name']}: row count check passed - {total:,} unloaded, {loaded:,} in staging")
        _check(job)

        _set(job, n, phase="finalizing")
        cols_sql = ", ".join(pq(x) for x in load_names)
        pg.autocommit = False
        t0 = time.time()
        with pg.cursor() as c:
            if not tgt_cols:
                c.execute(f"ALTER TABLE {stg_fq} RENAME TO {pq(item['target'])}")
                done = f"created table {body.target_schema}.{item['target']}"
            else:
                if body.if_exists == "overwrite":
                    c.execute(f"TRUNCATE {tgt_fq}")
                # Keeps the source's values in a GENERATED ALWAYS identity column
                # instead of failing on it (Postgres 10+, a no-op without one).
                override = " OVERRIDING SYSTEM VALUE" if pg.server_version >= 100000 else ""
                c.execute(f"INSERT INTO {tgt_fq} ({cols_sql}){override} SELECT {cols_sql} FROM {stg_fq}")
                c.execute(f"DROP TABLE {stg_fq}")
                done = (f"replaced the rows of" if body.if_exists == "overwrite" else "appended to") \
                    + f" {body.target_schema}.{item['target']}"
        pg.commit()
        finalized = True
        _log(job, "info", f"{item['name']}: {done} in {_dur(time.time() - t0)}")
        pg.autocommit = True
        t0 = time.time()
        # The data is committed by now. A failed ANALYZE (permissions, a
        # dropped connection) must not mark the table failed.
        try:
            with pg.cursor() as c:
                c.execute(f"ANALYZE {tgt_fq}")
            _log(job, "info", f"{item['name']}: refreshed Postgres statistics (ANALYZE) in {_dur(time.time() - t0)}")
        except Exception as e:
            _log(job, "warn", f"{item['name']}: copied, but ANALYZE failed ({e}) - run it by hand")
        return loaded
    finally:
        if "prog" in locals():
            prog.stop()
        try:
            if not finalized:
                if pg.closed:                       # dropped mid-copy - the staging table still needs to go
                    _untrack(job, pg)
                    pg = _pg(body.target_database, body.pg_conn_id)
                    _track(job, "pg", pg)
                pg.rollback()
                pg.autocommit = True
                with pg.cursor() as c:
                    c.execute(f"DROP TABLE IF EXISTS {stg_fq}")
        except Exception as e:
            _log(job, "warn", f"Cleanup in Postgres failed ({e}) - drop {staging} by hand if it is there")
        try:
            sf.cursor().execute(f"REMOVE {stage}")
        except Exception as e:
            _log(job, "warn", f"Could not clear {stage} ({e})")
        for conn in (pg, sf):
            _untrack(job, conn)
            _close(conn)



# ============================================================================
# History: every job is saved to the portal database (shuttle_jobs).
#
# A save never waits on the database and is never lost. Each one is first
# written to a local file (the "pending" folder under work_dir), then a
# background thread writes it to the database and deletes the file. If the
# database cannot be reached it keeps trying: 3 quick attempts, then every 30
# minutes 3 times, then every hour - across backend restarts too, since the
# files are picked up again on start. The page shows an alert while anything
# is waiting, with Retry now and Abort saving.
# ============================================================================

HEARTBEAT_SECONDS = 60
INTERRUPTED_AFTER = 5 * 60          # a 'running' row with no heartbeat for this long was cut off


def _retry_delay(attempts: int) -> int:
    """Seconds to wait before the next attempt, given how many have failed."""
    if attempts < 3:
        return (0, 15, 60)[attempts]
    if attempts < 6:
        return 30 * 60
    return 60 * 60


def _host() -> str:
    import socket
    return f"{socket.gethostname()}:{os.getpid()}"


def _snapshot(job: Dict[str, Any], with_logs: bool) -> Dict[str, Any]:
    with job["lock"]:
        tables = [{k: t.get(k) for k in ("name", "target", "status", "rows_copied", "est_rows", "chunks_total",
                                         "chunks_done", "stages", "error")} for t in job["tables"].values()]
        logs = list(job["logs"]) if with_logs else None
    return {
        "id": job["job_id"], "user_email": job["owner"], "direction": job["direction"],
        "copy_type": job["copy_type"], "if_exists": job["if_exists"],
        "source": job["source"], "target": job["target"], "status": job["status"],
        "rows_copied": sum(t["rows_copied"] or 0 for t in tables), "tables": tables, "logs": logs,
        "host": _host(), "started_at": job["started_at"], "finished_at": job["finished_at"],
    }


def _db_upsert(snap: Dict[str, Any]):
    import db
    with db.transaction(autocommit=True) as conn:
        conn.execute(
            """INSERT INTO shuttle_jobs (id, user_email, direction, copy_type, if_exists, source, target, status,
                                         rows_copied, tables, logs, host, started_at, finished_at, heartbeat_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?::json, ?::json, ?, to_timestamp(?), to_timestamp(?), now())
               ON CONFLICT (id) DO UPDATE SET
                   status = EXCLUDED.status, rows_copied = EXCLUDED.rows_copied, tables = EXCLUDED.tables,
                   logs = COALESCE(EXCLUDED.logs, shuttle_jobs.logs), host = EXCLUDED.host,
                   finished_at = EXCLUDED.finished_at, heartbeat_at = now()""",
            (snap["id"], snap["user_email"], snap["direction"], snap["copy_type"], snap["if_exists"],
             snap["source"], snap["target"], snap["status"], snap["rows_copied"],
             json.dumps(snap["tables"], default=str),
             json.dumps(snap["logs"], default=str) if snap["logs"] is not None else None,
             snap["host"], snap["started_at"], snap["finished_at"]),
        )


def _db_heartbeat(job_id: str):
    import db
    with db.transaction(autocommit=True) as conn:
        conn.execute("UPDATE shuttle_jobs SET heartbeat_at = now() WHERE id = ? AND status = 'running'", (job_id,))


class _HistorySaver:
    def __init__(self):
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def folder(self) -> str:
        path = os.path.join(_work_root(), "history_pending")
        os.makedirs(path, exist_ok=True)
        return path

    def _path(self, job_id: str) -> str:
        return os.path.join(self.folder(), f"{job_id}.json")

    def _read(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _write(self, path: str, entry: Dict[str, Any]):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entry, f, default=str)
        os.replace(tmp, path)

    def start(self):
        if self.thread is None:
            # Files left by a backend that stopped mid-job: that job is not
            # running any more, whatever the file says.
            for name in os.listdir(self.folder()):
                if name.endswith(".json"):
                    path = os.path.join(self.folder(), name)
                    entry = self._read(path)
                    snap = (entry or {}).get("snap") or {}
                    if snap.get("status") == "running" and snap.get("id") not in _jobs:
                        entry["snap"]["status"] = "interrupted"
                        self._write(path, entry)
            self.thread = threading.Thread(target=self._loop, daemon=True, name="shuttle-history")
            self.thread.start()

    def save(self, job: Dict[str, Any], with_logs: bool = False):
        """Queue the job's current picture. Local file first, database after.
        Never raises: a full disk loses this history picture, not the copy."""
        try:
            snap = _snapshot(job, with_logs)
            with self.lock:
                path = self._path(snap["id"])
                entry = self._read(path) or {"attempts": 0, "next_at": 0, "error": None}
                if (entry.get("snap") or {}).get("logs") and not snap["logs"]:
                    snap["logs"] = entry["snap"]["logs"]
                entry["snap"] = snap
                entry["version"] = entry.get("version", 0) + 1
                self._write(path, entry)
        except Exception:
            logger.exception("Data Shuttle history for %s could not be queued", job["job_id"][:8])
            return
        self.wake.set()

    def pending(self) -> List[Dict[str, Any]]:
        out = []
        for name in sorted(os.listdir(self.folder())):
            if name.endswith(".json"):
                entry = self._read(os.path.join(self.folder(), name))
                if entry and entry.get("snap") and entry.get("attempts", 0) > 0:      # not just queued a moment ago
                    snap = entry["snap"]
                    out.append({"job_id": snap["id"], "user_email": snap["user_email"], "source": snap["source"],
                                "target": snap["target"], "status": snap["status"], "direction": snap.get("direction"),
                                "rows_copied": snap.get("rows_copied"), "started_at": snap.get("started_at"),
                                "finished_at": snap.get("finished_at"), "tables_total": len(snap.get("tables") or []),
                                "attempts": entry["attempts"],
                                "next_at": entry["next_at"], "error": entry.get("error")})
        return out

    def retry_now(self, job_id: Optional[str] = None):
        with self.lock:
            for name in os.listdir(self.folder()):
                if name.endswith(".json") and (job_id is None or name == f"{job_id}.json"):
                    path = os.path.join(self.folder(), name)
                    entry = self._read(path)
                    if entry:
                        entry["next_at"] = 0
                        self._write(path, entry)
        self.wake.set()

    def abort(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            path = self._path(job_id)
            entry = self._read(path)
            if entry:
                os.remove(path)
            return entry

    def _attempt(self, path: str):
        with self.lock:
            entry = self._read(path)
        if not entry or not entry.get("snap") or entry.get("next_at", 0) > time.time():
            return
        try:
            _db_upsert(entry["snap"])
        except Exception as e:
            with self.lock:
                latest = self._read(path)
                if latest is None:          # aborted meanwhile
                    return
                latest["attempts"] = latest.get("attempts", 0) + 1
                latest["next_at"] = time.time() + _retry_delay(latest["attempts"])
                latest["error"] = str(e)[:500]
                self._write(path, latest)
            logger.warning("Data Shuttle history for %s not saved (attempt %d, next in %s): %s",
                           entry["snap"]["id"][:8], latest["attempts"], _dur(_retry_delay(latest["attempts"])), e)
            return
        with self.lock:
            latest = self._read(path)
            # A newer picture may have been queued while this one was saving.
            if latest and latest.get("version") == entry.get("version"):
                os.remove(path)
                if entry.get("attempts"):
                    logger.info("Data Shuttle history for %s saved after %d failed attempt(s)",
                                entry["snap"]["id"][:8], entry["attempts"])

    def _loop(self):
        while True:
            self.wake.wait(timeout=10)
            self.wake.clear()
            try:
                names = sorted(os.listdir(self.folder()))
            except Exception:
                logger.exception("Data Shuttle history saver")
                continue
            for name in names:
                if name.endswith(".json"):
                    # One bad file must not hold up every file after it.
                    try:
                        self._attempt(os.path.join(self.folder(), name))
                    except Exception:
                        logger.exception("Data Shuttle history saver: %s", name)


_history = _HistorySaver()


def _heartbeat_loop(job: Dict[str, Any]):
    """Tells the history the job is alive, so a restart shows as interrupted.
    Best effort, nothing queued: the next real save carries the state anyway."""
    while not job["done"].wait(HEARTBEAT_SECONDS):
        try:
            _db_heartbeat(job["job_id"])
        except Exception:
            pass


# ============================================================================
# Endpoints
# ============================================================================

def _payload(job: Dict[str, Any], since_log: int = 0) -> Dict[str, Any]:
    with job["lock"]:
        tables = [dict(t, stages=dict(t["stages"])) for t in job["tables"].values()]
        logs = list(job["logs"])
    progs = dict(job["_progress"])
    for n, t in zip(job["tables"].keys(), tables):
        at = t.pop("phase_at", None)
        t["phase_secs"] = int(time.time() - at) if at and t["status"] == "running" else None
        if n in progs and t["status"] == "running":
            t["live"] = progs[n].live()
            t["rows_copied"] = max(t["rows_copied"], t["live"]["rows"])
    rows = sum(t["rows_copied"] for t in tables)
    est = sum(t["est_rows"] for t in tables)
    if job["status"] == "success":
        percent = 100.0
    else:
        # Each table weighs by its rows (1 if unknown); done tables count in full.
        def pct(t):
            if t["status"] == "success":
                return 100.0
            if t.get("live"):
                return t["live"]["percent"]
            return 0.0
        weights = [max(t["est_rows"], 1) for t in tables]
        percent = round(min(99.9, sum(pct(t) * w for t, w in zip(tables, weights)) / sum(weights)), 1) if tables else None
    return {
        "job_id": job["job_id"], "status": job["status"], "direction": job["direction"],
        "copy_type": job["copy_type"], "if_exists": job["if_exists"],
        "source": job["source"], "target": job["target"],
        "tables_total": len(tables),
        "tables_done": sum(t["status"] in ("success", "failed") for t in tables),
        "tables_failed": sum(t["status"] == "failed" for t in tables),
        "rows_copied": rows, "est_rows_total": est, "percent": percent,
        "elapsed_ms": int(((job["finished_at"] or time.time()) - job["started_at"]) * 1000),
        "tables": tables, "logs": logs[since_log:], "log_count": len(logs),
    }


@shuttle_router.get("/health")
def health(auth: dict = Depends(require_permission(PERM))):
    out: Dict[str, Any] = {"snowflake": None, "postgres": None}
    try:
        r, w = sfqr._profile_config(sfqr.READ_PROFILE), sfqr._profile_config(sfqr.WRITE_PROFILE)
        out["snowflake"] = {"read_as": f"{r['user']}/{r.get('role') or ''}", "write_as": f"{w['user']}/{w.get('role') or ''}",
                            "allow_writes": r["allow_writes"]}
    except HTTPException as e:
        out["snowflake_error"] = e.detail
    try:
        out["postgres"] = {"connections": pgs._connections()}
    except HTTPException as e:
        out["postgres_error"] = e.detail
    try:
        out["free_disk_gb"] = round(shutil.disk_usage(_work_root()).free / 1024 ** 3, 1)
    except OSError as e:
        out["free_disk_gb"] = None
        out["disk_error"] = f"Staging folder not usable: {e}"
    out["max_workers"] = {"postgres": _max_workers("pg"), "snowflake": _max_workers("sf")}
    with _budget.cond:
        out["connections"] = {"postgres": {"in_use": _budget.used["pg"], "max": _budget.cap["pg"]},
                              "snowflake": {"in_use": _budget.used["sf"], "max": _budget.cap["sf"]}}
    return out


@shuttle_router.get("/catalog")
def catalog(
    engine: str = Query(..., pattern="^(snowflake|postgres)$"),
    side: str = Query("source", pattern="^(source|target)$"),
    database: Optional[str] = Query(None),
    schema: Optional[str] = Query(None),
    conn_id: Optional[str] = Query(None),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission(PERM)),
):
    """Databases, schemas or tables, through the same cached catalog calls the
    Snowflake and Postgres panes use. Snowflake targets are browsed with the
    write login, since that is who will own them."""
    if engine == "snowflake":
        profile = sfqr.WRITE_PROFILE if side == "target" else sfqr.READ_PROFILE
        kw = dict(bypass_cache=bypass_cache, profile=profile, conn_id=None, role=None, auth=auth)
        if not database:
            return sfqr.list_databases(**kw)
        if not schema:
            return sfqr.list_schemas(database=database, **kw)
        return sfqr.list_tables(database=database, schema=schema, **kw)
    kw = dict(bypass_cache=bypass_cache, conn_id=conn_id, auth=auth)
    if not database:
        return pgs.list_databases(**kw)
    if not schema:
        return pgs.list_schemas(database=database, **kw)
    return pgs.list_tables(database=database, schema=schema, **kw)


@shuttle_router.post("/start")
def start(body: ShuttleRequest, auth: dict = Depends(require_permission(PERM))):
    _cleanup_jobs()
    _validate(body)
    try:
        plan = _build_plan(body)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not plan the copy: {e}")
    if not body.confirm:
        with _jobs_lock:
            busy = _busy_targets(_target_keys(body, plan))
        if busy:
            plan["warnings"] = plan["warnings"] + [f"Another copy is writing to {', '.join(busy[:5])} right now - "
                                                   f"starting will be refused until it ends"]
        return {"requires_confirmation": True, **plan}

    min_free = float(_config.get("min_free_disk_gb", 5))
    try:
        free = shutil.disk_usage(_work_root()).free / 1024 ** 3
    except OSError as e:
        raise HTTPException(status_code=507, detail=f"The staging folder on the portal server is not usable: {e}")
    if free < min_free:
        raise HTTPException(status_code=507, detail=f"Only {free:.1f} GB free for staging files on the portal "
                                                    f"server, {min_free:g} GB needed. Free some space or lower "
                                                    f"the workers.")
    if _job_pool is None:
        raise HTTPException(status_code=503, detail="Data Shuttle is not initialised")
    job = _new_job(body, plan, auth["user"]["email"])
    _log(job, "info", "Queued")
    _job_pool.submit(_run_job, job["job_id"])
    return {"job_id": job["job_id"], "status": "running", "tables_total": len(plan["items"])}


@shuttle_router.get("/jobs/{job_id}/status")
def status(job_id: str, wait_ms: int = Query(0, ge=0, le=15000), since_log: int = Query(0, ge=0),
           auth: dict = Depends(require_permission(PERM))):
    job = _get_job(job_id, auth["user"]["email"])
    if wait_ms and job["status"] == "running":
        job["done"].wait(wait_ms / 1000.0)
    return _payload(job, since_log)


@shuttle_router.get("/jobs/{job_id}/stream")
def stream(job_id: str, since_log: int = Query(0, ge=0), auth: dict = Depends(require_permission(PERM))):
    job = _get_job(job_id, auth["user"]["email"])

    def sse(event, data):
        return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"

    def events():
        cursor, last, beat = since_log, 0.0, time.time()
        while True:
            job["pulse"].clear()
            p = _payload(job, cursor)
            now = time.time()
            if p["logs"]:
                yield sse("logs", {"logs": p["logs"], "log_count": p["log_count"]})
                cursor, beat = p["log_count"], now
            if now - last >= 0.5 or p["status"] != "running":
                yield sse("progress", {k: v for k, v in p.items() if k != "logs"})
                last = beat = now
            if p["status"] != "running":
                yield sse("done", {"status": p["status"]})
                return
            if now - beat >= 15:
                yield ": keep-alive\n\n"
                beat = now
            job["pulse"].wait(1.0)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@shuttle_router.post("/jobs/{job_id}/cancel")
def cancel(job_id: str, auth: dict = Depends(require_permission(PERM))):
    job = _get_job(job_id, auth["user"]["email"])
    if job["status"] != "running":
        return {"job_id": job_id, "status": job["status"]}
    _cancel_job(job, auth["user"]["email"])
    return {"job_id": job_id, "status": "cancelling"}


def _cancel_job(job: Dict[str, Any], who: str):
    """Flag the job and stop whatever statement each of its connections is
    running, so a long COPY does not have to finish first."""
    job["cancel_requested"] = True
    with job["lock"]:
        active = list(job["active"])
    sf_ids = []
    for kind, conn in active:
        try:
            if kind == "pg":
                conn.cancel()
            elif getattr(conn, "session_id", None):
                sf_ids.append(conn.session_id)
        except Exception:
            pass
    if sf_ids:
        try:
            c = _sf(sfqr.WRITE_PROFILE if job["direction"] == "pg_to_sf" else sfqr.READ_PROFILE)
            for sid in sf_ids:
                try:
                    c.cursor().execute("SELECT SYSTEM$CANCEL_ALL_QUERIES(?)", (sid,))
                except Exception:
                    pass
            _close(c)
        except Exception:
            logger.warning("Shuttle %s: could not cancel Snowflake queries", job["job_id"][:8], exc_info=True)
    _log(job, "warn", f"Cancel requested by {who} - stopping and cleaning up. Tables already done stay done, "
                      f"the one in progress is left as it was")


@shuttle_router.get("/jobs")
def list_jobs(auth: dict = Depends(require_permission(PERM))):
    owner = (auth["user"]["email"] or "").strip().lower()
    with _jobs_lock:
        jobs = sorted((j for j in _jobs.values() if j["owner"] == owner), key=lambda j: -j["created_at"])
    return {"jobs": [{"job_id": j["job_id"], "status": j["status"], "source": j["source"], "target": j["target"],
                      "created_at": j["created_at"]} for j in jobs[:20]]}


def _row_view(r: Dict[str, Any]) -> Dict[str, Any]:
    status = r["status"]
    if status == "running" and r["id"] not in _jobs and time.time() - (r["heartbeat_ts"] or 0) > INTERRUPTED_AFTER:
        status = "interrupted"
    return {"job_id": r["id"], "status": status, "direction": r["direction"], "source": r["source"],
            "target": r["target"], "rows_copied": int(r["rows_copied"] or 0), "started_at": r["started_ts"],
            "finished_at": r["finished_ts"], "tables_total": r.get("tables_total"), "saved": True}


@shuttle_router.get("/history")
def history(limit: int = Query(30, ge=1, le=200), auth: dict = Depends(require_permission(PERM))):
    """Your copies, newest first: running ones from memory, the rest from the
    database, plus any still waiting to be saved. Works with the database down."""
    owner = (auth["user"]["email"] or "").strip().lower()
    out: Dict[str, Dict[str, Any]] = {}
    db_error = None
    try:
        import db
        with db.transaction(read_only=True) as conn:
            rows = conn.execute(
                """SELECT id, status, direction, source, target, rows_copied,
                          EXTRACT(EPOCH FROM started_at) AS started_ts, EXTRACT(EPOCH FROM finished_at) AS finished_ts,
                          EXTRACT(EPOCH FROM heartbeat_at) AS heartbeat_ts, json_array_length(tables) AS tables_total
                   FROM shuttle_jobs WHERE user_email = ? ORDER BY started_at DESC LIMIT ?""",
                (owner, limit),
            ).fetchall()
        for r in rows:
            r = dict(r)
            for k in ("started_ts", "finished_ts", "heartbeat_ts"):
                r[k] = float(r[k]) if r[k] is not None else None
            out[r["id"]] = _row_view(r)
    except Exception as e:
        db_error = str(e)[:300]
    for entry in _history.pending():
        if entry["user_email"] == owner:
            # The pending picture is newer than any database row for the same job.
            out[entry["job_id"]] = {"job_id": entry["job_id"], "status": entry["status"], "direction": entry["direction"],
                                    "source": entry["source"], "target": entry["target"],
                                    "rows_copied": entry["rows_copied"], "started_at": entry["started_at"],
                                    "finished_at": entry["finished_at"], "tables_total": entry["tables_total"], "saved": False}
    with _jobs_lock:
        live = [j for j in _jobs.values() if j["owner"] == owner]
    for j in live:
        p = _payload(j, since_log=10 ** 9)
        # Saved means no picture is still waiting in the pending folder - true
        # even while the database is down for this read.
        out[j["job_id"]] = {**out.get(j["job_id"], {}), "saved": not os.path.exists(_history._path(j["job_id"])),
                            "job_id": j["job_id"], "status": j["status"],
                            "direction": j["direction"], "source": j["source"], "target": j["target"],
                            "rows_copied": p["rows_copied"], "started_at": j["started_at"],
                            "finished_at": j["finished_at"], "tables_total": p["tables_total"], "live": True}
    items = sorted(out.values(), key=lambda x: -(x.get("started_at") or time.time()))[:limit]
    return {"jobs": items, "db_error": db_error}


@shuttle_router.get("/history/{job_id}")
def history_detail(job_id: str, auth: dict = Depends(require_permission(PERM))):
    """One past copy with its tables and activity log, for reading back."""
    _valid_id(job_id)
    owner = (auth["user"]["email"] or "").strip().lower()
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job and job["owner"] == owner:
        return {**_payload(job), "live": True, "saved": not os.path.exists(_history._path(job_id))}
    entry = _history._read(_history._path(job_id))
    snap = entry["snap"] if entry and (entry.get("snap") or {}).get("user_email") == owner else None
    if snap is None:
        entry = None                    # someone else's pending file says nothing about this user's row
    if snap is None:
        import db
        try:
            with db.transaction(read_only=True) as conn:
                rows = conn.execute(
                    """SELECT id, user_email, status, direction, copy_type, if_exists, source, target, rows_copied,
                              tables, logs, EXTRACT(EPOCH FROM started_at) AS started_ts,
                              EXTRACT(EPOCH FROM finished_at) AS finished_ts, EXTRACT(EPOCH FROM heartbeat_at) AS heartbeat_ts
                       FROM shuttle_jobs WHERE id = ? AND user_email = ?""",
                    (job_id, owner),
                ).fetchall()
        except Exception as e:
            logger.warning("Data Shuttle history read failed: %s", e)
            raise HTTPException(status_code=503, detail="The portal database could not be reached - try again shortly")
        if not rows:
            raise HTTPException(status_code=404, detail="No such copy in your history")
        r = dict(rows[0])
        for k in ("started_ts", "finished_ts", "heartbeat_ts"):
            r[k] = float(r[k]) if r[k] is not None else None
        tables = r["tables"] if isinstance(r["tables"], list) else json.loads(r["tables"] or "[]")
        logs = r["logs"] if isinstance(r["logs"], list) or r["logs"] is None else json.loads(r["logs"])
        snap = {**r, "status": _row_view(r)["status"], "tables": tables, "logs": logs,
                "started_at": r["started_ts"], "finished_at": r["finished_ts"]}
    tables = snap["tables"] or []
    return {
        "job_id": snap["id"], "status": snap["status"], "direction": snap["direction"],
        "copy_type": snap.get("copy_type"), "if_exists": snap.get("if_exists"),
        "source": snap["source"], "target": snap["target"],
        "tables": [{**t, "phase": t.get("status"), "est_rows": t.get("est_rows") or t.get("rows_copied") or 0,
                    "rows_copied": t.get("rows_copied") or 0} for t in tables],
        "tables_total": len(tables), "tables_done": sum(t.get("status") in ("success", "failed") for t in tables),
        "rows_copied": int(snap.get("rows_copied") or 0),
        "est_rows_total": sum(t.get("est_rows") or 0 for t in tables),
        # Past copies: the share of tables that landed (no live picture to go by).
        "percent": 100.0 if snap["status"] == "success" else
                   (round(100.0 * sum(t.get("status") == "success" for t in tables) / len(tables), 1) if tables else 0.0),
        "elapsed_ms": int(((snap.get("finished_at") or snap.get("started_at") or 0) - (snap.get("started_at") or 0)) * 1000),
        "logs": snap.get("logs") or [], "log_count": len(snap.get("logs") or []),
        "saved": entry is None, "live": False,
    }


@shuttle_router.get("/history-pending")
def history_pending(auth: dict = Depends(require_permission(PERM))):
    """Copies whose history could not be written to the database yet."""
    owner = (auth["user"]["email"] or "").strip().lower()
    items = _history.pending()
    mine = [i for i in items if i["user_email"] == owner]
    return {"pending": mine, "others": len(items) - len(mine), "now": time.time()}


@shuttle_router.post("/history-pending/{job_id}/retry")
def history_pending_retry(job_id: str, auth: dict = Depends(require_permission(PERM))):
    _valid_id(job_id)
    owner = (auth["user"]["email"] or "").strip().lower()
    entry = _history._read(_history._path(job_id))
    if not entry or (entry.get("snap") or {}).get("user_email") != owner:
        raise HTTPException(status_code=404, detail="Nothing waiting to be saved for that copy")
    _history.retry_now(job_id)
    return {"job_id": job_id, "status": "retrying"}


@shuttle_router.delete("/history-pending/{job_id}")
def history_pending_abort(job_id: str, auth: dict = Depends(require_permission(PERM))):
    """Stop trying to save this copy's history. The copy itself is not affected."""
    _valid_id(job_id)
    owner = (auth["user"]["email"] or "").strip().lower()
    with _jobs_lock:
        live = _jobs.get(job_id)
    if live and live["status"] == "running":
        raise HTTPException(status_code=409, detail="That copy is still running - its history can only be dropped once it ends")
    entry = _history._read(_history._path(job_id))
    if not entry or (entry.get("snap") or {}).get("user_email") != owner:
        raise HTTPException(status_code=404, detail="Nothing waiting to be saved for that copy")
    _history.abort(job_id)
    logger.warning("Data Shuttle history for %s abandoned by %s after %d attempt(s)",
                   job_id[:8], owner, entry.get("attempts", 0))
    try:
        from audit import write_audit_event
        write_audit_event("data_shuttle_history_aborted", owner, entry["snap"]["target"],
                          f"History save abandoned after {entry.get('attempts', 0)} attempt(s): "
                          f"{entry['snap']['source']} -> {entry['snap']['target']} ({entry['snap']['status']})")
    except Exception:
        pass
    return {"job_id": job_id, "status": "aborted"}


def shutdown():
    """Cancel running copies for real, not just flag them: the job pool's
    threads are joined at exit, so a flagged copy in a long COPY would hold the
    shutdown up for as long as that statement runs."""
    with _jobs_lock:
        running = [j for j in _jobs.values() if j["status"] == "running"]
    for job in running:
        try:
            _cancel_job(job, "server shutdown")
        except Exception:
            job["cancel_requested"] = True
    if _job_pool is not None:
        _job_pool.shutdown(wait=False, cancel_futures=True)