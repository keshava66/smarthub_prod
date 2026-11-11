from __future__ import annotations

import configparser
import io
import json
import logging
import os
import queue as _queue
import re
import threading
import time
import uuid
import hashlib
from collections import Counter
from datetime import datetime, date, time as _time
from decimal import Decimal, InvalidOperation
from pathlib import Path
import base64
import tempfile
from typing import Dict, List, Optional

import asyncio
import concurrent.futures
import pytz
import xlsxwriter
from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth import require_permission

from data_validation_connectors import (
    create_connector,
    CONNECTOR_REGISTRY,
    ConnectorError,
    ensure_jdbc_jvm,
    JDBC_CONNECTOR_JAR_SOURCES,
)

logger = logging.getLogger(__name__)
EASTERN = pytz.timezone("America/New_York")

data_validation_router = APIRouter(prefix="/api/data-validation", tags=["Data Validation"])

# Injected once at startup by app.py — used only as a fallback for the Snowflake
# target (its main "snowflake" block) if data_validation_config.ini has no [target].
_CONFIG: Optional[dict] = None

DATA_VALIDATION_CONFIG_PATH = os.environ.get("DATA_VALIDATION_CONFIG_PATH") or str(
    Path(__file__).parent / "data_validation_config.ini"
)

REPORT_CACHE_TTL_SECONDS = 15 * 60  # reports are meant to be downloaded right after a run
_REPORT_SPOOL = Path(os.environ.get("DV_REPORT_SPOOL") or (Path(tempfile.gettempdir()) / "dv_reports"))


def _spool_path(token: str) -> Path:
    try:
        uuid.UUID(str(token))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Report not found or token expired. Please regenerate.")
    return _REPORT_SPOOL / f"{token}.json"


def _purge_expired_reports() -> None:
    now = time.time()
    try:
        entries = list(_REPORT_SPOOL.glob("*.json"))
    except OSError:
        return
    for path in entries:
        try:
            # mtime is set at write time; cheaper than opening every file.
            if now - path.stat().st_mtime > REPORT_CACHE_TTL_SECONDS:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def _store_report(token: str, file_bytes: bytes, owner: str) -> None:
    _REPORT_SPOOL.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_REPORT_SPOOL, 0o700)
    except OSError:
        pass
    payload = json.dumps({"owner": owner, "b64": base64.b64encode(file_bytes).decode("ascii")})
    final = _spool_path(token)
    # Write to a temp name and rename so a reader can never see a half-written
    # file — rename is atomic within a filesystem.
    tmp = final.with_suffix(".part")
    tmp.write_text(payload)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(final)


def _take_report(token: str, owner: str) -> Optional[bytes]:
    """Read and delete a report. Returns None if missing, expired, or not the
    caller's — the endpoint reports all three identically on purpose."""
    path = _spool_path(token)
    try:
        raw = path.read_text()
    except (OSError, ValueError):
        return None
    try:
        entry = json.loads(raw)
    except json.JSONDecodeError:
        path.unlink(missing_ok=True)
        return None
    if entry.get("owner") != owner:
        return None  # someone else's token — leave their file alone
    try:
        data = base64.b64decode(entry["b64"])
    except (KeyError, ValueError, TypeError):
        path.unlink(missing_ok=True)
        return None
    path.unlink(missing_ok=True)  # one-time download
    return data


SOURCE_SYSTEMS = [
    {"id": "insight7", "label": "Insight7"},
    {"id": "rimdocs", "label": "RIMDocs"},
    {"id": "snowflake", "label": "Snowflake"},
    {"id": "postgresql", "label": "PostgreSQL"},
    {"id": "docubridge", "label": "DocuBridge"},
    {"id": "denodo", "label": "Denodo"},
    {"id": "sqlserver", "label": "Lift"},
    {"id": "grail", "label": "Grail"},
    {"id": "databricks", "label": "Databricks"},
]

DEFAULT_SAMPLE_LIMIT = 20_000


# ─────────────────────────────────────────────────────────────────────────────
# Shape of the result payload the frontend echoes back to /prepare-download.
# Before this, prepare_download took a bare `dict` — any malformed or partial
# JSON blob would fall through to a raw KeyError -> 500 inside
# _build_report_xlsx instead of a clean validation error. This doesn't (and
# can't, without a server-side run cache) prove the payload actually came
# from a real /run — a user with viewDataValidation could still hand-craft a
# well-shaped fake result — but it does close the crash and catches obviously
# garbage input. Extra/unknown fields are ignored rather than rejected, so
# this doesn't need to be updated every time _run_comparison's result shape
# grows.
# ─────────────────────────────────────────────────────────────────────────────
class _RowCountIn(BaseModel):
    source: int
    target: int
    match: bool
    difference: Optional[int] = None


class _SchemaComparisonIn(BaseModel):
    match: Optional[bool] = None
    source_columns: Optional[int] = None
    target_columns: Optional[int] = None
    common_columns: Optional[int] = None
    source_only_columns: List[str] = []
    target_only_columns: List[str] = []
    scope_note: Optional[str] = None


class _AggregateChecksIn(BaseModel):
    match: Optional[bool] = None
    skipped: bool = True
    skip_reason: Optional[str] = None
    checked_columns: Optional[int] = None
    numeric_columns: Optional[int] = None
    mismatches: List[dict] = []
    mismatch_count: Optional[int] = None
    row_count_source: Optional[int] = None
    row_count_target: Optional[int] = None


class _DataTypeValidationIn(BaseModel):
    match: Optional[bool] = None
    checked_columns: Optional[int] = None
    mismatched_columns: Optional[int] = None
    major_mismatches: Optional[int] = None
    minor_mismatches: Optional[int] = None
    mismatches: List[dict] = []
    skipped: bool = False
    skip_reason: Optional[str] = None


class _NullChecksIn(BaseModel):
    match: Optional[bool] = None
    columns: List[dict] = []
    mismatched_columns: Optional[int] = None
    skipped: bool = False
    skip_reason: Optional[str] = None


class _DuplicateChecksIn(BaseModel):
    match: Optional[bool] = None
    source_duplicates: Optional[int] = None
    target_duplicates: Optional[int] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    excluded_columns: List[str] = []


class _MissingRecordsIn(BaseModel):
    count: int = 0
    sample: List[dict] = []
    sample_size: Optional[int] = None
    sampled: bool = True
    method: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None
    key_column: Optional[str] = None


class _ExtraRecordsIn(BaseModel):
    count: int = 0
    sample: List[dict] = []
    sample_size: Optional[int] = None
    sampled: bool = True
    method: Optional[str] = None
    skipped: bool = False
    skip_reason: Optional[str] = None


class _DataMismatchesIn(BaseModel):
    count: int = 0
    mismatched_row_count: int = 0
    sample: List[dict] = []
    sampled: bool = True
    not_applicable: bool = False
    skipped: bool = False
    skip_reason: Optional[str] = None


class _AdditionalMetricsIn(BaseModel):
    source_row_count_ms: Optional[int] = None
    target_row_count_ms: Optional[int] = None
    match_percentage: Optional[float] = None


class ValidationResultIn(BaseModel):
    source_system: str
    source_database: Optional[str] = None
    source_schema: Optional[str] = None
    source_table: str
    target_database: Optional[str] = None
    target_schema: Optional[str] = None
    target_table: str
    key_column: Optional[str] = None
    key_column_issue: Optional[str] = None
    key_column_auto: bool = False
    started_at: str
    completed_at: str
    duration_seconds: float
    overall_status: str
    row_count: _RowCountIn
    schema_comparison: _SchemaComparisonIn
    data_type_validation: _DataTypeValidationIn
    null_checks: _NullChecksIn
    duplicate_checks: _DuplicateChecksIn
    aggregate_checks: Optional[_AggregateChecksIn] = None
    missing_records: _MissingRecordsIn
    extra_records: _ExtraRecordsIn
    data_mismatches: _DataMismatchesIn
    additional_metrics: _AdditionalMetricsIn
    issues: List[str] = []


def set_data_validation_dependencies(config: Optional[dict]):
    global _CONFIG
    _CONFIG = config


ENVIRONMENTS = ["dev", "qa", "prod"]
DEFAULT_ENVIRONMENT = "prod"


def _load_data_validation_ini(path: Optional[str] = None) -> dict:
    """Read data_validation_config.ini fresh on every call (cheap — it's a small local
    file) so credential updates take effect without restarting the server.
    Sections are named '<source>' or '<source>.<env>' (env one of dev/qa/prod) —
    e.g. [insight7], [insight7.dev], [insight7.qa], [insight7.prod]. A plain '<source>'
    section (no env suffix) is used as the fallback for any env that doesn't
    have its own section. Same convention applies to '[target]' / '[target.dev]' etc.
    Returns {"sources": {id: {env: {...}}}, "target": {env: {...}}}.
    """
    ini_path = path or DATA_VALIDATION_CONFIG_PATH
    parser = configparser.ConfigParser(interpolation=None)
    if not parser.read(ini_path):
        return {"sources": {}, "target": {}}

    cfg: dict = {"sources": {}, "target": {}}
    for section in parser.sections():
        name = section.strip()
        if "." in name:
            base, env = name.rsplit(".", 1)
            env = env.strip().lower()
        else:
            base, env = name, "default"
        base = base.strip().lower()
        if base == "target":
            cfg["target"][env] = dict(parser[section])
        else:
            cfg["sources"].setdefault(base, {})[env] = dict(parser[section])
    return cfg


def _bootstrap_jdbc_jvm(cfg: dict) -> None:
    sources = cfg.get("sources", {})
    jar_paths = []
    for source_id, (cfg_key, env_var) in JDBC_CONNECTOR_JAR_SOURCES.items():
        envs = sources.get(source_id, {})
        jar = None
        for env_cfg in envs.values():
            jar = env_cfg.get(cfg_key)
            if jar:
                break
        jar = jar or os.environ.get(env_var)
        if jar:
            jar_paths.append(jar)
    ensure_jdbc_jvm(jar_paths)


def _data_validation_cfg() -> dict:
    cfg = _load_data_validation_ini()
    if not cfg.get("sources") and "target" not in cfg:
        raise HTTPException(
            status_code=503,
            detail=f"Data Validation config not found or empty at '{DATA_VALIDATION_CONFIG_PATH}'. "
                   f"Copy data_validation_config.example.ini to data_validation_config.ini and fill in "
                   f"the source systems you use.",
        )
    _bootstrap_jdbc_jvm(cfg)
    return cfg


def _source_cfg(source_system: str, env: str = DEFAULT_ENVIRONMENT) -> dict:
    env = (env or DEFAULT_ENVIRONMENT).strip().lower()
    cfg = _data_validation_cfg()
    sources = cfg.get("sources", {})
    entry = sources.get(source_system)
    if entry:
        if env in entry:
            return entry[env]
        if "default" in entry:
            return entry["default"]
    if source_system == "snowflake":
        # No dedicated [snowflake] source section — fall back to the target
        # Snowflake config. Comparing two databases/schemas within the same
        # account is the common case, so this avoids making users duplicate
        # the same account/user/key/role/warehouse into two sections.
        try:
            return _target_cfg(env)
        except HTTPException:
            pass
    raise HTTPException(
        status_code=400,
        detail=f"No [{source_system}.{env}] (or [{source_system}]) section found in "
               f"{DATA_VALIDATION_CONFIG_PATH}.",
    )


def _target_cfg(env: str = DEFAULT_ENVIRONMENT) -> dict:
    env = (env or DEFAULT_ENVIRONMENT).strip().lower()
    cfg = _load_data_validation_ini()
    target = cfg.get("target", {})
    if env in target:
        return target[env]
    if "default" in target:
        return target["default"]
    # Fall back to the main Snowflake config already used throughout the app.
    if _CONFIG and "snowflake" in _CONFIG:
        return _CONFIG["snowflake"]
    raise HTTPException(
        status_code=503,
        detail=f"No Snowflake target configuration found (checked [target.{env}] / [target] in "
               f"{DATA_VALIDATION_CONFIG_PATH} and dq_config.json's 'snowflake' block).",
    )


def _empty_or_none(v: Optional[str]) -> Optional[str]:
    """Query params arrive as '' rather than missing when a picker is cleared."""
    return v if v else None


# database/schema/table/key_column names get f-string interpolated straight
# into raw SQL in data_validation_connectors.py (e.g. f'"{schema}"."{table}"').
# They come from query params with no other validation, so without this check
# a crafted name (e.g. containing a stray quote) is a straight SQL injection
# into whatever source or the Snowflake target the request is running against.
_IDENTIFIER_RE = re.compile(r'^[A-Za-z0-9_$# .-]+$')


def _validate_identifier(value: Optional[str], label: str) -> Optional[str]:
    if value is None:
        return None
    if not _IDENTIFIER_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {label} '{value}' — only letters, numbers, spaces, and _ $ # . - are allowed.",
        )
    return value


# ─────────────────────────────────────────────────────────────────────────────
# Live connectivity checks — a section existing in the .ini doesn't mean the
# creds actually work, so the source picker only shows sources/environments
# it can actually connect to right now. Cached briefly since these are real
# network round-trips (and JDBC ones boot a JVM the first time).
# ─────────────────────────────────────────────────────────────────────────────
_CONNECTIVITY_CACHE: Dict[str, tuple] = {}  # key -> (expires_at, ok)
CONNECTIVITY_CACHE_TTL = 30  # seconds
CONNECTIVITY_CHECK_DEADLINE = 12  # seconds — max total wait before the picker gives up on slow sources


def _test_connectivity(source_system: str, cfg: dict) -> bool:
    try:
        with create_connector(source_system, cfg):
            pass
        return True
    except Exception as e:
        logger.warning(f"Connectivity check failed for {source_system}: {e}")
        return False


def _cached_connectivity(source_system: str, env_key: str, cfg: dict) -> bool:
    cache_key = f"{source_system}::{env_key}"
    now = time.time()
    cached = _CONNECTIVITY_CACHE.get(cache_key)
    if cached and cached[0] > now:
        return cached[1]
    ok = _test_connectivity(source_system, cfg)
    _CONNECTIVITY_CACHE[cache_key] = (now + CONNECTIVITY_CACHE_TTL, ok)
    return ok


def _connectivity_stale_ok(source_system: str, env_key: str, cfg: dict):
    """Return (value, is_fresh) without ever blocking on a live check.

    The picker used to block up to CONNECTIVITY_CHECK_DEADLINE on every load
    once the 30s TTL lapsed, so the first visit after any idle period sat there
    spinning. Serving the last known answer immediately and refreshing behind
    the scenes keeps the page instant; the result lands in the cache for the
    next call. Returns (None, False) the very first time, when there's nothing
    cached to serve.
    """
    cache_key = f"{source_system}::{env_key}"
    cached = _CONNECTIVITY_CACHE.get(cache_key)
    if cached and cached[0] > time.time():
        return cached[1], True
    if cached:
        _refresh_connectivity_async(source_system, env_key, cfg)
        return cached[1], False  # stale but usable
    return None, False


_CONNECTIVITY_REFRESHING: set = set()
_CONNECTIVITY_LOCK = threading.Lock()


def _refresh_connectivity_async(source_system: str, env_key: str, cfg: dict) -> None:
    cache_key = f"{source_system}::{env_key}"
    with _CONNECTIVITY_LOCK:
        if cache_key in _CONNECTIVITY_REFRESHING:
            return  # one refresh in flight per source is enough
        _CONNECTIVITY_REFRESHING.add(cache_key)

    def go():
        try:
            ok = _test_connectivity(source_system, cfg)
            _CONNECTIVITY_CACHE[cache_key] = (time.time() + CONNECTIVITY_CACHE_TTL, ok)
        except Exception as e:
            logger.warning(f"Background connectivity refresh failed for {cache_key}: {e}")
        finally:
            with _CONNECTIVITY_LOCK:
                _CONNECTIVITY_REFRESHING.discard(cache_key)

    threading.Thread(target=go, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Listing endpoints — sources
# ─────────────────────────────────────────────────────────────────────────────
@data_validation_router.get("/sources")
def get_sources(auth: dict = Depends(require_permission("viewDataValidation"))):
    """Every source system, each flagged 'configured' only if it has a section
    in the config file AND that section actually connects right now — sources
    that fail to connect stay in the list but disabled, not hidden. A source
    only gets an environment dropdown if it has explicit .dev/.qa/.prod
    sections; a plain (env-agnostic) section is offered as a single card."""
    out = {}
    environments = ENVIRONMENTS
    for msg in iter_sources():
        if "sources" in msg:
            environments = msg["environments"]
            out = {s["id"]: s for s in msg["sources"]}
        else:
            out[msg["source"]["id"]] = msg["source"]
    return {"environments": environments, "sources": list(out.values())}


def iter_sources():
    """Same answer as get_sources, but yielded in pieces so the UI can draw it
    as it lands. First message is the full list, with any source that still
    needs a live check marked pending. After that, one {"source": ...} message
    per pending source as soon as all its environments have been checked."""
    try:
        cfg = _data_validation_cfg()
    except HTTPException:
        cfg = {"sources": {}, "target": {}}

    raw_sources = dict(cfg.get("sources", {}))
    # Snowflake usually has no dedicated [snowflake] section — it falls back
    # to [target.*]. Fold that in so it's checked the same way as everything else.
    if "snowflake" not in raw_sources and cfg.get("target"):
        raw_sources["snowflake"] = cfg["target"]

    # Gather every (source, env, cfg) combo that needs a live check, then run
    # them in parallel — sequential JDBC connects (Denodo/SQL Server/Databricks
    # each booting a JVM) would make this endpoint painfully slow otherwise.
    # Anything with a cached answer (fresh or stale) is served instantly; a
    # stale one kicks off a background refresh for next time. Only sources
    # we've genuinely never reached need a blocking check.
    results: Dict[tuple, bool] = {}
    checks = []
    for source_id, envs in raw_sources.items():
        if source_id not in CONNECTOR_REGISTRY:
            continue
        for env_key, env_cfg in envs.items():
            value, _fresh = _connectivity_stale_ok(source_id, env_key, env_cfg)
            if value is None:
                checks.append((source_id, env_key, env_cfg))
            else:
                results[(source_id, env_key)] = value

    # how many env checks each source is still waiting on
    left: Dict[str, int] = {}
    for source_id, _env, _cfg in checks:
        left[source_id] = left.get(source_id, 0) + 1

    def build(s):
        source_id = s["id"]
        envs = raw_sources.get(source_id, {})
        working = [env_key for env_key in envs if results.get((source_id, env_key))]
        plain_only = envs and set(envs.keys()) == {"default"}
        row = {
            **s,
            "configured": bool(working),
            "environments": [] if (plain_only or not working) else [e for e in ENVIRONMENTS if e in working],
            "levels": CONNECTOR_REGISTRY[source_id].LEVELS,
        }
        if left.get(source_id):
            row["pending"] = True
        return row

    by_id = {s["id"]: s for s in SOURCE_SYSTEMS}
    yield {"environments": ENVIRONMENTS, "sources": [build(s) for s in SOURCE_SYSTEMS]}

    if not checks:
        return

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(checks), 8))
    futures = {
        pool.submit(_cached_connectivity, source_id, env_key, env_cfg): (source_id, env_key)
        for source_id, env_key, env_cfg in checks
    }
    try:
        # Hard deadline — a single unreachable host must not hang the whole
        # source picker. Anything still connecting after the deadline is
        # treated as down for this round (and briefly cached as such so the
        # next call doesn't re-block on it either).
        try:
            for future in concurrent.futures.as_completed(futures, timeout=CONNECTIVITY_CHECK_DEADLINE):
                source_id, env_key = futures[future]
                try:
                    results[(source_id, env_key)] = future.result()
                except Exception:
                    results[(source_id, env_key)] = False
                left[source_id] -= 1
                if not left[source_id] and source_id in by_id:
                    yield {"source": build(by_id[source_id])}
        except concurrent.futures.TimeoutError:
            pass

        now = time.time()
        late = set()
        for future, (source_id, env_key) in futures.items():
            if (source_id, env_key) in results:
                continue
            results[(source_id, env_key)] = False
            left[source_id] = 0
            late.add(source_id)
            _CONNECTIVITY_CACHE[f"{source_id}::{env_key}"] = (now + CONNECTIVITY_CACHE_TTL, False)
            logger.warning(f"Connectivity check for {source_id}/{env_key} exceeded "
                           f"{CONNECTIVITY_CHECK_DEADLINE}s — marking as unavailable.")
        for source_id in late:
            if source_id in by_id:
                yield {"source": build(by_id[source_id])}
    finally:
        # Don't wait for stragglers; let them finish (or die) in the background.
        pool.shutdown(wait=False)


@data_validation_router.get("/source-databases")
def get_source_databases(source_system: str = Query(...), env: str = Query(DEFAULT_ENVIRONMENT), auth: dict = Depends(require_permission("viewDataValidation"))):
    """List browsable databases for the chosen source system (empty if it has no database level)."""
    cfg = _source_cfg(source_system, env)
    try:
        with create_connector(source_system, cfg) as conn:
            if "database" not in conn.LEVELS:
                return {"applicable": False, "databases": []}
            return {"applicable": True, "databases": conn.list_databases()}
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing {source_system} databases: {e}")
        raise HTTPException(status_code=502, detail=f"Could not connect to {source_system}: {e}")


@data_validation_router.get("/source-schemas")
def get_source_schemas(source_system: str = Query(...), database: Optional[str] = Query(None), env: str = Query(DEFAULT_ENVIRONMENT), auth: dict = Depends(require_permission("viewDataValidation"))):
    """List browsable schemas for the chosen source system (empty if it has no schema level)."""
    _validate_identifier(database, "database")
    cfg = _source_cfg(source_system, env)
    try:
        with create_connector(source_system, cfg) as conn:
            if "schema" not in conn.LEVELS:
                return {"applicable": False, "schemas": []}
            return {"applicable": True, "schemas": conn.list_schemas(_empty_or_none(database))}
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing {source_system} schemas: {e}")
        raise HTTPException(status_code=502, detail=f"Could not connect to {source_system}: {e}")


@data_validation_router.get("/source-tables")
def get_source_tables(
    source_system: str = Query(...),
    database: Optional[str] = Query(None),
    schema: Optional[str] = Query(None),
    env: str = Query(DEFAULT_ENVIRONMENT),
    auth: dict = Depends(require_permission("viewDataValidation")),
):
    """List available tables/views for the chosen source system + database/schema."""
    _validate_identifier(database, "database")
    _validate_identifier(schema, "schema")
    cfg = _source_cfg(source_system, env)
    try:
        with create_connector(source_system, cfg) as conn:
            tables = conn.list_tables(_empty_or_none(database), _empty_or_none(schema))
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing {source_system} tables: {e}")
        raise HTTPException(status_code=502, detail=f"Could not connect to {source_system}: {e}")
    return {"source_system": source_system, "tables": tables}


# ─────────────────────────────────────────────────────────────────────────────
# Listing endpoints — Snowflake target
# ─────────────────────────────────────────────────────────────────────────────
@data_validation_router.get("/target-databases")
def get_target_databases(env: str = Query(DEFAULT_ENVIRONMENT), auth: dict = Depends(require_permission("viewDataValidation"))):
    cfg = _target_cfg(env)
    try:
        with create_connector("snowflake", cfg) as conn:
            return {"databases": conn.list_databases()}
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing Snowflake databases: {e}")
        raise HTTPException(status_code=502, detail=f"Could not connect to Snowflake: {e}")


@data_validation_router.get("/target-schemas")
def get_target_schemas(database: str = Query(...), env: str = Query(DEFAULT_ENVIRONMENT), auth: dict = Depends(require_permission("viewDataValidation"))):
    _validate_identifier(database, "database")
    cfg = _target_cfg(env)
    try:
        with create_connector("snowflake", cfg) as conn:
            return {"schemas": conn.list_schemas(database)}
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing Snowflake schemas: {e}")
        raise HTTPException(status_code=502, detail=f"Could not connect to Snowflake: {e}")


@data_validation_router.get("/target-tables")
def get_target_tables(database: str = Query(...), schema: str = Query(...), env: str = Query(DEFAULT_ENVIRONMENT), auth: dict = Depends(require_permission("viewDataValidation"))):
    """List available Snowflake target tables for the chosen database/schema."""
    _validate_identifier(database, "database")
    _validate_identifier(schema, "schema")
    cfg = _target_cfg(env)
    try:
        with create_connector("snowflake", cfg) as conn:
            tables = conn.list_tables(database, schema)
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing Snowflake target tables: {e}")
        raise HTTPException(status_code=502, detail=f"Could not connect to Snowflake: {e}")
    return {"tables": tables}


@data_validation_router.get("/source-table-search")
def search_source_tables(
    source_system: str = Query(...),
    query: str = Query(..., min_length=2),
    env: str = Query(DEFAULT_ENVIRONMENT),
    limit: int = Query(20, le=200),
    offset: int = Query(0, ge=0),
    max_schemas: int = Query(40, le=100),
    auth: dict = Depends(require_permission("viewDataValidation")),
):
    """Find tables by name alone, scanning across databases/schemas — for typing
    just a table name in the picker's search bar before choosing a database/schema.
    `offset` is how many (database, schema) pairs earlier calls already scanned —
    each call resumes from there instead of re-scanning the same schemas, so
    repeated calls (e.g. scrolling for more) actually reach new ground."""
    cfg = _source_cfg(source_system, env)
    q = query.strip().lower()
    results = []
    try:
        with create_connector(source_system, cfg) as conn:
            dbs = conn.list_databases() if "database" in conn.LEVELS else [None]
            pairs = []
            for db in dbs:
                try:
                    schemas = conn.list_schemas(db) if "schema" in conn.LEVELS else [None]
                except Exception as e:
                    logger.warning(f"Skipping database {db} during table search — can't connect: {e}")
                    continue
                pairs.extend((db, sch) for sch in schemas)
            window = pairs[offset:offset + max_schemas]
            scanned_this_call = 0
            for db, sch in window:
                scanned_this_call += 1
                try:
                    for t in conn.list_tables(db, sch):
                        if q in t.lower():
                            results.append({"database": db, "schema": sch, "table": t})
                except Exception as e:
                    logger.warning(f"Skipping {db}.{sch} during table search: {e}")
                if len(results) >= limit:
                    break
            next_offset = offset + scanned_this_call
            truncated = next_offset < len(pairs)
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error searching {source_system} tables: {e}")
        raise HTTPException(status_code=502, detail=f"Could not connect to {source_system}: {e}")
    return {"results": results, "truncated": truncated, "next_offset": next_offset}


@data_validation_router.get("/target-table-search")
def search_target_tables(
    query: str = Query(..., min_length=2),
    env: str = Query(DEFAULT_ENVIRONMENT),
    limit: int = Query(20, le=200),
    offset: int = Query(0, ge=0),
    max_schemas: int = Query(40, le=100),
    auth: dict = Depends(require_permission("viewDataValidation")),
):
    """Same as /source-table-search but for the Snowflake target side."""
    cfg = _target_cfg(env)
    q = query.strip().lower()
    results = []
    try:
        with create_connector("snowflake", cfg) as conn:
            pairs = []
            for db in conn.list_databases():
                try:
                    schemas = conn.list_schemas(db)
                except Exception as e:
                    logger.warning(f"Skipping database {db} during table search: {e}")
                    continue
                pairs.extend((db, sch) for sch in schemas)
            window = pairs[offset:offset + max_schemas]
            scanned_this_call = 0
            for db, sch in window:
                scanned_this_call += 1
                try:
                    for t in conn.list_tables(db, sch):
                        if q in t.lower():
                            results.append({"database": db, "schema": sch, "table": t})
                except Exception as e:
                    logger.warning(f"Skipping {db}.{sch} during table search: {e}")
                if len(results) >= limit:
                    break
            next_offset = offset + scanned_this_call
            truncated = next_offset < len(pairs)
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error searching target tables: {e}")
        raise HTTPException(status_code=502, detail=f"Could not connect to Snowflake: {e}")
    return {"results": results, "truncated": truncated, "next_offset": next_offset}


@data_validation_router.get("/common-columns")
def get_common_columns(
    source_system: str = Query(...),
    source_database: Optional[str] = Query(None),
    source_schema: Optional[str] = Query(None),
    source_table: str = Query(...),
    target_database: str = Query(...),
    target_schema: str = Query(...),
    target_table: str = Query(...),
    env: str = Query(DEFAULT_ENVIRONMENT),
    auth: dict = Depends(require_permission("viewDataValidation")),
):
    """Columns present on both source and target table, for the key-column picker."""
    _validate_identifier(source_database, "source_database")
    _validate_identifier(source_schema, "source_schema")
    _validate_identifier(source_table, "source_table")
    _validate_identifier(target_database, "target_database")
    _validate_identifier(target_schema, "target_schema")
    _validate_identifier(target_table, "target_table")
    src_cfg = _source_cfg(source_system, env)
    tgt_cfg = _target_cfg(env)
    try:
        with create_connector(source_system, src_cfg) as src_conn:
            src_columns = src_conn.get_columns(_empty_or_none(source_database), _empty_or_none(source_schema), source_table)
        with create_connector("snowflake", tgt_cfg) as tgt_conn:
            tgt_columns = tgt_conn.get_columns(target_database, target_schema, target_table)
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing columns for key-column picker: {e}")
        raise HTTPException(status_code=502, detail=f"Could not fetch columns: {e}")

    src_names = {c["name"].upper(): c for c in src_columns}
    tgt_names = {c["name"].upper(): c for c in tgt_columns}
    common = sorted(
        (upper for upper in src_names if upper in tgt_names),
        key=lambda upper: src_names[upper]["name"],
    )
    # The run rejects any key whose type can't be sorted/compared in SQL, so
    # flag those here — otherwise the picker happily offers a column that comes
    # straight back as "not found among the common, sortable columns".
    src_cls = CONNECTOR_REGISTRY.get(source_system.strip().lower())
    src_bad = getattr(src_cls, "NON_SORTABLE_TYPES", set())
    tgt_bad = CONNECTOR_REGISTRY["snowflake"].NON_SORTABLE_TYPES
    out = []
    for u in common:
        sortable = (
            str(src_names[u]["type"]).upper() not in src_bad
            and str(tgt_names[u]["type"]).upper() not in tgt_bad
        )
        out.append({
            "name": src_names[u]["name"],
            "type": src_names[u]["type"],
            "sortable": sortable,
            "suggested_score": _key_score(src_names[u]["name"], source_table) if sortable else 0,
        })
    return {"columns": out}


# ─────────────────────────────────────────────────────────────────────────────
# Comparison engine
# ─────────────────────────────────────────────────────────────────────────────
def _pick_key_column(common_columns: List[str], preferred: Optional[str],
                     table_name: Optional[str] = None) -> "tuple[Optional[str], Optional[str]]":
    """Returns (key_column, warning). key_column is None when no key should be
    used at all — the caller then does a full row-by-row comparison instead of
    a key-based one (many tables genuinely have no ID/key column).

    Raises ValueError if the user explicitly typed a key column that isn't
    among the common columns — we never silently substitute a different key
    for one the user asked for on purpose. Auto-swapping changes what's
    actually being validated (a different join key can produce a completely
    different missing/extra/mismatch result) without the user ever knowing
    their requested key was ignored."""
    if preferred:
        upper = preferred.upper()
        for c in common_columns:
            if c.upper() == upper:
                return c, None
        available = ", ".join(common_columns[:20]) or "(none — no sortable common columns)"
        raise ValueError(
            f"Requested key column '{preferred}' not found among the {len(common_columns)} common, "
            f"sortable column(s). Available: {available}. Leave key column blank to auto-detect or use "
            f"full row-by-row comparison instead."
        )
    best = None
    for c in common_columns:
        s = _key_score(c, table_name)
        if s > 0 and (best is None or s > best[0]):
            best = (s, c)
    if best:
        return best[1], None
    return None, None


# Suffixes that actually mark an identifier, checked as whole words (split on _
# and camelCase) so VALID / PAID / VOID / GRID / RAPID / HYBRID don't get picked
# up the way a bare .endswith("ID") did.
_KEY_TOKENS = {
    "ID": 100, "PK": 95, "KEY": 80, "SK": 80,
    "GUID": 75, "UUID": 75, "OID": 70,
    "NO": 40, "NUM": 40, "CODE": 35, "SEQ": 35,
}
# Columns that look key-ish but almost never are a good join key.
_KEY_PENALTY = ("CREATED", "UPDATED", "MODIFIED", "DELETED", "INSERT", "LOAD",
                "BATCH", "AUDIT", "IS_", "HAS_", "FLAG", "STATUS", "TYPE",
                "PARENT", "SOURCE", "USER", "SESSION", "JOB", "RUN")


def _key_tokens(name: str) -> List[str]:
    """Split ORDER_ID / orderId / OrderID into ['ORDER', 'ID']."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.upper()) if t]


def _split_glued(token: str, table_name: Optional[str]) -> "Optional[tuple[str, str]]":
    """ORDERNO / CUSTID arrive as one token with no separator to split on.

    Only treated as <entity><suffix> when the stem matches the table being
    compared — splitting on the suffix alone is what made AVOID, HYBRID and
    SOLID look like ID columns.
    """
    if not table_name:
        return None
    stems = {_singular(t) for t in _key_tokens(table_name)}
    for suffix in ("GUID", "UUID", "CODE", "KEY", "SEQ", "NUM", "ID", "NO", "PK", "SK"):
        if token.endswith(suffix) and len(token) > len(suffix):
            stem = token[: -len(suffix)]
            if _singular(stem) in stems:
                return stem, suffix
    return None


def _singular(token: str) -> str:
    if token.endswith("IES") and len(token) > 4:
        return token[:-3] + "Y"
    if token.endswith("SES") and len(token) > 4:
        return token[:-2]
    if token.endswith("S") and not token.endswith("SS") and len(token) > 3:
        return token[:-1]
    return token


def _key_score(column: str, table_name: Optional[str]) -> int:
    """Higher is a better auto-detected join key. 0 means 'not a key column'."""
    upper = column.upper()
    tokens = _key_tokens(column)
    if not tokens:
        return 0
    if len(tokens) == 1:
        glued = _split_glued(tokens[0], table_name)
        if glued:
            tokens = list(glued)

    if upper == "ID":
        score = 200
    else:
        score = _KEY_TOKENS.get(tokens[-1], 0)
        if not score:
            return 0
        # <TABLE>_ID on the table we're actually comparing is the likeliest PK.
        if table_name:
            noise = {"DIM", "FACT", "TBL", "TABLE", "STG", "STAGE", "V", "VW", "T"}
            # Strip prefixes and plurals so DIM_ORDERS still matches ORDER_ID.
            tbl = {_singular(t) for t in _key_tokens(table_name) if t not in noise}
            if tbl & {_singular(t) for t in tokens[:-1]}:
                score += 60
        # Single-token names like ORDERNO beat compound ones like CUST_ORDER_NO.
        if len(tokens) == 2:
            score += 10

    if any(p in upper for p in _KEY_PENALTY):
        score -= 70
    return max(score, 0)


def _real_names(col_map: dict, canon_names: List[str]) -> List[str]:
    """common_names/sortable_common_names are uppercased match-keys, not the
    columns' actual on-disk names — dialects that fold unquoted identifiers to
    lowercase (Postgres) or preserve case need the *real* name for SQL. This
    maps a list of canonical (uppercase) names back to each side's real name,
    in the same order."""
    return [col_map[c]["name"] for c in canon_names]


def _remap_rows(rows: List[dict], real_names: List[str], canon_names: List[str]) -> List[dict]:
    """Re-key rows fetched with real column names back to canonical (uppercase)
    names, so source and target rows can be merged/compared/hashed under a
    shared key regardless of each dialect's identifier casing."""
    mapping = dict(zip(real_names, canon_names))
    return [{mapping.get(k, k): v for k, v in row.items()} for row in rows]


def _norm_val(v) -> str:
    """Canonical string for a cell value so the same data coming back from two
    different drivers compares equal.

    Without this, Decimal('100.00') vs 100.0, True vs 1, and a tz-aware vs naive
    datetime all stringify differently and get reported as value mismatches (or
    as missing/extra records when it's the key column) even though source and
    target hold identical data.
    """
    if v is None:
        return "\x00NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, (int, float, Decimal)):
        try:
            d = Decimal(str(v)).normalize()
        except (InvalidOperation, ValueError):
            return str(v)
        # normalize() turns 100 into 1E+2 — undo that so it reads normally.
        return format(d, "f")
    if isinstance(v, datetime):
        # Compare the same instant regardless of whether the driver attached a
        # tzinfo; drop microseconds only when both sides have none anyway.
        if v.tzinfo is not None:
            v = v.astimezone(pytz.UTC).replace(tzinfo=None)
        return v.isoformat(sep=" ")
    if isinstance(v, (date, _time)):
        return v.isoformat()
    s = str(v)
    # Trailing whitespace differs between CHAR and VARCHAR storage.
    return s.rstrip()


def _row_hash(row: dict, columns: List[str]) -> str:
    """Order-stable hash of a row across a fixed column list, used to compare
    rows when there's no key column to join on."""
    parts = [_norm_val(row.get(c)) for c in columns]
    return hashlib.md5("\x1f".join(parts).encode("utf-8", errors="replace")).hexdigest()


def _full_row_diff(src_rows: List[dict], tgt_rows: List[dict], columns: List[str]) -> "tuple[dict, dict]":
    """Multiset (Counter) comparison of full rows across `columns` — used when
    there's no reliable key column to join on (either none exists, or the
    chosen one turned out not to be unique). Handles duplicate rows correctly
    since it's a multiset diff, not a set diff."""
    src_hashes = [_row_hash(r, columns) for r in src_rows]
    tgt_hashes = [_row_hash(r, columns) for r in tgt_rows]
    src_counts = Counter(src_hashes)
    tgt_counts = Counter(tgt_hashes)

    src_repr, tgt_repr = {}, {}
    for h, r in zip(src_hashes, src_rows):
        src_repr.setdefault(h, r)
    for h, r in zip(tgt_hashes, tgt_rows):
        tgt_repr.setdefault(h, r)

    missing_count = 0
    missing_sample = []
    for h, cnt in src_counts.items():
        diff = cnt - tgt_counts.get(h, 0)
        if diff > 0:
            missing_count += diff
            if len(missing_sample) < 25:
                missing_sample.append(src_repr[h])

    extra_count = 0
    extra_sample = []
    for h, cnt in tgt_counts.items():
        diff = cnt - src_counts.get(h, 0)
        if diff > 0:
            extra_count += diff
            if len(extra_sample) < 25:
                extra_sample.append(tgt_repr[h])

    missing_records = {
        "count": missing_count, "sample": missing_sample, "key_column": None,
        "sample_size": len(src_rows), "sampled": True, "method": "full_row",
        "skipped": False, "skip_reason": None,
    }
    extra_records = {
        "count": extra_count, "sample": extra_sample,
        "sample_size": len(tgt_rows), "sampled": True, "method": "full_row",
        "skipped": False, "skip_reason": None,
    }
    return missing_records, extra_records


def _both(src_call, tgt_call):
    """Run the source and target halves of a paired query at the same time.

    Every check here queries source then target sequentially, so a run costs
    the sum of the two. They use separate connections and never touch shared
    state, so the wall-clock cost can be the slower of the two instead — which
    matters most on the JDBC sources, where a single scan can take minutes.

    Exceptions propagate exactly as they would have when run in sequence, with
    the source error winning if both fail (that's the order the caller saw
    before, and the source is the more likely culprit).
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        src_future = pool.submit(src_call)
        tgt_future = pool.submit(tgt_call)
        concurrent.futures.wait([src_future, tgt_future])
        # Resolve source first so its exception is the one raised.
        return src_future.result(), tgt_future.result()


def _run_comparison(
    source_system: str,
    source_database: Optional[str],
    source_schema: Optional[str],
    source_table: str,
    target_database: str,
    target_schema: str,
    target_table: str,
    source_cfg: dict,
    target_cfg: dict,
    key_column: Optional[str],
    sample_limit: int,
    progress_cb,
) -> dict:
    """Blocking comparison logic. `progress_cb(pct, label)` reports progress."""
    started_at = datetime.now(EASTERN)
    issues: List[str] = []

    progress_cb(3, f"Connecting to {source_system}…")
    src_conn = create_connector(source_system, source_cfg)
    src_conn.connect()
    try:
        progress_cb(10, "Connecting to Snowflake (target)…")
        tgt_conn = create_connector("snowflake", target_cfg)
        tgt_conn.connect()
        try:
            progress_cb(20, "Comparing schemas…")
            src_columns, tgt_columns = _both(
                lambda: src_conn.get_columns(source_database, source_schema, source_table),
                lambda: tgt_conn.get_columns(target_database, target_schema, target_table),
            )

            src_col_map = {c["name"].upper(): c for c in src_columns}
            tgt_col_map = {c["name"].upper(): c for c in tgt_columns}
            src_names = set(src_col_map)
            tgt_names = set(tgt_col_map)
            common_names = sorted(src_names & tgt_names)
            src_only = sorted(src_names - tgt_names)
            tgt_only = sorted(tgt_names - src_names)
            no_common_columns = not common_names

            # Some column types can't be used in ORDER BY/GROUP BY/comparisons
            # at all on certain databases (e.g. SQL Server's XML type raises
            # "cannot be compared or sorted, except when using the IS NULL
            # operator"). Exclude those from key auto-detection, ORDER BY, and
            # GROUP BY on both sides — they're still selected and compared by
            # value in Python for full-row/null checks, just not sorted/grouped
            # in SQL.
            non_sortable_columns = sorted({
                name for name in common_names
                if str(src_col_map[name]["type"]).upper() in src_conn.NON_SORTABLE_TYPES
                or str(tgt_col_map[name]["type"]).upper() in tgt_conn.NON_SORTABLE_TYPES
            })
            sortable_common_names = [c for c in common_names if c not in non_sortable_columns]

            # Real (actual-cased) column names per side — always use these when
            # talking to the database. common_names/sortable_common_names are
            # uppercase match-keys only; using them directly in SQL breaks on
            # dialects that fold unquoted identifiers to lowercase (Postgres).
            src_common_real = _real_names(src_col_map, common_names)
            tgt_common_real = _real_names(tgt_col_map, common_names)
            src_sortable_real = _real_names(src_col_map, sortable_common_names)
            tgt_sortable_real = _real_names(tgt_col_map, sortable_common_names)

            if non_sortable_columns:
                issues.append(
                    f"{len(non_sortable_columns)} column(s) excluded from duplicate/ordering checks — "
                    f"type not sortable/comparable: {', '.join(non_sortable_columns[:10])}"
                )

            schema_comparison = {
                "source_columns": len(src_columns),
                "target_columns": len(tgt_columns),
                "common_columns": len(common_names),
                "source_only_columns": src_only,
                "target_only_columns": tgt_only,
                "match": not src_only and not tgt_only,
                "scope_note": None,
            }
            if src_only:
                issues.append(f"{len(src_only)} column(s) only in source: {', '.join(src_only[:10])}")
            if tgt_only:
                issues.append(f"{len(tgt_only)} column(s) only in target: {', '.join(tgt_only[:10])}")
            if not schema_comparison["match"]:
                if no_common_columns:
                    schema_comparison["scope_note"] = (
                        "Source and target share no common columns — data type, null, duplicate, and "
                        "record-level checks below were all skipped."
                    )
                else:
                    schema_comparison["scope_note"] = (
                        f"Schema mismatch detected: only the {len(common_names)} column(s) present on both "
                        f"sides are used for every check below ({len(src_only)} source-only and "
                        f"{len(tgt_only)} target-only column(s) are excluded from comparison)."
                    )

            # ── Data type validation ──
            if no_common_columns:
                data_type_validation = {
                    "checked_columns": 0, "mismatched_columns": 0,
                    "major_mismatches": 0, "minor_mismatches": 0, "mismatches": [],
                    "match": None, "skipped": True,
                    "skip_reason": "No common columns between source and target",
                }
            else:
                type_mismatches = []
                for name in common_names:
                    st = str(src_col_map[name]["type"]).upper()
                    tt = str(tgt_col_map[name]["type"]).upper()
                    if _normalize_type(st) != _normalize_type(tt):
                        type_mismatches.append({
                            "column": name, "source_type": st, "target_type": tt,
                            "severity": _type_mismatch_severity(st, tt),
                        })
                # A VARCHAR(50) -> VARCHAR(200) widening and a NUMBER -> VARCHAR
                # change were both just "mismatch", which buried the ones that
                # actually threaten the load among the ones nobody cares about.
                major = [m for m in type_mismatches if m["severity"] == "major"]
                data_type_validation = {
                    "checked_columns": len(common_names),
                    "mismatched_columns": len(type_mismatches),
                    "major_mismatches": len(major),
                    "minor_mismatches": len(type_mismatches) - len(major),
                    # Majors first so the 50-row cap never hides a real problem
                    # behind a pile of harmless length differences.
                    "mismatches": (major + [m for m in type_mismatches if m["severity"] != "major"])[:50],
                    "match": len(type_mismatches) == 0,
                    "skipped": False, "skip_reason": None,
                }
                if major:
                    issues.append(
                        f"{len(major)} column(s) have incompatible data types "
                        f"(e.g. {major[0]['column']}: {major[0]['source_type']} vs {major[0]['target_type']})"
                    )
                if len(type_mismatches) > len(major):
                    issues.append(
                        f"{len(type_mismatches) - len(major)} column(s) differ only in "
                        f"length/precision or an equivalent type name"
                    )

            # ── Row count ──
            progress_cb(35, "Counting rows…")

            # Timed inside each lambda so the reported per-side durations stay
            # true now that the two run concurrently — measuring around _both
            # would just report the slower side twice.
            def _timed(fn):
                def go():
                    t0 = time.time()
                    value = fn()
                    return value, int((time.time() - t0) * 1000)
                return go

            (src_count, src_ms), (tgt_count, tgt_ms) = _both(
                _timed(lambda: src_conn.get_row_count(source_database, source_schema, source_table)),
                _timed(lambda: tgt_conn.get_row_count(target_database, target_schema, target_table)),
            )
            row_count = {
                "source": src_count,
                "target": tgt_count,
                "match": src_count == tgt_count,
                "difference": abs(src_count - tgt_count),
            }
            if src_count != tgt_count:
                issues.append(f"Row count mismatch: source={src_count:,}, target={tgt_count:,}")

            # ── Null checks (common columns) ──
            progress_cb(50, "Checking for nulls…")
            if no_common_columns:
                null_checks = {
                    "columns": [], "mismatched_columns": 0, "match": None,
                    "skipped": True, "skip_reason": "No common columns between source and target",
                }
            else:
                src_nulls_real, tgt_nulls_real = _both(
                    lambda: src_conn.get_null_counts(source_database, source_schema, source_table, src_common_real),
                    lambda: tgt_conn.get_null_counts(target_database, target_schema, target_table, tgt_common_real),
                )
                src_nulls = {canon: src_nulls_real.get(real, 0) for canon, real in zip(common_names, src_common_real)}
                tgt_nulls = {canon: tgt_nulls_real.get(real, 0) for canon, real in zip(common_names, tgt_common_real)}
                null_columns = []
                mismatched_null_cols = 0
                for name in common_names:
                    sn = src_nulls.get(name, 0)
                    tn = tgt_nulls.get(name, 0)
                    match = sn == tn
                    if not match:
                        mismatched_null_cols += 1
                    st = str(src_col_map[name]["type"]).upper()
                    tt = str(tgt_col_map[name]["type"]).upper()
                    null_columns.append({
                        "column": name,
                        "source_type": st, "target_type": tt,
                        "type_match": _normalize_type(st) == _normalize_type(tt),
                        "source_nulls": sn, "target_nulls": tn, "match": match,
                    })
                null_checks = {
                    "columns": null_columns,
                    "mismatched_columns": mismatched_null_cols,
                    "match": mismatched_null_cols == 0,
                    "skipped": False, "skip_reason": None,
                }
                if mismatched_null_cols:
                    issues.append(f"{mismatched_null_cols} column(s) have differing null counts")

            # ── Full-table aggregates ──
            # Everything below this point compares a sample; this stage is the
            # only one that sees every row. It's best-effort: any dialect that
            # rejects the SQL degrades to a "skipped" result rather than failing
            # the run, so a source with an unsupported type or a permissions
            # gap still produces a full report.
            progress_cb(58, "Comparing full-table aggregates…")
            aggregate_checks = {
                "match": None, "skipped": True, "skip_reason": "Not evaluated",
                "checked_columns": 0, "mismatches": [], "row_count_source": None,
                "row_count_target": None,
            }
            if no_common_columns:
                aggregate_checks["skip_reason"] = "No common columns to aggregate"
            else:
                # Numeric columns get SUM/MIN/MAX; everything else just a
                # non-null count. Non-sortable types (CLOB/XML/…) are excluded
                # entirely — the same types already barred from GROUP BY.
                agg_names = [
                    n for n in common_names
                    if str(src_col_map[n]["type"]).upper() not in src_conn.NON_SORTABLE_TYPES
                    and str(tgt_col_map[n]["type"]).upper() not in tgt_conn.NON_SORTABLE_TYPES
                ]
                numeric_names = [
                    n for n in agg_names
                    if _normalize_type(str(src_col_map[n]["type"])) == "NUMERIC"
                    and _normalize_type(str(tgt_col_map[n]["type"])) == "NUMERIC"
                ]
                if not agg_names:
                    aggregate_checks["skip_reason"] = "No aggregatable common columns"
                else:
                    src_agg_real = [src_col_map[n]["name"] for n in agg_names]
                    tgt_agg_real = [tgt_col_map[n]["name"] for n in agg_names]
                    src_num_real = [src_col_map[n]["name"] for n in numeric_names]
                    tgt_num_real = [tgt_col_map[n]["name"] for n in numeric_names]
                    try:
                        src_agg, tgt_agg = _both(
                            lambda: src_conn.get_column_aggregates(
                                source_database, source_schema, source_table, src_agg_real, src_num_real),
                            lambda: tgt_conn.get_column_aggregates(
                                target_database, target_schema, target_table, tgt_agg_real, tgt_num_real),
                        )
                        agg_mismatches = []
                        for i, name in enumerate(agg_names):
                            s_stats = src_agg.get(src_agg_real[i]) or {}
                            t_stats = tgt_agg.get(tgt_agg_real[i]) or {}
                            for kind in ("non_null", "sum", "min", "max"):
                                if kind not in s_stats or kind not in t_stats:
                                    continue
                                # Same normalizer the row comparison uses, so
                                # Decimal/float/driver differences don't read as
                                # drift here either.
                                if _norm_val(s_stats[kind]) != _norm_val(t_stats[kind]):
                                    agg_mismatches.append({
                                        "column": name, "metric": kind,
                                        "source": s_stats[kind], "target": t_stats[kind],
                                    })
                        aggregate_checks = {
                            "match": len(agg_mismatches) == 0,
                            "skipped": False, "skip_reason": None,
                            "checked_columns": len(agg_names),
                            "numeric_columns": len(numeric_names),
                            "mismatches": agg_mismatches[:50],
                            "mismatch_count": len(agg_mismatches),
                            "row_count_source": (src_agg.get("*") or {}).get("row_count"),
                            "row_count_target": (tgt_agg.get("*") or {}).get("row_count"),
                        }
                        if agg_mismatches:
                            cols_affected = len({m["column"] for m in agg_mismatches})
                            issues.append(
                                f"{cols_affected} column(s) differ on full-table aggregates "
                                f"(e.g. {agg_mismatches[0]['column']} {agg_mismatches[0]['metric']}: "
                                f"{agg_mismatches[0]['source']} vs {agg_mismatches[0]['target']})"
                            )
                    except Exception as e:
                        logger.warning(f"Full-table aggregate comparison unavailable: {e}")
                        aggregate_checks["skip_reason"] = f"Not supported for this source: {e}"

            # ── Duplicate checks ──
            progress_cb(62, "Checking for duplicates…")
            if no_common_columns:
                duplicate_checks = {
                    "source_duplicates": 0, "target_duplicates": 0, "match": None,
                    "skipped": True, "skip_reason": "No common columns to check for duplicates",
                    "excluded_columns": [],
                }
            elif not sortable_common_names:
                duplicate_checks = {
                    "source_duplicates": 0, "target_duplicates": 0, "match": None,
                    "skipped": True,
                    "skip_reason": "All common columns have a type that can't be grouped/compared "
                                   f"(e.g. XML): {', '.join(non_sortable_columns[:10])}",
                    "excluded_columns": non_sortable_columns,
                }
            else:
                src_dupes, tgt_dupes = _both(
                    lambda: src_conn.get_duplicate_count(source_database, source_schema, source_table, src_sortable_real),
                    lambda: tgt_conn.get_duplicate_count(target_database, target_schema, target_table, tgt_sortable_real),
                )
                duplicate_checks = {
                    "source_duplicates": src_dupes,
                    "target_duplicates": tgt_dupes,
                    "match": src_dupes == tgt_dupes,
                    "skipped": False, "skip_reason": None,
                    "excluded_columns": non_sortable_columns,
                }
                if src_dupes:
                    issues.append(f"{src_dupes:,} duplicate row-group(s) found in source")
                if tgt_dupes:
                    issues.append(f"{tgt_dupes:,} duplicate row-group(s) found in target")

            # ── Missing/extra records + data mismatches (sampled) ──
            progress_cb(75, "Comparing sampled records…")
            key_col, key_warning = _pick_key_column(sortable_common_names, key_column, source_table)
            key_column_auto = bool(key_col) and not key_column
            # Tracked separately from the generic `issues` list so the UI can
            # surface it as its own prominent callout instead of burying it
            # among schema/null/duplicate issues.
            key_column_issue: Optional[str] = key_warning
            if key_warning:
                issues.append(key_warning)

            missing_records = {"count": 0, "sample": [], "key_column": key_col, "sample_size": 0, "sampled": True, "method": None, "skipped": False, "skip_reason": None}
            extra_records = {"count": 0, "sample": [], "sample_size": 0, "sampled": True, "method": None, "skipped": False, "skip_reason": None}
            data_mismatches = {"count": 0, "mismatched_row_count": 0, "sample": [], "sampled": True, "not_applicable": False, "skipped": False, "skip_reason": None}

            if no_common_columns:
                reason = "No common columns between source and target"
                issues.append(f"{reason} — skipped record-level comparison")
                missing_records["skipped"] = True
                missing_records["skip_reason"] = reason
                extra_records["skipped"] = True
                extra_records["skip_reason"] = reason
                data_mismatches["skipped"] = True
                data_mismatches["skip_reason"] = reason
                data_mismatches["not_applicable"] = True
            if key_col and not no_common_columns:
                # Validate the key across the WHOLE table before trusting it.
                # The sampled uniqueness check further down only ever saw
                # `sample_limit` rows, so a key that repeats outside the sample
                # window slipped through and silently dropped rows when the
                # {key: row} map was built. Both of these reuse work already
                # done: get_duplicate_count is the same one-column GROUP BY the
                # duplicate check runs, and the null counts came back with the
                # null check above — no extra round-trips.
                pre_issue = None
                src_key_nulls = src_nulls.get(key_col, 0) if not null_checks["skipped"] else 0
                tgt_key_nulls = tgt_nulls.get(key_col, 0) if not null_checks["skipped"] else 0
                if src_key_nulls or tgt_key_nulls:
                    pre_issue = (
                        f"Key column '{key_col}' contains NULLs ({src_key_nulls:,} in source, "
                        f"{tgt_key_nulls:,} in target) — a NULL can't identify a record, so the "
                        f"comparison used a full row-by-row match instead"
                    )
                else:
                    try:
                        src_key_dupes, tgt_key_dupes = _both(
                            lambda: src_conn.get_duplicate_count(source_database, source_schema, source_table, [src_col_map[key_col]["name"]]),
                            lambda: tgt_conn.get_duplicate_count(target_database, target_schema, target_table, [tgt_col_map[key_col]["name"]]),
                        )
                        if src_key_dupes or tgt_key_dupes:
                            pre_issue = (
                                f"Key column '{key_col}' is not unique across the full table "
                                f"({src_key_dupes:,} repeated value(s) in source, {tgt_key_dupes:,} in "
                                f"target) — the comparison used a full row-by-row match instead"
                            )
                    except Exception as e:
                        # Best-effort: the sampled check below still guards us.
                        logger.warning(f"Could not verify uniqueness of key column {key_col}: {e}")

                if pre_issue:
                    key_column_issue = pre_issue
                    issues.append(pre_issue)
                    key_col = None
                    key_column_auto = False

            if no_common_columns:
                pass
            elif key_col:
                src_key_real = src_col_map[key_col]["name"]
                tgt_key_real = tgt_col_map[key_col]["name"]
                src_rows, tgt_rows = _both(
                    lambda: src_conn.fetch_rows(source_database, source_schema, source_table,
                                                src_common_real, sample_limit, order_by=[src_key_real]),
                    lambda: tgt_conn.fetch_rows(target_database, target_schema, target_table,
                                                tgt_common_real, sample_limit, order_by=[tgt_key_real]),
                )
                src_rows = _remap_rows(src_rows, src_common_real, common_names)
                tgt_rows = _remap_rows(tgt_rows, tgt_common_real, common_names)

                # A key column only works as a join key if it's actually unique.
                # Building {key: row} from a column with duplicate values would
                # silently keep just the last row per key and drop the rest —
                # detect that up front and fall back to full row-by-row instead
                # of quietly losing data.
                src_key_counts = Counter(_norm_val(r.get(key_col)) for r in src_rows)
                tgt_key_counts = Counter(_norm_val(r.get(key_col)) for r in tgt_rows)
                src_dupe_keys = sum(1 for c in src_key_counts.values() if c > 1)
                tgt_dupe_keys = sum(1 for c in tgt_key_counts.values() if c > 1)

                # NULL keys can't identify a row either. Two rows with a NULL key
                # aren't "the same record", so joining on them would pair up
                # unrelated rows and hide real differences.
                null_key = _norm_val(None)
                src_null_keys = src_key_counts.get(null_key, 0)
                tgt_null_keys = tgt_key_counts.get(null_key, 0)

                if src_dupe_keys or tgt_dupe_keys or src_null_keys or tgt_null_keys:
                    if src_null_keys or tgt_null_keys:
                        key_column_issue = (
                            f"Key column '{key_col}' contains NULLs ({src_null_keys} in source, "
                            f"{tgt_null_keys} in target, within the sampled rows) — a NULL can't identify "
                            f"a record, so falling back to full row-by-row comparison instead"
                        )
                    else:
                        key_column_issue = (
                            f"Key column '{key_col}' is not unique ({src_dupe_keys} duplicate value(s) in "
                            f"source, {tgt_dupe_keys} in target, within the sampled rows) — falling back to "
                            f"full row-by-row comparison instead"
                        )
                    issues.append(key_column_issue)
                    key_col = None
                    data_mismatches["not_applicable"] = True
                    # Re-fetch ordered by all sortable common columns rather
                    # than reusing the key-ordered rows above — full-row mode's
                    # sample should be selected the same deterministic way
                    # regardless of why it ended up in this mode. (Still SELECT
                    # every common column — only the ORDER BY is restricted to
                    # sortable ones.)
                    src_rows, tgt_rows = _both(
                        lambda: src_conn.fetch_rows(source_database, source_schema, source_table,
                                                    src_common_real, sample_limit, order_by=src_sortable_real),
                        lambda: tgt_conn.fetch_rows(target_database, target_schema, target_table,
                                                    tgt_common_real, sample_limit, order_by=tgt_sortable_real),
                    )
                    src_rows = _remap_rows(src_rows, src_common_real, common_names)
                    tgt_rows = _remap_rows(tgt_rows, tgt_common_real, common_names)
                    missing_records, extra_records = _full_row_diff(src_rows, tgt_rows, common_names)
                    if missing_records["count"]:
                        issues.append(f"{missing_records['count']:,} row(s) present in source but not found in target (full-row match, sampled)")
                    if extra_records["count"]:
                        issues.append(f"{extra_records['count']:,} row(s) present in target but not found in source (full-row match, sampled)")
                else:
                    # ── Key-based comparison: join on key_col, then diff field-by-field ──
                    missing_records["method"] = "key"
                    extra_records["method"] = "key"

                    # Each side's sample is independently the first `sample_limit`
                    # rows ordered by key. If the sample was truncated on either
                    # side, the two windows can cover different key ranges (e.g.
                    # source's window reaches key 50000, target's only reaches
                    # 48000 because of a different row count) — rows in that gap
                    # would look "missing"/"extra" purely because of where the
                    # LIMIT cut off, not because they're actually different. Trim
                    # both sides down to the key range both windows actually
                    # reached before diffing.
                    src_truncated = len(src_rows) >= sample_limit
                    tgt_truncated = len(tgt_rows) >= sample_limit
                    if src_truncated or tgt_truncated:
                        try:
                            candidates = []
                            if src_truncated and src_rows:
                                candidates.append(src_rows[-1][key_col])
                            if tgt_truncated and tgt_rows:
                                candidates.append(tgt_rows[-1][key_col])
                            boundary = min(candidates) if candidates else None
                            if boundary is not None:
                                src_rows = [r for r in src_rows if r.get(key_col) is not None and r[key_col] <= boundary]
                                tgt_rows = [r for r in tgt_rows if r.get(key_col) is not None and r[key_col] <= boundary]
                                issues.append(
                                    f"Source and/or target sample hit the {sample_limit:,}-row limit with "
                                    f"different key coverage — missing/extra/mismatch checks were trimmed to "
                                    f"keys <= {boundary} so both sides compare the same range. Raise the row "
                                    f"sample limit in Advanced options for fuller coverage."
                                )
                        except TypeError:
                            # Key values from the two drivers weren't directly
                            # comparable (e.g. str vs datetime) — can't safely
                            # determine a shared boundary, so fall back to the
                            # untrimmed windows and flag the risk instead.
                            issues.append(
                                "Source and target samples may not cover the same key range (key values "
                                "weren't directly comparable to confirm) — missing/extra counts near the "
                                "sample boundary may be sampling artifacts rather than real differences."
                            )

                    src_by_key = {_norm_val(r.get(key_col)): r for r in src_rows}
                    tgt_by_key = {_norm_val(r.get(key_col)): r for r in tgt_rows}

                    missing_keys = [k for k in src_by_key if k not in tgt_by_key]
                    missing_records["count"] = len(missing_keys)
                    missing_records["sample"] = [src_by_key[k] for k in missing_keys[:25]]
                    missing_records["sample_size"] = len(src_rows)
                    if missing_keys:
                        issues.append(f"{len(missing_keys):,} record(s) present in source but missing from target (sampled)")

                    extra_keys = [k for k in tgt_by_key if k not in src_by_key]
                    extra_records["count"] = len(extra_keys)
                    extra_records["sample"] = [tgt_by_key[k] for k in extra_keys[:25]]
                    extra_records["sample_size"] = len(tgt_rows)
                    if extra_keys:
                        issues.append(f"{len(extra_keys):,} record(s) present in target but missing from source (sampled)")

                    mismatch_rows = {}
                    mismatch_count = 0
                    mismatched_row_count = 0
                    other_cols = [c for c in common_names if c != key_col]
                    for k, src_row in src_by_key.items():
                        tgt_row = tgt_by_key.get(k)
                        if tgt_row is None:
                            continue
                        row_mismatched_cols = [
                            col for col in other_cols
                            if _norm_val(src_row.get(col)) != _norm_val(tgt_row.get(col))
                        ]
                        if row_mismatched_cols:
                            mismatch_count += len(row_mismatched_cols)
                            mismatched_row_count += 1
                            if len(mismatch_rows) < 25:
                                mismatch_rows[k] = {
                                    "key": k,
                                    "mismatched_columns": row_mismatched_cols,
                                    "source_row": src_row,
                                    "target_row": tgt_row,
                                }
                    data_mismatches["count"] = mismatch_count
                    data_mismatches["mismatched_row_count"] = mismatched_row_count
                    data_mismatches["sample"] = list(mismatch_rows.values())
                    if mismatch_count:
                        issues.append(f"{mismatch_count:,} field-level value mismatch(es) found (sampled)")
            else:
                # ── No key column at all: full row-by-row comparison across all
                # common columns. Sorted by every sortable common column so the
                # sample taken from each side (when smaller than the full
                # table) is deterministic and far more likely to line up
                # between source and target than an unordered LIMIT would be
                # (columns with a non-sortable type, e.g. XML, are still
                # selected and hashed — just not part of the ORDER BY). A
                # "mismatched" row just shows up as one missing + one extra
                # record — there's no key to align fields under, so
                # field-level mismatches aren't reported here.
                data_mismatches["not_applicable"] = True

                src_rows, tgt_rows = _both(
                    lambda: src_conn.fetch_rows(source_database, source_schema, source_table,
                                                src_common_real, sample_limit, order_by=src_sortable_real),
                    lambda: tgt_conn.fetch_rows(target_database, target_schema, target_table,
                                                tgt_common_real, sample_limit, order_by=tgt_sortable_real),
                )
                src_rows = _remap_rows(src_rows, src_common_real, common_names)
                tgt_rows = _remap_rows(tgt_rows, tgt_common_real, common_names)
                missing_records, extra_records = _full_row_diff(src_rows, tgt_rows, common_names)
                if missing_records["count"]:
                    issues.append(f"{missing_records['count']:,} row(s) present in source but not found in target (full-row match, sampled)")
                if extra_records["count"]:
                    issues.append(f"{extra_records['count']:,} row(s) present in target but not found in source (full-row match, sampled)")

            progress_cb(95, "Finalizing results…")
            completed_at = datetime.now(EASTERN)

            is_failed = (
                not row_count["match"]
                or missing_records["count"] > 0
                or extra_records["count"] > 0
                or (not data_mismatches["not_applicable"] and data_mismatches["count"] > 0)
                or not schema_comparison["match"]
            )
            if aggregate_checks.get("match") is False:
                # Aggregates cover every row, so a difference here is a genuine
                # data difference, not a sampling artefact.
                is_failed = True
            # null_checks was missing here, so a table with differing null counts
            # reported PASSED overall while the UI showed the null row as a warning.
            has_warning = (
                data_type_validation["match"] is False
                or duplicate_checks["match"] is False
                or null_checks["match"] is False
                or key_column_issue is not None
            )
            overall_status = "FAILED" if is_failed else ("WARNING" if has_warning else "PASSED")

            return {
                "run_id": str(uuid.uuid4()),
                "source_system": source_system,
                "source_database": source_database,
                "source_schema": source_schema,
                "source_table": source_table,
                "target_database": target_database,
                "target_schema": target_schema,
                "target_table": target_table,
                "key_column": key_col,
                "key_column_issue": key_column_issue,
                "key_column_auto": key_column_auto,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "duration_seconds": round((completed_at - started_at).total_seconds(), 2),
                "overall_status": overall_status,
                "row_count": row_count,
                "schema_comparison": schema_comparison,
                "non_sortable_columns": non_sortable_columns,
                "data_type_validation": data_type_validation,
                "null_checks": null_checks,
                "duplicate_checks": duplicate_checks,
                "aggregate_checks": aggregate_checks,
                "missing_records": missing_records,
                "extra_records": extra_records,
                "data_mismatches": data_mismatches,
                "additional_metrics": {
                    "source_row_count_ms": src_ms,
                    "target_row_count_ms": tgt_ms,
                    # This is a SAMPLE match rate, not a full-table one — missing_records/
                    # extra_records/data_mismatches only ever look at up to `sample_limit`
                    # rows per side (fetch_rows never scans the whole table), so the
                    # denominator has to be the sample actually compared, not
                    # row_count["source"]. A row only counts as "matched" if it was found
                    # on both sides AND every common field agreed — a row present on both
                    # sides with a field-level mismatch is NOT a match, even though it
                    # doesn't show up in missing_records.
                    "match_percentage": None if missing_records["skipped"] else (
                        max(0.0, round(100.0 * (1 - (
                            (missing_records["count"] + data_mismatches.get("mismatched_row_count", 0))
                            / missing_records["sample_size"]
                        )), 2))
                        if missing_records["sample_size"] else 100.0
                    ),
                },
                "issues": issues,
            }
        finally:
            # A raised exception here would replace the successful `return`
            # above (Python runs `finally` before actually returning, and an
            # exception in `finally` wins) — so a flaky disconnect on a
            # perfectly good run would get reported as a failed IT check.
            # Disconnect is best-effort cleanup, not part of the result.
            try:
                tgt_conn.disconnect()
            except Exception as e:
                logger.warning(f"Error disconnecting from Snowflake (target): {e}")
    finally:
        try:
            src_conn.disconnect()
        except Exception as e:
            logger.warning(f"Error disconnecting from {source_system}: {e}")


_TYPE_ALIASES = {
    "VARCHAR2": "VARCHAR", "NVARCHAR2": "VARCHAR", "NVARCHAR": "VARCHAR", "TEXT": "VARCHAR",
    "NTEXT": "VARCHAR", "STRING": "VARCHAR", "CHARACTER VARYING": "VARCHAR", "CHAR VARYING": "VARCHAR",
    "CHARACTER": "CHAR", "NCHAR": "CHAR", "BPCHAR": "CHAR",
    "NUMBER": "NUMERIC", "DECIMAL": "NUMERIC", "DEC": "NUMERIC", "INT": "NUMERIC",
    "INTEGER": "NUMERIC", "BIGINT": "NUMERIC", "SMALLINT": "NUMERIC", "TINYINT": "NUMERIC",
    "INT2": "NUMERIC", "INT4": "NUMERIC", "INT8": "NUMERIC", "FLOAT": "NUMERIC", "FLOAT4": "NUMERIC",
    "FLOAT8": "NUMERIC", "DOUBLE": "NUMERIC", "DOUBLE PRECISION": "NUMERIC", "REAL": "NUMERIC",
    "MONEY": "NUMERIC", "BINARY_FLOAT": "NUMERIC", "BINARY_DOUBLE": "NUMERIC",
    "TIMESTAMP_NTZ": "TIMESTAMP", "TIMESTAMP_TZ": "TIMESTAMP", "TIMESTAMP_LTZ": "TIMESTAMP",
    "DATETIME": "TIMESTAMP", "DATETIME2": "TIMESTAMP", "SMALLDATETIME": "TIMESTAMP",
    "TIMESTAMP WITHOUT TIME ZONE": "TIMESTAMP", "TIMESTAMP WITH TIME ZONE": "TIMESTAMP",
    "TIMESTAMP WITH LOCAL TIME ZONE": "TIMESTAMP",
    "BOOL": "BOOLEAN", "BIT": "BOOLEAN",
    "CLOB": "VARCHAR", "NCLOB": "VARCHAR", "BLOB": "BINARY", "VARBINARY": "BINARY",
    "BYTEA": "BINARY", "RAW": "BINARY",
}


def _normalize_type(t: str) -> str:
    """Collapse near-equivalent type names across dialects (e.g. VARCHAR2 vs VARCHAR).

    Matches the base type name exactly rather than by substring. The old
    substring scan folded anything merely *containing* an alias into it — so
    INTERVAL and POINT both came back as NUMERIC (because they contain "INT"),
    meaning a genuine INTERVAL-vs-NUMBER difference was reported as a match.
    """
    t = (t or "").upper().strip()
    # Drop precision/scale and array markers: NUMBER(10,2) -> NUMBER, INT[] -> INT.
    base = re.sub(r"\s*\(.*", "", t)
    base = re.sub(r"\s*\[\s*\]\s*$", "", base).strip()
    # Oracle spells intervals "INTERVAL DAY(3) TO SECOND(6)" — keep them distinct
    # from numerics rather than letting the DAY/SECOND parts confuse the match.
    if base.startswith("INTERVAL"):
        return "INTERVAL"
    if base.startswith("TIMESTAMP"):
        return _TYPE_ALIASES.get(base, "TIMESTAMP")
    return _TYPE_ALIASES.get(base, base)


# Types that hold the same kind of value. A difference *within* a family is a
# storage detail (VARCHAR vs CHAR, NUMERIC vs its precision); a difference
# *across* families means the data itself is being reinterpreted.
_TYPE_FAMILY = {
    "VARCHAR": "text", "CHAR": "text",
    "NUMERIC": "number",
    "TIMESTAMP": "datetime", "DATE": "datetime", "TIME": "datetime",
    "BOOLEAN": "boolean",
    "BINARY": "binary",
    "INTERVAL": "interval",
}


def _type_mismatch_severity(src_type: str, tgt_type: str) -> str:
    """'minor' when both sides hold the same kind of value, 'major' otherwise.

    Minor covers VARCHAR(50) vs VARCHAR(200) and NUMBER(10,2) vs NUMBER(12,4) —
    real differences, but they don't change what the value means and usually
    don't need action. Major is a genuine class change (NUMBER vs VARCHAR,
    DATE vs VARCHAR) that will bite during a load.
    """
    sn, tn = _normalize_type(src_type), _normalize_type(tgt_type)
    sf, tf = _TYPE_FAMILY.get(sn), _TYPE_FAMILY.get(tn)
    if sf and tf and sf == tf:
        return "minor"
    if sf is None or tf is None:
        # An unrecognised type (VARIANT, GEOGRAPHY, a vendor type) — can't
        # reason about it, so don't quietly downgrade it.
        return "major"
    return "major"


# ─────────────────────────────────────────────────────────────────────────────
# SSE streaming run endpoint
# ─────────────────────────────────────────────────────────────────────────────
class _RunCancelled(Exception):
    """Raised inside the worker when the client goes away."""


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


@data_validation_router.get("/run")
async def run_it_check(
    source_system: str = Query(...),
    source_database: Optional[str] = Query(None),
    source_schema: Optional[str] = Query(None),
    source_table: str = Query(...),
    target_database: str = Query(...),
    target_schema: str = Query(...),
    target_table: str = Query(...),
    key_column: Optional[str] = Query(None),
    sample_limit: int = Query(DEFAULT_SAMPLE_LIMIT, ge=100, le=200_000),
    env: str = Query(DEFAULT_ENVIRONMENT),
    auth: dict = Depends(require_permission("runDataValidation")),
):
    """Run the source→target comparison, streaming progress via SSE. The final
    event (`type: result`) carries the full metrics payload the frontend renders."""

    async def stream():
        try:
            _validate_identifier(source_database, "source database")
            _validate_identifier(source_schema, "source schema")
            _validate_identifier(source_table, "source table")
            _validate_identifier(target_database, "target database")
            _validate_identifier(target_schema, "target schema")
            _validate_identifier(target_table, "target table")
            _validate_identifier(key_column, "key column")
            source_cfg = _source_cfg(source_system, env)
            target_cfg = _target_cfg(env)
        except HTTPException as e:
            yield _sse({"type": "error", "message": e.detail})
            yield _sse({"type": "done"})
            return

        q: _queue.Queue = _queue.Queue()
        sentinel = object()
        error_box = [None]
        result_box = [None]
        # Set when the client disconnects (tab closed, navigated away, Cancel
        # pressed). Previously nothing watched for this: the browser stopped
        # listening but the worker thread kept running every remaining stage
        # against the warehouse, so a big run couldn't be called off once
        # started. Cancellation is cooperative — an in-flight query still has to
        # finish, but no further stage begins.
        cancel_event = threading.Event()

        def worker():
            try:
                def progress_cb(pct, label):
                    if cancel_event.is_set():
                        raise _RunCancelled()
                    q.put(("progress", pct, label))

                result = _run_comparison(
                    source_system,
                    _empty_or_none(source_database), _empty_or_none(source_schema), source_table,
                    target_database, target_schema, target_table,
                    source_cfg, target_cfg, key_column, sample_limit, progress_cb,
                )
                if cancel_event.is_set():
                    return
                result_box[0] = result
            except _RunCancelled:
                logger.info(
                    f"IT check cancelled by client: {source_system} "
                    f"{_qualified(source_database, source_schema, source_table)}"
                )
            except Exception as exc:
                logger.error(f"IT check failed: {exc}", exc_info=True)
                error_box[0] = str(exc)
            finally:
                q.put((sentinel,))

        t = threading.Thread(target=worker, daemon=True)
        t.start()

        try:
            yield _sse({"type": "progress", "pct": 0, "message": "Starting IT check…"})
            while True:
                item = await asyncio.get_event_loop().run_in_executor(None, q.get)
                if item[0] is sentinel:
                    break
                _, pct, label = item
                yield _sse({"type": "progress", "pct": pct, "message": label})
                await asyncio.sleep(0)
        except (asyncio.CancelledError, GeneratorExit):
            # Client hung up mid-run. Tell the worker to stop at its next
            # checkpoint and let it wind down on its own — it's a daemon thread
            # holding its own connections, which it closes in its finally block.
            cancel_event.set()
            raise

        # Bounded join: the worker always puts the sentinel in a finally, so
        # reaching here means it's finishing. Don't block the event loop worker
        # forever if a driver wedges on disconnect.
        t.join(timeout=30)
        if t.is_alive():
            logger.warning("IT check worker still running after result was delivered; leaving it to finish.")

        if error_box[0]:
            yield _sse({"type": "error", "message": error_box[0]})
        elif result_box[0]:
            yield _sse({"type": "result", "result": result_box[0]})

        yield _sse({"type": "done"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Downloadable report
# ─────────────────────────────────────────────────────────────────────────────
def _qualified(database: Optional[str], schema: Optional[str], table: str) -> str:
    parts = [p for p in (database, schema, table) if p]
    return ".".join(parts)


def _agg_summary(result: dict):
    """Aggregates are the only full-table row comparison in the report, so make
    it obvious when they didn't run rather than showing a bare blank."""
    agg = result.get("aggregate_checks") or {}
    if agg.get("skipped", True):
        return f"Not run — {agg.get('skip_reason') or 'not evaluated'}"
    if agg.get("match"):
        return f"Yes — {agg.get('checked_columns', 0)} column(s), all rows"
    return f"No — {agg.get('mismatch_count', 0)} aggregate difference(s) across all rows"


def _build_report_xlsx(result: dict) -> bytes:
    out = io.BytesIO()
    wb = xlsxwriter.Workbook(out, {"in_memory": True})
    bold = wb.add_format({"bold": True})
    header_fmt = wb.add_format({"bold": True, "bg_color": "#312C2A", "font_color": "white"})
    pass_fmt = wb.add_format({"bg_color": "#e7f5e0", "font_color": "#328714", "bold": True})
    fail_fmt = wb.add_format({"bg_color": "#fbe6e6", "font_color": "#9e0000", "bold": True})

    source_ref = _qualified(result.get("source_database"), result.get("source_schema"), result["source_table"])
    target_ref = _qualified(result.get("target_database"), result.get("target_schema"), result["target_table"])

    # Summary sheet
    ws = wb.add_worksheet("Summary")
    ws.write(0, 0, "IT Check Report", bold)
    status_fmt = pass_fmt if result["overall_status"] == "PASSED" else fail_fmt
    rows = [
        ("Source System", result["source_system"]),
        ("Source Table", source_ref),
        ("Target Table", target_ref),
        ("Match Method", "Key column: " + result["key_column"] if result.get("key_column") else "Full row-by-row (no key column)"),
        ("Key Column Source", "Auto-detected" if result.get("key_column_auto") else ("User-specified" if result.get("key_column") else "N/A")),
        ("Key Column Issue", result.get("key_column_issue") or "None"),
        ("Started At", result["started_at"]),
        ("Completed At", result["completed_at"]),
        ("Duration (s)", result["duration_seconds"]),
        ("Overall Status", result["overall_status"]),
        ("Source Row Count", result["row_count"]["source"]),
        ("Target Row Count", result["row_count"]["target"]),
        ("Row Count Match", result["row_count"]["match"]),
        ("Schema Match", result["schema_comparison"]["match"]),
        ("Data Type Match", result["data_type_validation"]["match"]),
        ("Null Checks Match", result["null_checks"]["match"]),
        ("Duplicate Checks Match", result["duplicate_checks"]["match"]),
        ("Full-Table Aggregate Match", _agg_summary(result)),
        ("Missing Records (sampled)", result["missing_records"]["count"]),
        ("Extra Records (sampled)", result["extra_records"]["count"]),
        ("Data Mismatches (sampled)",
         "N/A — no key column" if result["data_mismatches"]["not_applicable"] else result["data_mismatches"]["count"]),
        ("Sampled Match Percentage",
         "N/A — skipped" if result["additional_metrics"]["match_percentage"] is None
         else result["additional_metrics"]["match_percentage"]),
    ]
    r = 2
    for label, val in rows:
        ws.write(r, 0, label, bold)
        if label == "Overall Status":
            ws.write(r, 1, val, status_fmt)
        else:
            ws.write(r, 1, val)
        r += 1

    if result["issues"]:
        r += 1
        ws.write(r, 0, "Issues Found", bold)
        r += 1
        for issue in result["issues"]:
            ws.write(r, 0, issue)
            r += 1
    ws.set_column(0, 0, 28)
    ws.set_column(1, 1, 50)

    # Column comparison sheet
    ws2 = wb.add_worksheet("Column Comparison")
    headers = ["Column", "Source Type", "Target Type", "Type Match", "Source Nulls", "Target Nulls", "Nulls Match"]
    for c, h in enumerate(headers):
        ws2.write(0, c, h, header_fmt)
    for i, col in enumerate(result["null_checks"]["columns"], start=1):
        ws2.write(i, 0, col["column"])
        ws2.write(i, 1, col.get("source_type", ""))
        ws2.write(i, 2, col.get("target_type", ""))
        ws2.write(i, 3, col.get("type_match", ""))
        ws2.write(i, 4, col["source_nulls"])
        ws2.write(i, 5, col["target_nulls"])
        ws2.write(i, 6, col["match"])
    ws2.set_column(0, 2, 24)

    # Data type mismatches
    if result["data_type_validation"]["mismatches"]:
        ws3 = wb.add_worksheet("Data Type Mismatches")
        for c, h in enumerate(["Column", "Source Type", "Target Type"]):
            ws3.write(0, c, h, header_fmt)
        for i, m in enumerate(result["data_type_validation"]["mismatches"], start=1):
            ws3.write(i, 0, m["column"])
            ws3.write(i, 1, m["source_type"])
            ws3.write(i, 2, m["target_type"])
        ws3.set_column(0, 2, 24)

    # Missing records sample
    if result["missing_records"]["sample"]:
        ws4 = wb.add_worksheet("Missing Records (sample)")
        sample = result["missing_records"]["sample"]
        cols = list(sample[0].keys())
        for c, h in enumerate(cols):
            ws4.write(0, c, h, header_fmt)
        for i, row in enumerate(sample, start=1):
            for c, h in enumerate(cols):
                ws4.write(i, c, str(row.get(h, "")))
        ws4.set_column(0, len(cols), 20)

    # Extra records sample (present in target, not found in source)
    if result["extra_records"]["sample"]:
        ws4b = wb.add_worksheet("Extra Records (sample)")
        sample = result["extra_records"]["sample"]
        cols = list(sample[0].keys())
        for c, h in enumerate(cols):
            ws4b.write(0, c, h, header_fmt)
        for i, row in enumerate(sample, start=1):
            for c, h in enumerate(cols):
                ws4b.write(i, c, str(row.get(h, "")))
        ws4b.set_column(0, len(cols), 20)

    # Data mismatches sample (only meaningful in key-based mode)
    if not result["data_mismatches"]["not_applicable"] and result["data_mismatches"]["sample"]:
        ws5 = wb.add_worksheet("Data Mismatches (sample)")
        for c, h in enumerate(["Key", "Column", "Source Value", "Target Value"]):
            ws5.write(0, c, h, header_fmt)
        i = 1
        for m in result["data_mismatches"]["sample"]:
            for col in m["mismatched_columns"]:
                ws5.write(i, 0, str(m["key"]))
                ws5.write(i, 1, col)
                ws5.write(i, 2, str(m["source_row"].get(col, "")))
                ws5.write(i, 3, str(m["target_row"].get(col, "")))
                i += 1
        ws5.set_column(0, 3, 24)

    wb.close()
    out.seek(0)
    return out.read()


@data_validation_router.post("/prepare-download")
def prepare_download(result: ValidationResultIn, auth: dict = Depends(require_permission("viewDataValidation"))):
    """Accepts the result JSON the frontend received from /run and returns a
    short-lived, owner-scoped token for /api/data-validation/download-report."""
    _purge_expired_reports()
    result_dict = result.model_dump() if hasattr(result, "model_dump") else result.dict()
    try:
        file_bytes = _build_report_xlsx(result_dict)
    except Exception as e:
        logger.error(f"Error building IT check report: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not build report: {e}")

    token = str(uuid.uuid4())
    try:
        _store_report(token, file_bytes, auth["user"]["email"])
    except OSError as e:
        logger.error(f"Could not spool report to {_REPORT_SPOOL}: {e}")
        raise HTTPException(status_code=500, detail="Could not stage report for download.")
    return {"token": token}


@data_validation_router.get("/download-report")
def download_report(token: str = Query(...), auth: dict = Depends(require_permission("viewDataValidation"))):
    _purge_expired_reports()
    # Same 404 for "doesn't exist" and "exists but belongs to someone else" —
    # don't let the error message confirm whether a given token is valid for
    # another user.
    data = _take_report(token, auth["user"]["email"])
    if data is None:
        raise HTTPException(status_code=404, detail="Report not found or token expired. Please regenerate.")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=it_check_report.xlsx"},
    )