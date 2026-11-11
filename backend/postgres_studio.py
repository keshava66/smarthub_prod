"""Data Studio, Postgres pane.

Same API shape as snowflake_query_runner.py (the frontend pane is shared), but
talking to the server in the 'postgres_query_runner' config section.

Snowflake keeps every result for RESULT_SCAN. Postgres doesn't, so a SELECT is
run once into a temp table on the worksheet's session and every page, sort,
filter and download reads that table. See "Sessions and query jobs" below.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

import db
import postgres_query_runner as pgqr
from auth import require_permission
from snowflake_query_runner import (
    ALL_OPS, EXPORT_FORMATS, ROWS_PAGE_MAX_SIZE, SET_OPS, TEXT_OPS,
    ColumnFilter, MutateRequest, RowsRequest, SavedQueryRequest, TableRowsRequest,
    _DuplicateRow, _MutationMismatch, _statement_result, confirm_detail, failed_at, numbered_sections, plan_statements, _cache_drop_prefix, _cache_get, _cache_set,
    _change_summary, _dedupe_columns, _key_columns_for, _paged_inline, _parse_json_param,
    _stream_rows, build_order_by, qualified, quote_ident,
)

logger = logging.getLogger(__name__)

pg_studio_router = APIRouter(prefix="/api/pgstudio", tags=["Data Studio - Postgres"])
PERM = "runPostgresStudio"

DEFAULT_STATEMENT_TIMEOUT_SECONDS = 300
MAX_STATEMENT_TIMEOUT_SECONDS = 3600
JOB_TTL_SECONDS = 45 * 60
INLINE_MAX_ROWS = 100_000            # EXPLAIN / SHOW / RETURNING are read into memory, capped here
DOWNLOAD_CHUNK_ROWS = 10_000
HISTORY_KEEP_PER_USER = 500
RN = "__sh_rn"                       # hidden row number that keeps the original result order

_NOT_COMPARABLE = {"json", "xml", "point", "line", "lseg", "box", "path", "polygon", "circle", "geometry", "geography"}


# ============================================================================
# Config and connections. Host, user and password come from the existing
# 'postgres_query_runner' section; these keys are optional extras on it.
# ============================================================================

def _truthy(value) -> bool:
    return value not in (False, "false", "False", 0, "0")


def _cfg(conn_id: Optional[str] = None) -> Dict[str, Any]:
    """Settings for one connection. Numbered keys (host_1, user_1, password_1,
    default_database_1, label_1...) each make a connection; keys without a
    number are shared by all of them. No numbered users means the one login
    the section always had, called "default"."""
    cfg = pgqr._require_config()
    v = pgqr._cfg_value
    cfg["allow_writes"] = _truthy(v("allow_writes", "PGQR_ALLOW_WRITES", True))
    cfg["statement_timeout_seconds"] = int(v("statement_timeout_seconds", "PGQR_STATEMENT_TIMEOUT", DEFAULT_STATEMENT_TIMEOUT_SECONDS))
    cfg["max_statement_timeout_seconds"] = int(v("max_statement_timeout_seconds", "PGQR_MAX_STATEMENT_TIMEOUT", MAX_STATEMENT_TIMEOUT_SECONDS))
    cfg["default_schema"] = v("default_schema", "PGQR_SCHEMA", "public")

    numbered = numbered_sections(pgqr._config)
    if not numbered:
        if conn_id not in (None, "", "default"):
            raise HTTPException(status_code=400, detail=f"Unknown connection '{conn_id}'")
        cfg["conn_id"], cfg["label"] = "default", v("label", "PGQR_LABEL") or f"{cfg['user']}@{cfg['host']}"
        return cfg
    if conn_id in (None, ""):
        conn_id = next(iter(numbered))
    if conn_id not in numbered:
        raise HTTPException(status_code=400, detail=f"Unknown connection '{conn_id}'")
    n = numbered[conn_id]
    for key in ("host", "user", "password", "sslmode", "default_schema"):
        if key in n:
            cfg[key] = n[key]
    for key, target in (("port", "port"), ("connect_timeout_seconds", "connect_timeout"),
                        ("statement_timeout_seconds", "statement_timeout_seconds"),
                        ("max_statement_timeout_seconds", "max_statement_timeout_seconds")):
        if key in n:
            cfg[target] = int(n[key])
    if "allow_writes" in n:
        cfg["allow_writes"] = _truthy(n["allow_writes"])
    cfg["default_database"] = n.get("default_database") or n.get("maintenance_database") or cfg["default_database"]
    cfg["conn_id"], cfg["label"] = conn_id, n.get("label") or f"{cfg['user']}@{cfg['host']}"
    return cfg


def _connections() -> List[Dict[str, Any]]:
    """What the connection dropdown lists. Never includes passwords."""
    ids = list(numbered_sections(pgqr._config)) or ["default"]
    out = []
    for cid in ids:
        c = _cfg(cid)
        out.append({"id": cid, "label": c["label"], "user": c["user"], "host": c["host"],
                    "default_database": c["default_database"], "allow_writes": c["allow_writes"]})
    return out


def _connect(database: Optional[str] = None, conn_id: Optional[str] = None):
    import psycopg2

    cfg = _cfg(conn_id)
    try:
        conn = psycopg2.connect(
            host=cfg["host"], port=cfg["port"], user=cfg["user"], password=cfg["password"],
            dbname=database or cfg["default_database"], sslmode=cfg["sslmode"],
            connect_timeout=cfg["connect_timeout"], application_name="SmartHub_DataStudio",
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not connect to Postgres: {e}")
    conn.autocommit = True
    return conn


def _quiet_close(conn):
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _is_conn_error(e: Exception) -> bool:
    import psycopg2
    return isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError))


class _Pool:
    """Warm autocommit connections per database for catalog calls, table pages
    and grid saves. Query jobs get their own connection (they hold a temp table)."""
    MAX_AGE = 30 * 60
    MAX_IDLE = 4

    def __init__(self):
        self._idle: Dict[str, List[tuple]] = {}
        self._lock = threading.Lock()

    def acquire(self, database, conn_id=None):
        key = (conn_id or "", database or "")
        while True:
            with self._lock:
                bucket = self._idle.get(key)
                if not bucket:
                    break
                conn, created = bucket.pop()
            if conn.closed or time.time() - created > self.MAX_AGE:
                _quiet_close(conn)
                continue
            return conn, key, created
        return _connect(database, conn_id), key, time.time()

    def release(self, conn, key, created, ok=True):
        if not ok or conn.closed or time.time() - created > self.MAX_AGE:
            _quiet_close(conn)
            return
        try:
            if not conn.autocommit:
                conn.rollback()
                conn.autocommit = True
        except Exception:
            _quiet_close(conn)
            return
        with self._lock:
            bucket = self._idle.setdefault(key, [])
            if len(bucket) < self.MAX_IDLE:
                bucket.append((conn, created))
                return
        _quiet_close(conn)


_pool = _Pool()


@contextmanager
def _pooled(database=None, conn_id=None):
    conn, key, created = _pool.acquire(database, conn_id)
    ok = True
    try:
        yield conn
    except Exception as e:
        ok = not _is_conn_error(e)
        raise
    finally:
        _pool.release(conn, key, created, ok)


def _meta(database: Optional[str], fn, conn_id: Optional[str] = None):
    """Catalog read on a pooled connection, retried once on a dead one."""
    for attempt in (0, 1):
        try:
            with _pooled(database, conn_id) as conn, conn.cursor() as cur:
                return fn(cur)
        except Exception as e:
            if attempt == 0 and _is_conn_error(e):
                continue
            raise


def _ps(sql: str) -> str:
    """'?' placeholders (what the grid preview shows) to psycopg2's %s.
    Quoted text is left alone apart from doubling %."""
    out, quote = [], None
    for ch in sql:
        if quote:
            out.append("%%" if ch == "%" else ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
    return "".join(out)


def _safe(v):
    return pgqr._to_json_safe(v)


def _http_error(e: Exception, what: str) -> HTTPException:
    if isinstance(e, HTTPException):
        return e
    if "permission denied" in str(e).lower():
        return HTTPException(status_code=403, detail=f"This login has no access to {what}.")
    return HTTPException(status_code=500, detail=f"Failed to read {what}: {e}")


# ============================================================================
# Filters (same contract as the Snowflake pane, Postgres SQL)
# ============================================================================

def build_where(filters: Optional[List[ColumnFilter]], valid_columns: Optional[List[str]] = None):
    if not filters:
        return "", []
    valid = set(valid_columns) if valid_columns else None
    clauses: List[str] = []
    params: List[Any] = []
    for f in filters:
        op = (f.op or "").lower()
        if op not in ALL_OPS:
            raise HTTPException(status_code=400, detail=f"Unsupported filter operator '{f.op}'")
        if valid is not None and f.col not in valid:
            raise HTTPException(status_code=400, detail=f"'{f.col}' is not a column of this result")
        col = quote_ident(f.col)
        text = f"CAST({col} AS text)"
        if op == "is_null":
            clauses.append(f"{col} IS NULL")
        elif op == "is_not_null":
            clauses.append(f"{col} IS NOT NULL")
        elif op in TEXT_OPS:
            # Backslash is LIKE's default escape, so a typed % or _ stays literal.
            p = ("" if f.value is None else str(f.value)).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = {"contains": f"%{p}%", "not_contains": f"%{p}%", "starts": f"{p}%", "ends": f"%{p}"}[op]
            if op == "not_contains":
                clauses.append(f"({text} NOT ILIKE ? OR {col} IS NULL)")
            else:
                clauses.append(f"{text} ILIKE ?")
            params.append(pattern)
        elif op in SET_OPS:
            values = f.values if f.values is not None else ([f.value] if f.value is not None else [])
            if not values:
                raise HTTPException(status_code=400, detail=f"Filter on '{f.col}' needs at least one value")
            if len(values) > 1000:
                raise HTTPException(status_code=400, detail="An IN filter is limited to 1000 values")
            neg = "NOT " if op == "not_in" else ""
            clauses.append(f"{text} {neg}IN ({', '.join('?' for _ in values)})")
            params.extend("" if v is None else str(v) for v in values)
        else:
            if op in ("eq", "neq") and f.value is None:
                clauses.append(f"{col} IS {'' if op == 'eq' else 'NOT '}NULL")
                continue
            sql_op = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
            clauses.append(f"{text} {sql_op} ?")
            params.append(str(f.value))
    return (" WHERE " + " AND ".join(clauses), params) if clauses else ("", [])


# ============================================================================
# Catalog
# ============================================================================

def _column_row(name, data_type, nullable, default, position, comment, pk_cols) -> Dict[str, Any]:
    base = (data_type or "").split("(")[0].strip().lower()
    return {
        "name": name,
        "data_type": data_type,          # format_type() output, used as-is in CAST(? AS ...)
        "display_type": data_type,
        "nullable": nullable,
        "default": default,
        "position": position,
        "char_length": None, "precision": None, "scale": None,
        "comment": comment,
        "is_primary_key": name in pk_cols,
        "editable": True,
        "comparable": base not in _NOT_COMPARABLE,
    }


def _fetch_columns(database: str, schema: str, table: str, bypass_cache: bool = False,
                   conn_id: Optional[str] = None) -> List[Dict[str, Any]]:
    key = f"pg-colmeta:{conn_id}:{database}:{schema}:{table}"
    if not bypass_cache:
        cached = _cache_get(key)
        if cached is not None:
            return cached

    def read(cur):
        cur.execute(
            """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid), a.attnum, col_description(c.oid, a.attnum)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
            LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
            WHERE n.nspname = %s AND c.relname = %s
            ORDER BY a.attnum
            """,
            (schema, table),
        )
        raw = cur.fetchall()
        if not raw:
            raise HTTPException(status_code=404, detail=f"{schema}.{table} was not found in {database}")
        cur.execute(
            """
            SELECT a.attname
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
            WHERE i.indrelid = %s::regclass AND i.indisprimary
            ORDER BY array_position(i.indkey::int2[], a.attnum)
            """,
            (qualified(None, schema, table),),
        )
        return raw, [r[0] for r in cur.fetchall()]

    raw, pk = _meta(database, read, conn_id)
    columns = [_column_row(*row, pk_cols=pk) for row in raw]
    _cache_set(key, columns)
    return columns


def _object_ddl(database: str, schema: str, name: str, conn_id: Optional[str] = None) -> str:
    reg = qualified(None, schema, name)

    def read(cur):
        cur.execute("SELECT relkind FROM pg_class WHERE oid = %s::regclass", (reg,))
        kind = cur.fetchone()[0]
        if kind in ("v", "m"):
            cur.execute("SELECT pg_get_viewdef(%s::regclass, true)", (reg,))
            head = "CREATE MATERIALIZED VIEW" if kind == "m" else "CREATE OR REPLACE VIEW"
            return f"{head} {reg} AS\n{cur.fetchone()[0]}"
        cur.execute(
            """
            SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, pg_get_expr(d.adbin, d.adrelid)
            FROM pg_attribute a LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
            WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
            """,
            (reg,),
        )
        lines = [
            f"    {quote_ident(n)} {t}{f' DEFAULT {dflt}' if dflt else ''}{' NOT NULL' if nn else ''}"
            for n, t, nn, dflt in cur.fetchall()
        ]
        cur.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %s::regclass ORDER BY contype, conname",
            (reg,),
        )
        lines += [f"    CONSTRAINT {quote_ident(c)} {d}" for c, d in cur.fetchall()]
        out = [f"CREATE TABLE {reg} (\n" + ",\n".join(lines) + "\n);"]
        cur.execute(
            """
            SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i
            WHERE i.indrelid = %s::regclass
              AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)
            """,
            (reg,),
        )
        out += [f"{r[0]};" for r in cur.fetchall()]
        cur.execute("SELECT obj_description(%s::regclass, 'pg_class')", (reg,))
        comment = cur.fetchone()[0]
        if comment:
            out.append(f"COMMENT ON TABLE {reg} IS '{comment.replace(chr(39), chr(39) * 2)}';")
        return "\n\n".join(out)

    return _meta(database, read, conn_id)


# ============================================================================
# Sessions and query jobs
#
# Each worksheet gets its own connection (a session), kept until the worksheet
# is closed or sits idle for SESSION_IDLE_SECONDS. So SET work_mem, temp
# tables, search_path, an open BEGIN... carry over between runs, like DBeaver.
# A run's result goes into a temp table on that session; the next run in the
# same worksheet drops it.
# ============================================================================

SESSION_IDLE_SECONDS = 20 * 60

_executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="pgstudio")
_slots = threading.Semaphore(pgqr.DEFAULT_MAX_CONCURRENT_JOBS)
_slots_size = pgqr.DEFAULT_MAX_CONCURRENT_JOBS
_slots_lock = threading.Lock()
_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()


class _Cancelled(Exception):
    pass


def _resize_slots(n: int):
    global _slots, _slots_size
    with _slots_lock:
        if n != _slots_size:
            _slots, _slots_size = threading.Semaphore(n), n


def _new_session(key: Optional[str], database: str, conn_id: Optional[str]) -> Dict[str, Any]:
    return {"key": key, "database": database, "conn_id": conn_id, "conn": None, "temp": None,
            "lock": threading.Lock(), "last_used": time.time()}


def _session_for(owner: str, session_id: Optional[str], database: str, conn_id: str) -> Dict[str, Any]:
    """The worksheet's session. Switching the worksheet to another database or
    login starts a new one, since a Postgres connection is tied to both.
    No session_id means a throwaway session, closed when its job is."""
    if not session_id:
        return _new_session(None, database, conn_id)
    key = f"{owner}:{session_id}"
    old = None
    with _sessions_lock:
        s = _sessions.get(key)
        if s and (s["database"], s["conn_id"]) != (database, conn_id):
            old, s = s, None
        if s is None:
            s = _sessions[key] = _new_session(key, database, conn_id)
        s["last_used"] = time.time()
    if old:
        _close_session(old)
    return s


def _close_session(s: Dict[str, Any]):
    with s["lock"]:
        _quiet_close(s["conn"])
        s["conn"], s["temp"] = None, None


def _drop_temp(s: Dict[str, Any]):
    """Caller holds s['lock']."""
    if s["temp"] and s["conn"] is not None and not s["conn"].closed:
        try:
            with s["conn"].cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS pg_temp.{quote_ident(s['temp'])}")
        except Exception:
            pass
    s["temp"] = None


def _get_job(job_id: str, owner: Optional[str] = None) -> Dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job or (owner is not None and job["owner"] != (owner or "").strip().lower()):
        raise HTTPException(status_code=404, detail="Unknown or expired query job")
    job["last_used"] = job["session"]["last_used"] = time.time()
    return job


def _close_job(job: Dict[str, Any]):
    """Throwaway sessions close; a worksheet session only loses this job's result."""
    s = job["session"]
    if s["key"] is None:
        _close_session(s)
        return
    with s["lock"]:
        if job.get("temp_table") and s["temp"] == job["temp_table"]:
            _drop_temp(s)


def cleanup_jobs():
    now = time.time()
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items() if j["created_at"] < now - JOB_TTL_SECONDS]
    for jid in stale:
        with _jobs_lock:
            job = _jobs.pop(jid, None)
        if job and job["status"] != "running":
            _close_job(job)
    with _sessions_lock:
        idle = [k for k, s in _sessions.items()
                if now - s["last_used"] > SESSION_IDLE_SECONDS and not s["lock"].locked()]
        idle_sessions = [_sessions.pop(k) for k in idle]
    for s in idle_sessions:
        _close_session(s)


def _type_names(cur, oids) -> Dict[int, str]:
    oids = sorted({o for o in oids if o})
    if not oids:
        return {}
    cur.execute("SELECT oid, format_type(oid, NULL) FROM pg_type WHERE oid = ANY(%s)", (oids,))
    return {int(o): t for o, t in cur.fetchall()}


def _in_failed_transaction(conn) -> bool:
    import psycopg2.extensions as ext
    return conn.info.transaction_status == ext.TRANSACTION_STATUS_INERROR


def _run_last(job: Dict[str, Any], cur, st: Dict[str, Any]):
    """The statement whose result the grid shows."""
    c, sql = st["cls"], st["sql"]
    use_temp = c.returns_rows and c.cursorable
    if use_temp:
        # Newline before ')' so a trailing -- comment can't eat it.
        cur.execute(f"SELECT * FROM ({sql}\n) AS _q LIMIT 0")
        desc = cur.description
        # A void column (SELECT pg_sleep(1)) can't go in a table, so that result is read inline.
        cur.execute("SELECT count(*) FROM pg_type WHERE oid = ANY(%s) AND typtype = 'p'", ([d[1] for d in desc],))
        use_temp = cur.fetchone()[0] == 0
    if use_temp:
        names = [c2["name"] for c2 in _dedupe_columns([{"name": d[0]} for d in desc])]
        tmp = f"_sh_result_{job['job_id'][:16]}"
        # ponytail: the whole result is copied into a temp table, same as Snowflake keeping it for
        # RESULT_SCAN. A 100M row SELECT costs that much temp space; add a row cap here if it bites.
        cur.execute(
            f"CREATE TEMP TABLE {quote_ident(tmp)} AS "
            f"SELECT row_number() OVER () AS {quote_ident(RN)}, _q.* "
            f"FROM ({sql}\n) AS _q({', '.join(quote_ident(n) for n in names)})"
        )
        job["session"]["temp"] = job["temp_table"] = tmp
        count = cur.rowcount
        if count is None or count < 0:
            cur.execute(f"SELECT COUNT(*) FROM {quote_ident(tmp)}")
            count = cur.fetchone()[0]
        cur.execute(
            "SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = %s::regclass AND attnum > 1 AND NOT attisdropped ORDER BY attnum",
            (f"pg_temp.{quote_ident(tmp)}",),
        )
        job["columns"] = [{"name": n, "type": t, "display_type": t} for n, t in cur.fetchall()]
        job["row_count"] = int(count)
    elif c.returns_rows:
        cur.execute(sql)
        if cur.description is None:
            raise ValueError("The statement returned no result set")
        desc = cur.description
        rows = cur.fetchmany(INLINE_MAX_ROWS)
        types = _type_names(cur, [d[1] for d in desc])
        job["columns"] = _dedupe_columns([{"name": d[0], "type": types.get(d[1], ""), "display_type": types.get(d[1], "")} for d in desc])
        job["inline_rows"] = [[_safe(v) for v in r] for r in rows]
        job["row_count"] = len(job["inline_rows"])
    else:
        cur.execute(sql)
        job["rows_affected"] = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        job["columns"] = []
        job["row_count"] = 0


def _run_job(job_id: str):
    import psycopg2

    job = _get_job(job_id)
    s = job["session"]
    planned = job["statements"]
    k = 0
    with s["lock"]:
        try:
            if s["conn"] is None or s["conn"].closed:
                s["conn"], s["temp"] = _connect(job["database"], s["conn_id"]), None
            conn = job["conn"] = s["conn"]
            _drop_temp(s)   # the worksheet's previous result
            with conn.cursor() as cur:
                if not _in_failed_transaction(conn):
                    # Reset every run so the configured cap holds; a SET earlier in the same run still wins.
                    cur.execute("SELECT set_config('statement_timeout', %s, false)", (str(job["timeout_seconds"] * 1000),))
                for k, st in enumerate(planned):
                    if job["cancel_requested"]:
                        raise _Cancelled()
                    started = time.time()
                    if k < len(planned) - 1:
                        c = st["cls"]
                        if c.returns_rows and c.cursorable:
                            # Counted, not fetched, so a big SELECT mid-script never lands in API memory.
                            cur.execute(f"SELECT count(*) FROM ({st['sql']}\n) AS _q")
                            extra = {"row_count": cur.fetchone()[0]}
                        else:
                            cur.execute(st["sql"])
                            extra = {"rows_affected": max(cur.rowcount or 0, 0)}
                    else:
                        _run_last(job, cur, st)
                        extra = {"row_count": job["row_count"], "rows_affected": job["rows_affected"]}
                    if len(planned) > 1:
                        job["statement_results"].append(_statement_result(st, k, started, "success", **extra))
            job["status"] = "success"
        except _Cancelled:
            job["status"] = "cancelled"
        except HTTPException as e:
            job.update(status="failed", error=str(e.detail))
        except Exception as e:
            if isinstance(e, psycopg2.extensions.QueryCanceledError):
                if job["cancel_requested"]:
                    job["status"] = "cancelled"
                else:
                    job.update(status="failed", error=failed_at(f"Query timed out after {job['timeout_seconds']}s", planned, k))
            else:
                job.update(status="failed", error=failed_at(str(e).strip(), planned, k))
            if s["conn"] is not None and s["conn"].closed:
                s["conn"], s["temp"] = None, None
        finally:
            job["finished_at"] = time.time()
            if s["key"] is None and (not job.get("temp_table") or job["status"] != "success" or job.get("closed")):
                _quiet_close(s["conn"])
                s["conn"] = None
            elif job.get("closed"):
                _drop_temp(s)
    job["done"].set()
    _slots.release()


def _job_status(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "statement_type": job["lead_keyword"],
        "returns_rows": job["returns_rows"],
        "is_destructive": job["is_destructive"],
        "columns": job["columns"],
        "row_count": job["row_count"],
        "rows_affected": job["rows_affected"],
        "query_id": None,
        "error": job["error"],
        "elapsed_ms": int(((job["finished_at"] or time.time()) - job["started_at"]) * 1000),
        "statement_count": len(job["statements"]),
        "statements": job["statement_results"],
    }


def _result_conn(job: Dict[str, Any]):
    """Caller holds the session lock."""
    s = job["session"]
    if s["conn"] is None or s["conn"].closed or s["temp"] != job["temp_table"]:
        raise HTTPException(status_code=410, detail="This result is no longer available (the worksheet ran something newer, "
                                                    "or sat idle too long). Re-run the query.")
    return s["conn"]


def _result_sql(job, sort, filters):
    names = [c["name"] for c in job["columns"]]
    where, params = build_where(filters, names)
    order = build_order_by(sort, names)
    order = f"{order}, {quote_ident(RN)}" if order else f" ORDER BY {quote_ident(RN)}"
    cols = ", ".join(quote_ident(n) for n in names)
    return f"SELECT {cols} FROM {quote_ident(job['temp_table'])}", where, order, params


# ============================================================================
# History / saved queries (pg_query_history, pg_saved_queries)
# ============================================================================

def _db(read_only: bool = False):
    return db.transaction(read_only=read_only, datetime_format=db.utc_naive_iso)


_db_ready = False


def _init_db():
    global _db_ready
    if not _db_ready:
        try:
            db.ensure_ready()
            _db_ready = True
        except Exception as e:
            logger.warning("Data Studio (Postgres) workspace storage not ready yet: %s", e)


def _record_history(email: str, job: Dict[str, Any]):
    if job["status"] == "running" or job.get("_history_written"):
        return
    job["_history_written"] = True
    _init_db()
    if not _db_ready:
        return
    email = (email or "").strip().lower()
    try:
        with _db() as conn:
            conn.execute(
                """INSERT INTO pg_query_history (id, user_email, sql, database, schema, status, elapsed_ms, row_count, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (uuid.uuid4().hex, email, job["sql"][:20000], job.get("database") or "", "", job["status"],
                 int(((job["finished_at"] or time.time()) - job["started_at"]) * 1000),
                 job.get("row_count"), (job.get("error") or "")[:2000], datetime.now(timezone.utc)),
            )
            conn.execute(
                """DELETE FROM pg_query_history h
                   USING (SELECT created_at, id FROM pg_query_history WHERE user_email = ?
                          ORDER BY created_at DESC, id DESC OFFSET ? LIMIT 1) AS edge
                   WHERE h.user_email = ? AND (h.created_at, h.id) <= (edge.created_at, edge.id)""",
                (email, HISTORY_KEEP_PER_USER, email),
            )
    except Exception as e:
        logger.info("Could not record Postgres query history: %s", e)


# ============================================================================
# Endpoints - explorer
# ============================================================================

class PgQueryRequest(BaseModel):
    sql: str
    database: Optional[str] = None
    schema_: Optional[str] = Field(None, alias="schema")
    session_id: Optional[str] = None     # the worksheet id; runs with the same one share a connection
    confirm: bool = False
    timeout_seconds: Optional[int] = None
    wait_ms: int = 2500
    page_size: int = 100

    model_config = {"populate_by_name": True}


@pg_studio_router.get("/health")
def health(conn_id: Optional[str] = Query(None), auth: dict = Depends(require_permission(PERM))):
    cfg = _cfg(conn_id)
    return {
        "configured": True,
        "engine": "postgres",
        "connections": _connections(),
        "conn_id": cfg["conn_id"],
        "account": f"{cfg['host']}:{cfg['port']}",
        "user": cfg["user"],
        "role": cfg["user"],
        "warehouse": None,
        "default_database": cfg["default_database"],
        "default_schema": cfg["default_schema"],
        "allow_writes": cfg["allow_writes"],
        "max_page_size": ROWS_PAGE_MAX_SIZE,
        "write_profile": {"configured": False},
    }


@pg_studio_router.get("/databases")
def list_databases(bypass_cache: bool = Query(False), conn_id: Optional[str] = Query(None),
                   auth: dict = Depends(require_permission(PERM))):
    cfg = _cfg(conn_id)
    key = f"pg-studio-dbs:{cfg['conn_id']}"
    if not bypass_cache and (cached := _cache_get(key)) is not None:
        return {**cached, "cached": True}

    def read(cur):
        cur.execute(
            "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate "
            "AND has_database_privilege(datname, 'CONNECT') ORDER BY datname"
        )
        return [r[0] for r in cur.fetchall()]

    try:
        payload = {"databases": _meta(cfg["default_database"], read, cfg["conn_id"]), "cached": False}
    except Exception as e:
        raise _http_error(e, "databases")
    _cache_set(key, payload)
    return payload


@pg_studio_router.get("/schemas")
def list_schemas(database: str = Query(...), bypass_cache: bool = Query(False), conn_id: Optional[str] = Query(None),
                 auth: dict = Depends(require_permission(PERM))):
    conn_id = _cfg(conn_id)["conn_id"]
    key = f"pg-studio-schemas:{conn_id}:{database}"
    if not bypass_cache and (cached := _cache_get(key)) is not None:
        return {**cached, "cached": True}

    def read(cur):
        cur.execute(
            "SELECT nspname FROM pg_namespace WHERE has_schema_privilege(oid, 'USAGE') "
            "AND nspname NOT LIKE 'pg\\_toast%' AND nspname NOT LIKE 'pg\\_temp\\_%' ORDER BY nspname"
        )
        return [r[0] for r in cur.fetchall()]

    try:
        payload = {"schemas": _meta(database, read, conn_id), "cached": False}
    except Exception as e:
        raise _http_error(e, f"schemas in {database}")
    _cache_set(key, payload)
    return payload


@pg_studio_router.get("/tables")
def list_tables(database: str = Query(...), schema: str = Query(...), bypass_cache: bool = Query(False),
                conn_id: Optional[str] = Query(None), auth: dict = Depends(require_permission(PERM))):
    conn_id = _cfg(conn_id)["conn_id"]
    key = f"pg-studio-tables:{conn_id}:{database}:{schema}"
    if not bypass_cache and (cached := _cache_get(key)) is not None:
        return {**cached, "cached": True}
    kinds = {"r": "table", "p": "table", "v": "view", "m": "materialized_view", "f": "external_table"}

    def read(cur):
        cur.execute(
            """
            SELECT c.relname, c.relkind, c.reltuples::bigint,
                   CASE WHEN c.relkind IN ('r', 'p', 'm') THEN pg_total_relation_size(c.oid) END
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition
            ORDER BY c.relname
            """,
            (schema,),
        )
        return [
            {"name": name, "kind": kinds[kind], "est_rows": est if est is not None and est >= 0 and kind in "rpm" else None,
             "size_bytes": size, "transient": False}
            for name, kind, est, size in cur.fetchall()
        ]

    try:
        payload = {"tables": _meta(database, read, conn_id), "cached": False}
    except Exception as e:
        raise _http_error(e, f"tables in {database}.{schema}")
    _cache_set(key, payload)
    return payload


@pg_studio_router.get("/columns")
def list_columns(database: str = Query(...), schema: str = Query(...), table: str = Query(...),
                 bypass_cache: bool = Query(False), conn_id: Optional[str] = Query(None),
                 auth: dict = Depends(require_permission(PERM))):
    try:
        columns = _fetch_columns(database, schema, table, bypass_cache, _cfg(conn_id)["conn_id"])
    except Exception as e:
        raise _http_error(e, f"columns of {schema}.{table}")
    return {"columns": columns, "primary_key": [c["name"] for c in columns if c["is_primary_key"]], "cached": False}


@pg_studio_router.get("/ddl")
def get_ddl(database: str = Query(...), schema: str = Query(...), name: str = Query(...), kind: str = Query("TABLE"),
            conn_id: Optional[str] = Query(None), auth: dict = Depends(require_permission(PERM))):
    try:
        return {"ddl": _object_ddl(database, schema, name, _cfg(conn_id)["conn_id"])}
    except Exception as e:
        raise _http_error(e, f"the DDL of {schema}.{name}")


# ============================================================================
# Endpoints - query execution
# ============================================================================

@pg_studio_router.post("/query")
def start_query(body: PgQueryRequest, conn_id: Optional[str] = Query(None), auth: dict = Depends(require_permission(PERM))):
    cleanup_jobs()
    cfg = _cfg(conn_id)
    _resize_slots(cfg["max_concurrent_jobs"])

    # Postgres strings don't treat backslash as an escape (E'' strings aside).
    planned = plan_statements(body.sql, pgqr.classify_and_validate_sql, backslash_escapes=False)
    risky = [p for p in planned if p["cls"].is_destructive]
    if risky and not cfg["allow_writes"]:
        raise HTTPException(status_code=403, detail="This connection is configured read-only (allow_writes = false)")
    if risky and not body.confirm:
        raise HTTPException(status_code=409, detail=confirm_detail(planned))
    owner = (auth["user"]["email"] or "").strip().lower()
    database = body.database or cfg["default_database"]
    session = _session_for(owner, body.session_id, database, cfg["conn_id"])
    if session["lock"].locked():
        raise HTTPException(status_code=409, detail="This worksheet is still running something. Cancel it or wait for it to finish.")
    if not _slots.acquire(blocking=False):
        raise HTTPException(status_code=429, detail=f"Too many queries already running (max {cfg['max_concurrent_jobs']} concurrent). Cancel one or wait for it to finish.")

    last = planned[-1]["cls"]
    timeout = max(1, min(int(body.timeout_seconds or cfg["statement_timeout_seconds"]), cfg["max_statement_timeout_seconds"]))
    now = time.time()
    job = {
        "job_id": uuid.uuid4().hex,
        "owner": owner,
        "sql": body.sql.strip() if len(planned) > 1 else last.cleaned_sql,
        "statements": planned,
        "statement_results": [],
        "database": database,
        "session": session,
        "lead_keyword": last.lead_keyword,
        "is_destructive": bool(risky),
        "returns_rows": last.returns_rows,
        "timeout_seconds": timeout,
        "status": "running",
        "conn": None, "temp_table": None, "inline_rows": None,
        "columns": None, "row_count": None, "rows_affected": None, "error": None,
        "created_at": now, "started_at": now, "last_used": now, "finished_at": None,
        "done": threading.Event(), "cancel_requested": False,
    }
    with _jobs_lock:
        _jobs[job["job_id"]] = job
    _executor.submit(_run_job, job["job_id"])

    response = {
        "job_id": job["job_id"], "status": "running", "statement_type": last.lead_keyword,
        "is_destructive": bool(risky), "returns_rows": last.returns_rows,
        "warning": last.warning if len(planned) == 1 else None,
    }
    wait = max(0.0, min(int(body.wait_ms or 0), 15000) / 1000.0)
    if wait and job["done"].wait(wait):
        response.update(_job_status(job))
        _record_history(auth["user"]["email"], job)
        if job["status"] == "success" and job["returns_rows"]:
            try:
                response["first_page"] = get_query_rows(
                    job["job_id"], RowsRequest(page=1, page_size=max(1, min(int(body.page_size or 100), ROWS_PAGE_MAX_SIZE))), auth=auth)
            except HTTPException:
                pass
    return response


@pg_studio_router.get("/query/{job_id}/status")
def get_query_status(job_id: str, wait_ms: int = Query(0, ge=0, le=15000), auth: dict = Depends(require_permission(PERM))):
    job = _get_job(job_id, auth["user"]["email"])
    if wait_ms and job["status"] == "running":
        job["done"].wait(wait_ms / 1000.0)
    _record_history(auth["user"]["email"], job)
    return _job_status(job)


@pg_studio_router.post("/query/{job_id}/rows")
def get_query_rows(job_id: str, body: RowsRequest, auth: dict = Depends(require_permission(PERM))):
    job = _get_job(job_id, auth["user"]["email"])
    if job["status"] != "success":
        raise HTTPException(status_code=409, detail=f"Query is not ready (status: {job['status']})")
    page = max(1, int(body.page))
    size = max(1, min(int(body.page_size), ROWS_PAGE_MAX_SIZE))
    body = body.model_copy(update={"page": page, "page_size": size})
    if not job["returns_rows"]:
        return {"columns": [], "rows": [], "page": 1, "page_size": size, "total_rows": 0, "total_pages": 1}

    if job["inline_rows"] is not None:
        rows, total = _paged_inline(job, body)
    else:
        select, where, order, params = _result_sql(job, body.sort, body.filters)
        with job["session"]["lock"]:
            try:
                with _result_conn(job).cursor() as cur:
                    total = job["row_count"] or 0
                    if where:
                        cur.execute(_ps(f"SELECT COUNT(*) FROM {quote_ident(job['temp_table'])}{where}"), tuple(params))
                        total = int(cur.fetchone()[0])
                    cur.execute(_ps(f"{select}{where}{order} LIMIT {size} OFFSET {(page - 1) * size}"), tuple(params))
                    rows = [[_safe(v) for v in r] for r in cur.fetchall()]
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Failed to fetch rows: {e}")
    return {
        "columns": job["columns"], "rows": rows, "page": page, "page_size": size,
        "total_rows": total, "total_pages": max(1, -(-total // size)) if total else 1,
    }


@pg_studio_router.post("/query/{job_id}/cancel")
def cancel_query(job_id: str, auth: dict = Depends(require_permission(PERM))):
    job = _get_job(job_id, auth["user"]["email"])
    if job["status"] != "running":
        return {"job_id": job_id, "status": job["status"]}
    job["cancel_requested"] = True
    try:
        if job.get("conn") is not None:
            job["conn"].cancel()
    except Exception as e:
        logger.info("Postgres cancel failed for job %s: %s", job_id, e)
    return {"job_id": job_id, "status": "cancelling"}


@pg_studio_router.delete("/query/{job_id}")
def close_query(job_id: str, auth: dict = Depends(require_permission(PERM))):
    job = _get_job(job_id, auth["user"]["email"])
    with _jobs_lock:
        _jobs.pop(job_id, None)
    job["closed"] = True
    if job["status"] != "running":
        _close_job(job)
    return {"job_id": job_id, "status": "closed"}


@pg_studio_router.delete("/session/{session_id}")
def close_session(session_id: str, auth: dict = Depends(require_permission(PERM))):
    """Called when a worksheet is closed. Anything still running in it is cancelled."""
    with _sessions_lock:
        s = _sessions.pop(f"{_email(auth)}:{session_id}", None)
    if s:
        if s["lock"].locked() and s["conn"] is not None:
            try:
                s["conn"].cancel()
            except Exception:
                pass
        _close_session(s)
    return {"session_id": session_id, "closed": bool(s)}


@pg_studio_router.get("/query/{job_id}/download")
def download_query(job_id: str, fmt: str = Query("csv"), sort_json: Optional[str] = Query(None),
                   filters_json: Optional[str] = Query(None), auth: dict = Depends(require_permission(PERM))):
    job = _get_job(job_id, auth["user"]["email"])
    fmt = (fmt or "csv").lower()
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported export format '{fmt}'")
    if job["status"] != "success" or not job["returns_rows"]:
        raise HTTPException(status_code=409, detail="This query has no rows to download")
    sort = _parse_json_param(sort_json, "sort")
    filters = [ColumnFilter(**f) for f in (_parse_json_param(filters_json, "filters") or [])]
    names = [c["name"] for c in job["columns"]]
    base = f"postgres_query_{job_id[:8]}"

    if job["inline_rows"] is not None:
        rows, _ = _paged_inline(job, RowsRequest(page=1, page_size=len(job["inline_rows"]) or 1, sort=sort, filters=filters))
        return _stream_rows(fmt, names, iter([rows]), base, job["columns"])

    select, where, order, params = _result_sql(job, sort, filters)
    with job["session"]["lock"]:
        _result_conn(job)

    def batches():
        import psycopg2.extensions as ext

        with job["session"]["lock"]:
            conn = _result_conn(job)
            name = quote_ident(f"dl_{uuid.uuid4().hex[:12]}")
            # A cursor streams in chunks. It needs a transaction: open one unless the
            # user already has one going in this worksheet, which is left as it was.
            own = conn.info.transaction_status == ext.TRANSACTION_STATUS_IDLE
            with conn.cursor() as cur:
                if own:
                    cur.execute("BEGIN")
                try:
                    cur.execute(_ps(f"DECLARE {name} NO SCROLL CURSOR FOR {select}{where}{order}"), tuple(params))
                    while True:
                        cur.execute(f"FETCH FORWARD {DOWNLOAD_CHUNK_ROWS} FROM {name}")
                        batch = cur.fetchall()
                        if not batch:
                            break
                        yield [[_safe(v) for v in r] for r in batch]
                finally:
                    try:
                        cur.execute(f"CLOSE {name}")
                    except Exception:
                        pass
                    if own:
                        try:
                            cur.execute("COMMIT")
                        except Exception:
                            pass

    return _stream_rows(fmt, names, batches(), base, job["columns"])


# ============================================================================
# Table data editor
# ============================================================================

def _pg_array(values) -> str:
    parts = []
    for x in values:
        if x is None:
            parts.append("NULL")
        elif isinstance(x, list):
            parts.append(_pg_array(x))
        else:
            s = json.dumps(x) if isinstance(x, dict) else ("t" if x is True else "f" if x is False else str(x))
            parts.append('"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return "{" + ",".join(parts) + "}"


def _bind(value: Any, data_type: str = "text"):
    """Everything binds as text and gets CAST to the column type in SQL."""
    if value is None:
        return None
    if data_type.endswith("[]") and isinstance(value, str) and value.strip().startswith("["):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    if isinstance(value, list) and data_type.endswith("[]"):
        return _pg_array(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _where_for_row(original: Dict[str, Any], key_columns: List[str], by_name: Dict[str, Dict[str, Any]]):
    clauses, params = [], []
    for name in key_columns:
        col = by_name.get(name)
        if col is None:
            raise HTTPException(status_code=400, detail=f"Unknown key column '{name}'")
        if name not in original:
            raise HTTPException(status_code=400, detail=f"The row is missing key column '{name}' - refresh and try again")
        ident = quote_ident(name)
        if col["comparable"]:
            clauses.append(f"{ident} IS NOT DISTINCT FROM CAST(? AS {col['data_type']})")
            params.append(_bind(original[name], col["data_type"]))
        else:
            clauses.append(f"CAST({ident} AS text) IS NOT DISTINCT FROM ?")
            params.append(_bind(original[name]))
    if not clauses:
        raise HTTPException(status_code=400, detail="No key columns available to match this row")
    return " AND ".join(clauses), params


def _dup_check(target, row, key_columns, by_name):
    where, params = _where_for_row(row, key_columns, by_name)
    return f"SELECT COUNT(*) FROM {target} WHERE {where}", params


@pg_studio_router.post("/table/rows")
def table_rows(body: TableRowsRequest, conn_id: Optional[str] = Query(None), auth: dict = Depends(require_permission(PERM))):
    cfg = _cfg(conn_id)
    conn_id = cfg["conn_id"]
    page = max(1, int(body.page))
    size = max(1, min(int(body.page_size), ROWS_PAGE_MAX_SIZE))
    columns = _fetch_columns(body.database, body.schema_, body.table, conn_id=conn_id)
    names = [c["name"] for c in columns]
    where, params = build_where(body.filters, names)
    order = build_order_by(body.sort, names)
    target = qualified(None, body.schema_, body.table)

    def count_rows():
        with _pooled(body.database, conn_id) as conn, conn.cursor() as cur:
            cur.execute(_ps(f"SELECT COUNT(*) FROM {target}{where}"), tuple(params))
            return int(cur.fetchone()[0])

    count_future = _executor.submit(count_rows) if body.exact_count else None
    try:
        with _pooled(body.database, conn_id) as conn, conn.cursor() as cur:
            cur.execute(_ps(f"SELECT * FROM {target}{where}{order} LIMIT {size} OFFSET {(page - 1) * size}"), tuple(params))
            rows = [[_safe(v) for v in r] for r in cur.fetchall()]
    except Exception as e:
        if count_future:
            count_future.cancel()
        raise HTTPException(status_code=500, detail=f"Failed to read {body.schema_}.{body.table}: {e}")

    total = None
    if count_future:
        try:
            total = count_future.result()
        except Exception as e:
            logger.info("Row count for %s failed: %s", target, e)
    return {
        "columns": columns,
        "result_columns": [{"name": c["name"], "type": c["data_type"], "display_type": c["display_type"]} for c in columns],
        "rows": rows,
        "page": page, "page_size": size,
        "total_rows": total,
        "total_pages": max(1, -(-total // size)) if total else None,
        "primary_key": [c["name"] for c in columns if c["is_primary_key"]],
        "editable": cfg["allow_writes"],
    }


@pg_studio_router.post("/table/mutate")
def table_mutate(body: MutateRequest, conn_id: Optional[str] = Query(None), auth: dict = Depends(require_permission(PERM))):
    """Grid edits as one transaction; every statement must touch exactly one
    row or the whole batch rolls back."""
    cfg = _cfg(conn_id)
    conn_id = cfg["conn_id"]
    if not cfg["allow_writes"]:
        raise HTTPException(status_code=403, detail="This connection is configured read-only")
    if not body.changes:
        raise HTTPException(status_code=400, detail="No changes to apply")
    if len(body.changes) > 500:
        raise HTTPException(status_code=400, detail="Apply at most 500 row changes at a time")

    columns = _fetch_columns(body.database, body.schema_, body.table, conn_id=conn_id)
    by_name = {c["name"]: c for c in columns}
    key_columns = _key_columns_for(columns, body.key_columns)
    target = qualified(None, body.schema_, body.table)

    def value_sql(name, value):
        if name not in by_name:
            raise HTTPException(status_code=400, detail=f"Unknown column '{name}'")
        if value is None:
            return "NULL", []
        return f"CAST(? AS {by_name[name]['data_type']})", [_bind(value, by_name[name]["data_type"])]

    plan: List[Dict[str, Any]] = []
    for i, ch in enumerate(body.changes):
        kind = (ch.kind or "").lower()
        if kind == "insert":
            values = ch.values or {}
            if not values:
                raise HTTPException(status_code=400, detail=f"Change {i + 1}: an insert needs at least one value")
            parts = [value_sql(n, v) for n, v in values.items()]
            sql = (f"INSERT INTO {target} ({', '.join(quote_ident(n) for n in values)}) "
                   f"VALUES ({', '.join(p[0] for p in parts)})")
            plan.append({"kind": "insert", "sql": sql, "params": [x for p in parts for x in p[1]], "index": i,
                         "dup_check": _dup_check(target, {k: values.get(k) for k in key_columns}, key_columns, by_name)})
        elif kind == "update":
            values, original = ch.values or {}, ch.original or {}
            if not values:
                raise HTTPException(status_code=400, detail=f"Change {i + 1}: an update needs at least one changed value")
            parts = [value_sql(n, v) for n, v in values.items()]
            where, wparams = _where_for_row(original, key_columns, by_name)
            sql = f"UPDATE {target} SET {', '.join(f'{quote_ident(n)} = {p[0]}' for n, p in zip(values, parts))} WHERE {where}"
            after = {k: values.get(k, original.get(k)) for k in key_columns}
            plan.append({"kind": "update", "sql": sql, "params": [x for p in parts for x in p[1]] + wparams, "index": i,
                         "dup_check": _dup_check(target, after, key_columns, by_name)})
        elif kind == "delete":
            where, wparams = _where_for_row(ch.original or {}, key_columns, by_name)
            plan.append({"kind": "delete", "sql": f"DELETE FROM {target} WHERE {where}", "params": wparams, "index": i})
        else:
            raise HTTPException(status_code=400, detail=f"Change {i + 1}: unknown change kind '{ch.kind}'")

    if not body.confirm:
        return {
            "requires_confirmation": True,
            "key_columns": key_columns,
            "statements": [{"kind": p["kind"], "sql": p["sql"], "params": p["params"]} for p in plan],
            "summary": _change_summary(plan),
        }

    applied = []
    try:
        with _pooled(body.database, conn_id) as conn:
            conn.autocommit = False
            try:
                with conn.cursor() as cur:
                    for step in plan:
                        cur.execute(_ps(step["sql"]), tuple(step["params"]))
                        if cur.rowcount != 1:
                            raise _MutationMismatch(step, max(cur.rowcount, 0))
                        if step.get("dup_check"):
                            check_sql, check_params = step["dup_check"]
                            cur.execute(_ps(check_sql), tuple(check_params))
                            if int(cur.fetchone()[0]) > 1:
                                raise _DuplicateRow(step, key_columns)
                        applied.append({"kind": step["kind"], "rows": 1})
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except _DuplicateRow as dr:
        what = "the same primary key as" if any(c["is_primary_key"] for c in columns) else "exactly the same values as"
        raise HTTPException(status_code=409, detail=(
            f"Change {dr.step['index'] + 1} ({dr.step['kind']}) would leave a row with {what} another row. "
            "Nothing was applied. Change at least one value and try again."))
    except _MutationMismatch as mm:
        step = mm.step
        detail = (f"Change {step['index'] + 1} ({step['kind']}) matched no rows - the row was changed or deleted by someone "
                  "else since you loaded it. Nothing was applied; refresh and try again.") if mm.affected == 0 else (
                  f"Change {step['index'] + 1} ({step['kind']}) would have affected {mm.affected} rows instead of 1. "
                  "Nothing was applied. This table has no unique key for these values.")
        raise HTTPException(status_code=409, detail=detail)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Changes were rolled back: {e}")

    _cache_drop_prefix(f"pg-studio-tables:{conn_id}:{body.database}:{body.schema_}")
    try:
        from audit import write_audit_event
        write_audit_event("pgstudio_table_edit", auth["user"]["email"],
                          f"{body.database}.{body.schema_}.{body.table}", _change_summary(plan))
    except Exception:
        pass
    return {"applied": applied, "total": len(applied), "summary": _change_summary(plan)}


# ============================================================================
# Workspace endpoints
# ============================================================================

def _email(auth) -> str:
    return (auth["user"]["email"] or "").strip().lower()


def _check_saved(body: SavedQueryRequest) -> str:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the saved query a name")
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="That name is too long (200 characters max)")
    if not (body.sql or "").strip():
        raise HTTPException(status_code=400, detail="There's no SQL to save")
    return name


@pg_studio_router.get("/history")
def get_history(limit: int = Query(50, ge=1, le=200), auth: dict = Depends(require_permission(PERM))):
    _init_db()
    with _db(read_only=True) as conn:
        rows = conn.execute(
            "SELECT id, sql, database, schema, status, elapsed_ms, row_count, error, created_at "
            "FROM pg_query_history WHERE user_email = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (_email(auth), limit),
        ).fetchall()
    return {"history": [dict(r) for r in rows]}


@pg_studio_router.delete("/history")
def clear_history(auth: dict = Depends(require_permission(PERM))):
    _init_db()
    with _db() as conn:
        conn.execute("DELETE FROM pg_query_history WHERE user_email = ?", (_email(auth),))
    return {"cleared": True}


@pg_studio_router.get("/saved")
def list_saved(auth: dict = Depends(require_permission(PERM))):
    _init_db()
    email = _email(auth)
    with _db(read_only=True) as conn:
        rows = conn.execute(
            "SELECT id, user_email, name, sql, database, schema, shared, created_at, updated_at "
            "FROM pg_saved_queries WHERE user_email = ? OR shared ORDER BY updated_at DESC, id",
            (email,),
        ).fetchall()
    return {"saved": [{**dict(r), "shared": bool(r["shared"]), "owned": r["user_email"] == email} for r in rows]}


@pg_studio_router.post("/saved")
def create_saved(body: SavedQueryRequest, auth: dict = Depends(require_permission(PERM))):
    _init_db()
    name = _check_saved(body)
    now = datetime.now(timezone.utc)
    saved_id = uuid.uuid4().hex
    with _db() as conn:
        conn.execute(
            "INSERT INTO pg_saved_queries (id, user_email, name, sql, database, schema, shared, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (saved_id, _email(auth), name, body.sql[:200000], body.database or "", body.schema_ or "", bool(body.shared), now, now),
        )
    return {"id": saved_id, "name": name, "created_at": db.utc_naive_iso(now)}


@pg_studio_router.put("/saved/{saved_id}")
def update_saved(saved_id: str, body: SavedQueryRequest, auth: dict = Depends(require_permission(PERM))):
    _init_db()
    name = _check_saved(body)
    now = datetime.now(timezone.utc)
    with _db() as conn:
        updated = conn.execute(
            "UPDATE pg_saved_queries SET name = ?, sql = ?, database = ?, schema = ?, shared = ?, updated_at = ? "
            "WHERE id = ? AND user_email = ? RETURNING id",
            (name, body.sql[:200000], body.database or "", body.schema_ or "", bool(body.shared), now, saved_id, _email(auth)),
        ).fetchone()
        if not updated:
            exists = conn.execute("SELECT 1 FROM pg_saved_queries WHERE id = ?", (saved_id,)).fetchone()
            raise HTTPException(status_code=403 if exists else 404,
                                detail="Only the owner can edit this saved query" if exists else "Saved query not found")
    return {"id": saved_id, "updated_at": db.utc_naive_iso(now)}


@pg_studio_router.delete("/saved/{saved_id}")
def delete_saved(saved_id: str, auth: dict = Depends(require_permission(PERM))):
    _init_db()
    with _db() as conn:
        deleted = conn.execute(
            "DELETE FROM pg_saved_queries WHERE id = ? AND user_email = ? RETURNING id", (saved_id, _email(auth))
        ).fetchone()
        if not deleted:
            exists = conn.execute("SELECT 1 FROM pg_saved_queries WHERE id = ?", (saved_id,)).fetchone()
            raise HTTPException(status_code=403 if exists else 404,
                                detail="Only the owner can delete this saved query" if exists else "Saved query not found")
    return {"deleted": True}


def _selfcheck():
    """Asserts for the pure helpers. Run from the backend folder:
    python -c "import postgres_studio as p; p._selfcheck()" """
    assert _ps("SELECT ? FROM \"a?%\" WHERE x LIKE '5%'") == "SELECT %s FROM \"a?%%\" WHERE x LIKE '5%%'"
    w, p = build_where([ColumnFilter(col="n", op="contains", value="50%")], ["n"])
    assert w == ' WHERE CAST("n" AS text) ILIKE ?' and p == ["%50\\%%"], (w, p)
    assert _bind([1, None, "a\"b"], "text[]") == '{"1",NULL,"a\\"b"}'
    assert _bind("[1,2]", "integer[]") == '{"1","2"}'
    print("ok")