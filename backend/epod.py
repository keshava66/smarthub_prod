from __future__ import annotations

import csv
import io
import logging
import threading
import time
from decimal import Decimal
from datetime import date, datetime, time as time_, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from auth import require_permission
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

epod_router = APIRouter(prefix="/api/epod", tags=["EPOD"])

# Wired in from app.py via set_epod_dependencies() with the SAME
# 'postgres_query_runner' section of dq_config.json used by
# postgres_query_runner.py - no separate 'epod' config block, no PGQR_* env
# var fallback. Only the database name is overridden below since these
# tables live on 'regulatory', not whatever default_database the query
# runner points at.
_config: Optional[Dict[str, Any]] = None

DATABASE = "regulatory"
SCHEMA = "epod_extracts_core"

# ingestion_log columns pulled in via the join, aliased so they never
# collide with a same-named doc_chunks column (both tables have doc_id,
# file_name, file_path, updated_at).
LOG_COLUMN_ALIASES = {
    "file_size": "doc_file_size",
    "file_hash": "doc_file_hash",
    "last_modified": "doc_last_modified",
    "ingested_at": "doc_ingested_at",
    "updated_at": "doc_log_updated_at",
    "status": "ingestion_status",
    "error_message": "ingestion_error_message",
    "retry_count": "retry_count",
    "chunk_count": "log_chunk_count",
    "pages_total": "log_pages_total",
}

# Every doc_chunks column except chunk_vector, which is handled separately
# below.
CHUNK_COLUMNS = [
    "chunk_id", "doc_id", "file_name", "file_path",
    "chunk_index", "chunk_total", "chunk_level", "section_title",
    "page_start", "page_end", "content_types", "chunk_text",
    "created_at", "updated_at",
]

# chunk_vector is a pgvector embedding - typically 1536 floats, several KB of
# text per row, and unreadable as a grid cell. Shipping it whole would make a
# 50-row page tens of megabytes, so the SELECT derives two small stand-ins
# instead: how many dimensions the embedding has (or NULL when it was never
# generated), and a short prefix of its text form so the actual values are
# still inspectable from the UI.
VECTOR_PREVIEW_CHARS = 160
VECTOR_DERIVED_COLUMNS = ["chunk_vector_dims", "chunk_vector_preview"]
VECTOR_SELECT_SQL = (
    'vector_dims(c."chunk_vector") AS "chunk_vector_dims", '
    f'LEFT(c."chunk_vector"::text, {VECTOR_PREVIEW_CHARS}) AS "chunk_vector_preview"'
)

ALL_COLUMNS = CHUNK_COLUMNS + VECTOR_DERIVED_COLUMNS + list(LOG_COLUMN_ALIASES.values())

ARRAY_FILTER_SQL = {"content_types": 'c."content_types"'}
SEARCH_SQL = {
    "file_name": 'c."file_name"',
    "section_title": 'c."section_title"',
    "chunk_text": 'c."chunk_text"',
    "doc_id": 'c."doc_id"',
    "file_path": 'c."file_path"',
    "chunk_id": 'c."chunk_id"::text',
}
# Only valid on datasets that have an ingestion_log - _build_where drops any
# key that isn't in the map it's handed, so this stays out of SEARCH_SQL.
LOG_SEARCH_SQL = {
    "ingestion_error_message": 'l."error_message"',
}

MAX_PAGE_SIZE = 250
DOWNLOAD_CHUNK_ROWS = 5_000


def set_epod_dependencies(config: Optional[Dict[str, Any]]):
    """Called once from app.py with the 'postgres_query_runner' section of
    dq_config.json - not a dedicated 'epod' section, and no env var
    fallback of any kind."""
    global _config
    _config = config
    if not _config:
        logger.warning("⚠️ postgres_query_runner config not found in dq_config.json - EPOD explorer endpoints will be unavailable")


def _require_config() -> Dict[str, Any]:
    if not _config:
        raise HTTPException(
            status_code=503,
            detail="EPOD explorer is not configured - missing 'postgres_query_runner' section in dq_config.json",
        )
    return {
        "host": _config.get("host"),
        "port": int(_config.get("port") or 5432),
        "user": _config.get("user"),
        "password": _config.get("password") or "",
        "database": DATABASE,
        "sslmode": _config.get("sslmode") or "prefer",
        "connect_timeout": int(_config.get("connect_timeout_seconds") or 10),
    }


def _connect():
    import psycopg2

    cfg = _require_config()
    try:
        return psycopg2.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            dbname=cfg["database"],
            sslmode=cfg["sslmode"],
            connect_timeout=cfg["connect_timeout"],
            application_name="dqe_epod_explorer",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"EPOD database connection failed: {e}")
        raise HTTPException(status_code=502, detail="Could not connect to the data source. Please try again later.")


# ============================================================================
# Warm, TTL-refreshed connection - same shape as the HAQ / query runner explorers.
# ============================================================================

class _EpodConnectionManager:
    CONNECTION_TTL = 2 * 60 * 60  # 2 hours

    def __init__(self):
        self._conn = None
        self._created_at = 0.0
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            expired = (time.time() - self._created_at) > self.CONNECTION_TTL
            if self._conn is not None and not expired:
                try:
                    cur = self._conn.cursor()
                    cur.execute("SELECT 1")
                    cur.close()
                    return self._conn
                except Exception:
                    logger.info("EPOD connection stale, reconnecting")
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = _connect()
            self._conn.autocommit = True
            self._created_at = time.time()
            return self._conn


_conn_manager = _EpodConnectionManager()


def _to_json_safe(val):
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, (datetime, date, time_)):
        return val.isoformat()
    if isinstance(val, timedelta):
        return str(val)
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(val).hex()
    if isinstance(val, (list, tuple)):
        return [_to_json_safe(v) for v in val]
    if isinstance(val, float):
        if val != val or val in (float("inf"), float("-inf")):
            return None
        return val
    if isinstance(val, (int, str)):
        return val
    try:
        return str(val)
    except Exception:
        return None


def _run(sql: str, params: Optional[list] = None) -> List[Dict[str, Any]]:
    conn = _conn_manager.get()
    with conn.cursor() as cur:
        cur.execute(sql, params or [])
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall() if cur.description else []
    return [{cols[i]: _to_json_safe(r[i]) for i in range(len(cols))} for r in rows]


# ============================================================================
# Dataset discovery - the person's Postgres has one doc_chunks_<suffix> /
# ingestion_log_<suffix> pair per document set (EDQM, NDA, BLA, ...), plus
# the base 'doc_chunks' / 'ingestion_log'. We discover these from
# information_schema rather than hardcoding a list, since the exact set
# changes over time and there's no guarantee every doc_chunks_<suffix>
# has a matching ingestion_log_<suffix> (some don't, going by what's on
# disk today) - datasets without a log table still work, just without the
# ingestion-log-derived KPIs/charts.
# ============================================================================

_dataset_cache: Dict[str, Any] = {"items": None, "ts": 0.0}
_DATASET_CACHE_TTL = 60  # seconds

# doc_chunks_<suffix> tables that physically exist in epod_extracts_core but
# should never show up in the dataset picker - backups kept around on disk
# and a one-off extract, none of them meant for day-to-day exploration.
EXCLUDED_DATASET_SUFFIXES = {"ind_3yrs_bkp", "ind_bkp", "subex"}


def _discover_datasets(force: bool = False) -> List[Dict[str, Any]]:
    now = time.time()
    if not force and _dataset_cache["items"] is not None and (now - _dataset_cache["ts"]) < _DATASET_CACHE_TTL:
        return _dataset_cache["items"]

    chunk_rows = _run(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name LIKE 'doc\\_chunks%%' ESCAPE '\\' "
        "ORDER BY table_name",
        [SCHEMA],
    )
    log_rows = _run(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name LIKE 'ingestion\\_log%%' ESCAPE '\\'",
        [SCHEMA],
    )
    log_table_names = {r["table_name"] for r in log_rows}

    items: List[Dict[str, Any]] = []
    for r in chunk_rows:
        t = r["table_name"]
        if t == "doc_chunks":
            suffix, id_, label = "", "default", "CTA"
        elif t.startswith("doc_chunks_"):
            suffix = t[len("doc_chunks_"):]
            if suffix in EXCLUDED_DATASET_SUFFIXES:
                continue
            id_ = suffix
            label = suffix.replace("_", " ").upper()
        else:
            continue
        log_name = "ingestion_log" if suffix == "" else f"ingestion_log_{suffix}"
        has_log = log_name in log_table_names
        items.append({
            "id": id_,
            "label": label,
            "chunks_table": f"{SCHEMA}.{t}",
            "log_table": f"{SCHEMA}.{log_name}" if has_log else None,
            "has_log": has_log,
        })

    items.sort(key=lambda d: (d["id"] != "default", d["label"]))
    _dataset_cache["items"] = items
    _dataset_cache["ts"] = now
    return items


def _resolve_dataset(dataset_id: Optional[str]) -> Dict[str, Any]:
    items = _discover_datasets()
    if not items:
        raise HTTPException(status_code=503, detail="No datasets are available right now.")
    if not dataset_id:
        return items[0]
    for d in items:
        if d["id"] == dataset_id:
            return d
    raise HTTPException(status_code=404, detail=f"Unknown EPOD dataset '{dataset_id}'")


def _join_sql(ds: Dict[str, Any]) -> str:
    if ds["log_table"]:
        return f'FROM {ds["chunks_table"]} c LEFT JOIN {ds["log_table"]} l ON c."doc_id" = l."doc_id"'
    return f'FROM {ds["chunks_table"]} c'


def _select_columns_sql(ds: Dict[str, Any]) -> str:
    parts = [f'c."{col}"' for col in CHUNK_COLUMNS]
    parts.append(VECTOR_SELECT_SQL)
    if ds["has_log"]:
        parts += [f'l."{log_col}" AS "{alias}"' for log_col, alias in LOG_COLUMN_ALIASES.items()]
    else:
        parts += [f'NULL AS "{alias}"' for alias in LOG_COLUMN_ALIASES.values()]
    return ", ".join(parts)


def _filter_sql_map(ds: Dict[str, Any]) -> Dict[str, str]:
    m = {"chunk_level": 'c."chunk_level"'}
    if ds["has_log"]:
        m["ingestion_status"] = 'l."status"'
        # Small bounded ints (0,1,2...) - useful as a checkbox facet for
        # finding documents that needed retries.
        m["retry_count"] = 'l."retry_count"'
    return m


def _order_sql_map(ds: Dict[str, Any]) -> Dict[str, str]:
    """One entry per column the grid can show, so every header is clickable.
    chunk_text and content_types are deliberately absent - sorting a body of
    text or a text[] isn't meaningful."""
    m = {
        "chunk_id": 'c."chunk_id"',
        "file_name": 'c."file_name"', "doc_id": 'c."doc_id"', "file_path": 'c."file_path"',
        "chunk_index": 'c."chunk_index"',
        "chunk_total": 'c."chunk_total"', "chunk_level": 'c."chunk_level"', "section_title": 'c."section_title"',
        "page_start": 'c."page_start"', "page_end": 'c."page_end"',
        "created_at": 'c."created_at"', "updated_at": 'c."updated_at"',
    }
    if ds["has_log"]:
        m.update({
            "doc_file_size": 'l."file_size"', "doc_file_hash": 'l."file_hash"',
            "doc_last_modified": 'l."last_modified"', "doc_ingested_at": 'l."ingested_at"',
            "doc_log_updated_at": 'l."updated_at"',
            "ingestion_status": 'l."status"', "ingestion_error_message": 'l."error_message"',
            "retry_count": 'l."retry_count"',
            "log_chunk_count": 'l."chunk_count"', "log_pages_total": 'l."pages_total"',
        })
    return m


def _parse_filters(filters: Optional[str]) -> Dict[str, List[str]]:
    """Same 'COL:val1||val2,COL2:val3' shape used by unstructured.py / haq.py.
    Not validated against a specific dataset's columns here - _build_where
    silently drops anything that isn't in the maps it's given, since which
    columns are valid depends on whether this dataset has a log table."""
    parsed: Dict[str, List[str]] = {}
    if not filters:
        return parsed
    for part in filters.split(","):
        if ":" not in part:
            continue
        col, _, vals = part.partition(":")
        col = col.strip()
        if not col or not vals:
            continue
        parsed[col] = [v for v in vals.split("||") if v != ""]
    return parsed


# Label the facet rail shows for rows where a column is NULL or blank. It's a
# real, selectable value, so the facet SELECT and the WHERE builder have to
# derive it from the same expression - comparing a bare column against the
# literal '(unspecified)' never matches NULL, so picking it returned nothing.
UNSPECIFIED = "(unspecified)"


def _facet_expr(col_sql: str) -> str:
    """Normalized facet value: trimmed, with NULL and blank folding into the
    UNSPECIFIED sentinel. Case is left alone - epod facet labels are shown
    as stored and matched exactly."""
    return f"COALESCE(NULLIF(TRIM({col_sql}::text), ''), '{UNSPECIFIED}')"


def _build_where(filters: Dict[str, List[str]], search: Dict[str, str], filter_sql_map: Dict[str, str],
                 has_log: bool = False) -> tuple[str, list]:
    """`has_log` unlocks the ingestion_log-only search fields; without the
    join those columns don't exist, so they're dropped rather than erroring."""
    clauses = []
    params: list = []
    search_sql = {**SEARCH_SQL, **(LOG_SEARCH_SQL if has_log else {})}

    for col, vals in filters.items():
        if not vals:
            continue
        if col in ARRAY_FILTER_SQL:
            # content_types is text[] - match rows whose array overlaps any
            # of the selected values.
            clauses.append(f'{ARRAY_FILTER_SQL[col]} && %s')
            params.append(vals)
        elif col in filter_sql_map:
            placeholders = ", ".join(["%s"] * len(vals))
            clauses.append(f'{_facet_expr(filter_sql_map[col])} IN ({placeholders})')
            params.extend(vals)
        # else: column not valid for this dataset (e.g. ingestion_status
        # filter left over after switching to a dataset with no log table) -
        # silently ignored rather than erroring, since the UI clears it too.

    for col, term in search.items():
        if col not in search_sql or not term:
            continue
        clauses.append(f'{search_sql[col]} ILIKE %s')
        params.append(f"%{term}%")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


def _search_params(file_name: str, section_title: str, chunk_text: str, doc_id: str,
                   file_path: str = "", chunk_id: str = "", ingestion_error_message: str = "") -> Dict[str, str]:
    return {
        "file_name": file_name or "",
        "section_title": section_title or "",
        "chunk_text": chunk_text or "",
        "doc_id": doc_id or "",
        "file_path": file_path or "",
        "chunk_id": chunk_id or "",
        "ingestion_error_message": ingestion_error_message or "",
    }


DatasetParam = Query(None, description="doc_chunks table suffix (e.g. 'edqm', 'nda') - omit for the base doc_chunks table. See GET /datasets.")


# ============================================================================
# Endpoints
# ============================================================================

@epod_router.get("/datasets")
def epod_datasets():
    """Every doc_chunks* table found in epod_extracts_core, for the dataset
    picker next to the Explore/Data table tabs."""
    items = _discover_datasets()
    return {
        "items": [{"id": d["id"], "label": d["label"], "has_log": d["has_log"]} for d in items],
        "default": items[0]["id"] if items else None,
    }


@epod_router.get("/summary")
def epod_summary(dataset: Optional[str] = DatasetParam):
    """Headline numbers for the Explore tab: documents, avg chunks and pages
    per document, last update. Pages come from the log table, so they're
    None for datasets that don't have one."""
    ds = _resolve_dataset(dataset)

    rows = _run(f"""
        SELECT
            COUNT(*) AS total_chunks,
            COUNT(DISTINCT doc_id) AS distinct_documents,
            COUNT(DISTINCT file_name) AS distinct_files,
            MAX(updated_at) AS last_updated
        FROM {ds["chunks_table"]}
    """)
    row = rows[0] if rows else {}
    total = row.pop("total_chunks", 0) or 0
    docs = row.get("distinct_documents") or 0
    row["avg_chunks_per_doc"] = round(total / docs, 1) if docs else 0

    row["avg_pages_per_doc"] = None
    if ds["has_log"]:
        pages = _run(f'SELECT AVG(NULLIF(pages_total, 0)) AS avg_pages FROM {ds["log_table"]}')
        avg = pages[0].get("avg_pages") if pages else None
        row["avg_pages_per_doc"] = round(float(avg), 1) if avg is not None else None

    return {**row, "dataset": ds["id"], "has_log": ds["has_log"]}


@epod_router.get("/charts/chunk-level")
def epod_chunk_level_chart(dataset: Optional[str] = DatasetParam):
    """Chunk count grouped by chunk_level (section / split / page / ...)."""
    ds = _resolve_dataset(dataset)
    return {"rows": _run(f"""
        SELECT COALESCE(chunk_level, '(unspecified)') AS chunk_level, COUNT(*) AS count
        FROM {ds["chunks_table"]}
        GROUP BY chunk_level
        ORDER BY count DESC
    """)}


@epod_router.get("/charts/content-types")
def epod_content_types_chart(dataset: Optional[str] = DatasetParam):
    """Chunk count grouped by each content type present in the content_types array."""
    ds = _resolve_dataset(dataset)
    return {"rows": _run(f"""
        SELECT COALESCE(content_type, '(none)') AS content_type, COUNT(*) AS count
        FROM {ds["chunks_table"]}
        CROSS JOIN LATERAL unnest(content_types) AS content_type
        GROUP BY content_type
        ORDER BY count DESC
    """)}


@epod_router.get("/charts/top-documents")
def epod_top_documents_chart(dataset: Optional[str] = DatasetParam, limit: int = Query(12, ge=1, le=50)):
    """Documents with the most chunks - drives the ranked-bars card."""
    ds = _resolve_dataset(dataset)
    return {"rows": _run(f"""
        SELECT file_name, COUNT(*) AS count
        FROM {ds["chunks_table"]}
        GROUP BY file_name
        ORDER BY count DESC
        LIMIT %s
    """, [limit])}


@epod_router.get("/charts/chunks-over-time")
def epod_chunks_over_time_chart(dataset: Optional[str] = DatasetParam):
    """Chunks created per day - drives the trend line."""
    ds = _resolve_dataset(dataset)
    return {"rows": _run(f"""
        SELECT date_trunc('day', created_at) AS period, COUNT(*) AS count
        FROM {ds["chunks_table"]}
        WHERE created_at IS NOT NULL
        GROUP BY period
        ORDER BY period ASC
    """)}


@epod_router.get("/charts/largest-documents")
def epod_largest_documents_chart(dataset: Optional[str] = DatasetParam, limit: int = Query(10, ge=1, le=50)):
    """Largest source documents by file_size - from ingestion_log, not doc_chunks."""
    ds = _resolve_dataset(dataset)
    if not ds["has_log"]:
        return {"rows": [], "has_log": False}
    return {"rows": _run(f"""
        SELECT file_name, file_size
        FROM {ds["log_table"]}
        WHERE file_size IS NOT NULL
        ORDER BY file_size DESC
        LIMIT %s
    """, [limit]), "has_log": True}


@epod_router.get("/charts/ingestion-volume-over-time")
def epod_ingestion_volume_chart(dataset: Optional[str] = DatasetParam):
    """Documents ingested per day, from ingestion_log.ingested_at."""
    ds = _resolve_dataset(dataset)
    if not ds["has_log"]:
        return {"rows": [], "has_log": False}
    return {"rows": _run(f"""
        SELECT date_trunc('day', ingested_at) AS period, COUNT(*) AS count
        FROM {ds["log_table"]}
        WHERE ingested_at IS NOT NULL
        GROUP BY period
        ORDER BY period ASC
    """), "has_log": True}


@epod_router.get("/facets")
def epod_facets(dataset: Optional[str] = DatasetParam, filters: Optional[str] = Query(None, description="col:val1||val2,col2:val3")):
    """Distinct-value counts for the filter rail (chunk_level, content_types,
    and ingestion_status when this dataset has a log table)."""
    ds = _resolve_dataset(dataset)
    join_sql = _join_sql(ds)
    filter_sql_map = _filter_sql_map(ds)
    parsed = _parse_filters(filters)
    out: Dict[str, List[Dict[str, Any]]] = {}

    for col, col_sql in filter_sql_map.items():
        other_filters = {k: v for k, v in parsed.items() if k != col}
        where_sql, params = _build_where(other_filters, {}, filter_sql_map)
        rows = _run(
            f"SELECT {_facet_expr(col_sql)} AS value, COUNT(*) AS count {join_sql} {where_sql} "
            f"GROUP BY 1 ORDER BY count DESC",
            params,
        )
        out[col] = rows

    for col, col_sql in ARRAY_FILTER_SQL.items():
        other_filters = {k: v for k, v in parsed.items() if k != col}
        where_sql, params = _build_where(other_filters, {}, filter_sql_map)
        rows = _run(
            f"SELECT COALESCE(value, '(none)') AS value, COUNT(*) AS count {join_sql} "
            f"CROSS JOIN LATERAL unnest({col_sql}) AS value {where_sql} "
            f"GROUP BY value ORDER BY count DESC",
            params,
        )
        out[col] = rows

    return out


@epod_router.get("/data")
def epod_data(
    dataset: Optional[str] = DatasetParam,
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_section_title: str = Query(""),
    search_chunk_text: str = Query(""),
    search_doc_id: str = Query(""),
    search_file_path: str = Query(""),
    search_chunk_id: str = Query(""),
    search_ingestion_error_message: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    ds = _resolve_dataset(dataset)
    join_sql = _join_sql(ds)
    filter_sql_map = _filter_sql_map(ds)
    order_sql_map = _order_sql_map(ds)

    parsed = _parse_filters(filters)
    search = _search_params(
        search_file_name, search_section_title, search_chunk_text, search_doc_id,
        search_file_path, search_chunk_id, search_ingestion_error_message,
    )
    where_sql, params = _build_where(parsed, search, filter_sql_map, ds["has_log"])

    total = _run(f"SELECT COUNT(*) AS c {join_sql} {where_sql}", params)[0]["c"]

    order_sql = order_sql_map.get(sort_col) or order_sql_map["file_name"]
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    offset = (page - 1) * page_size
    rows = _run(
        f'SELECT {_select_columns_sql(ds)} {join_sql} {where_sql} '
        f'ORDER BY {order_sql} {order_dir} NULLS LAST, c."chunk_index" ASC NULLS LAST '
        f'LIMIT %s OFFSET %s',
        params + [page_size, offset],
    )

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "dataset": ds["id"],
        "has_log": ds["has_log"],
    }


@epod_router.get("/data/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def epod_data_download(
    dataset: Optional[str] = DatasetParam,
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_section_title: str = Query(""),
    search_chunk_text: str = Query(""),
    search_doc_id: str = Query(""),
    search_file_path: str = Query(""),
    search_chunk_id: str = Query(""),
    search_ingestion_error_message: str = Query(""),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    """Streams every matching row (not just the page on screen) as CSV,
    fetched in chunks so memory stays flat regardless of result size."""
    ds = _resolve_dataset(dataset)
    join_sql = _join_sql(ds)
    filter_sql_map = _filter_sql_map(ds)
    order_sql_map = _order_sql_map(ds)

    parsed = _parse_filters(filters)
    search = _search_params(
        search_file_name, search_section_title, search_chunk_text, search_doc_id,
        search_file_path, search_chunk_id, search_ingestion_error_message,
    )
    where_sql, params = _build_where(parsed, search, filter_sql_map, ds["has_log"])

    order_sql = order_sql_map.get(sort_col) or order_sql_map["file_name"]
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    sql = (
        f'SELECT {_select_columns_sql(ds)} {join_sql} {where_sql} '
        f'ORDER BY {order_sql} {order_dir} NULLS LAST, c."chunk_index" ASC NULLS LAST'
    )
    filename = f'epod_{ds["id"]}_doc_chunks.csv'

    def generate():
        conn = _connect()
        conn.autocommit = True
        try:
            with conn.cursor(name="epod_download_cursor") as cur:
                cur.itersize = DOWNLOAD_CHUNK_ROWS
                cur.execute(sql, params)
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(ALL_COLUMNS)
                yield buf.getvalue()
                while True:
                    batch = cur.fetchmany(DOWNLOAD_CHUNK_ROWS)
                    if not batch:
                        break
                    buf = io.StringIO()
                    writer = csv.writer(buf)
                    for row in batch:
                        writer.writerow(["" if v is None else _to_json_safe(v) for v in row])
                    yield buf.getvalue()
        finally:
            conn.close()

    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@epod_router.get("/data/{chunk_id}")
def epod_data_detail(chunk_id: str, dataset: Optional[str] = DatasetParam):
    """Single chunk, full detail including its document's ingestion_log
    metadata when available (used by the row-click detail panel)."""
    ds = _resolve_dataset(dataset)
    rows = _run(f'SELECT {_select_columns_sql(ds)} {_join_sql(ds)} WHERE c."chunk_id" = %s', [chunk_id])
    if not rows:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return rows[0]