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

nipo_router = APIRouter(prefix="/api/nipo", tags=["Nipo"])

# Wired in from app.py via set_nipo_dependencies() with the SAME
# 'postgres_query_runner' section of dq_config.json used by epod.py / haq.py -
# no dedicated 'nipo' config block. Database overridden to 'regulatory' since
# rimdocs_nipo lives there, same as epod_extracts_core.
_config: Optional[Dict[str, Any]] = None

DATABASE = "regulatory"
SCHEMA = "rimdocs_nipo"
TABLE = f"{SCHEMA}.doc_chunks"

# rimdocs_nipo has NO ingestion_log table - doc_chunks (~294M rows, one row
# per chunk) is the only table, so this dashboard is built entirely off it,
# grouped by document_id to get one "document" per group. There's no
# materialized doc-level table backing this, so every summary/chart/table
# query below scans and groups the full 294M rows - slow compared to the
# other explorers. Worth a materialized view refreshed on a schedule if this
# gets slow in practice; not built here since nobody asked for it yet.
CHUNK_COLUMNS = [
    "id", "document_id", "filename", "title", "introduction", "indication",
    "indication_code", "application_type", "application_number",
    "protocol_number", "chunk_id", "chunk_text", "chunk_number",
    "chunk_metadata", "embedding_status", "embedding_error", "created_at",
]

# One row per document_id - what the data table and CSV download show.
DOC_COLUMNS = [
    "document_id", "filename", "title", "indication", "indication_code",
    "application_type", "application_number", "protocol_number",
    "chunk_count", "first_created_at", "last_created_at",
]

# Filtered on the raw chunk rows, before grouping into documents.
FILTERABLE_COLUMNS = ["indication", "application_type", "embedding_status"]
SEARCHABLE_COLUMNS = ["filename", "title", "protocol_number", "document_id"]

# Label the facet rail shows for rows where the column is NULL or blank.
# It's a real, selectable filter value, so the WHERE builder and the facet
# query have to agree on it exactly - see _facet_value_sql below.
UNSPECIFIED = "(unspecified)"


def _facet_value_sql(col: str, agg: Optional[str] = None) -> str:
    """Normalized value for a filterable column: uppercased, with NULL and
    blank both collapsing to the UNSPECIFIED sentinel. `agg` wraps the raw
    column in an aggregate (MIN) when we're grouping by document first."""
    raw = f'"{col}"'
    if agg:
        raw = f"{agg}({raw})"
    return f"COALESCE(NULLIF(UPPER({raw}), ''), '{UNSPECIFIED}')"

MAX_PAGE_SIZE = 250
DOWNLOAD_CHUNK_ROWS = 5_000

DOC_AGG_SELECT = """
    document_id,
    MIN(filename) AS filename,
    MIN(title) AS title,
    MIN(indication) AS indication,
    MIN(indication_code) AS indication_code,
    MIN(application_type) AS application_type,
    MIN(application_number) AS application_number,
    MIN(protocol_number) AS protocol_number,
    COUNT(*) AS chunk_count,
    MIN(created_at) AS first_created_at,
    MAX(created_at) AS last_created_at
"""


def set_nipo_dependencies(config: Optional[Dict[str, Any]]):
    """Called once from app.py with the 'postgres_query_runner' section of
    dq_config.json - not a dedicated 'nipo' section."""
    global _config
    _config = config
    if not _config:
        logger.warning("⚠️ postgres_query_runner config not found in dq_config.json - Nipo explorer endpoints will be unavailable")


def _require_config() -> Dict[str, Any]:
    if not _config:
        raise HTTPException(
            status_code=503,
            detail="Nipo explorer is not configured - missing 'postgres_query_runner' section in dq_config.json",
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
            application_name="dqe_nipo_explorer",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Nipo database connection failed: {e}")
        raise HTTPException(status_code=502, detail="Could not connect to the data source. Please try again later.")


# ============================================================================
# Warm, TTL-refreshed connection - same shape as the other explorer modules.
# ============================================================================

class _NipoConnectionManager:
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
                    logger.info("Nipo connection stale, reconnecting")
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = _connect()
            self._conn.autocommit = True
            self._created_at = time.time()
            return self._conn


_conn_manager = _NipoConnectionManager()


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


def _parse_filters(filters: Optional[str]) -> Dict[str, List[str]]:
    """Same 'COL:val1||val2,COL2:val3' shape used by epod.py / haq.py."""
    parsed: Dict[str, List[str]] = {}
    if not filters:
        return parsed
    for part in filters.split(","):
        if ":" not in part:
            continue
        col, _, vals = part.partition(":")
        col = col.strip()
        if col not in FILTERABLE_COLUMNS or not vals:
            continue
        parsed[col] = [v for v in vals.split("||") if v != ""]
    return parsed


def _build_where(filters: Dict[str, List[str]], search: Dict[str, str]) -> tuple[str, list]:
    """Filters/search apply to raw chunk rows, BEFORE grouping by
    document_id - e.g. embedding_status:FAILED keeps only the failed chunks
    of a document."""
    clauses = []
    params: list = []

    for col, vals in filters.items():
        if not vals:
            continue
        # Compare against the same normalized expression the facet rail was
        # built from, otherwise picking "(unspecified)" sends the literal
        # string to the DB and matches nothing - NULL is not '(UNSPECIFIED)'.
        placeholders = ", ".join(["%s"] * len(vals))
        clauses.append(f"{_facet_value_sql(col)} IN ({placeholders})")
        params.extend([
            UNSPECIFIED if v.strip().lower() == UNSPECIFIED else v.upper()
            for v in vals
        ])

    for col, term in search.items():
        if col not in SEARCHABLE_COLUMNS or not term:
            continue
        if col == "document_id":
            clauses.append('CAST("document_id" AS TEXT) ILIKE %s')
        else:
            clauses.append(f'"{col}" ILIKE %s')
        params.append(f"%{term}%")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


# ============================================================================
# Endpoints
# ============================================================================

@nipo_router.get("/summary")
def nipo_summary():
    """KPI strip for the Nipo dashboard - one pass over doc_chunks."""
    rows = _run(f"""
        SELECT
            COUNT(DISTINCT document_id) AS total_documents,
            COUNT(DISTINCT indication) AS distinct_indications,
            COUNT(DISTINCT protocol_number) AS distinct_protocols
        FROM {TABLE}
    """)
    return rows[0] if rows else {}


@nipo_router.get("/charts/by-indication")
def nipo_by_indication_chart(limit: int = Query(10, ge=1, le=50)):
    """Document count grouped by indication."""
    return {"rows": _run(f"""
        SELECT {_facet_value_sql('indication')} AS indication, COUNT(DISTINCT document_id) AS count
        FROM {TABLE}
        GROUP BY 1
        ORDER BY count DESC
        LIMIT %s
    """, [limit])}


@nipo_router.get("/charts/by-application-type")
def nipo_by_application_type_chart(limit: int = Query(10, ge=1, le=50)):
    """Document count grouped by application_type."""
    return {"rows": _run(f"""
        SELECT {_facet_value_sql('application_type')} AS application_type, COUNT(DISTINCT document_id) AS count
        FROM {TABLE}
        GROUP BY 1
        ORDER BY count DESC
        LIMIT %s
    """, [limit])}


@nipo_router.get("/facets")
def nipo_facets(filters: Optional[str] = Query(None, description="col:val1||val2,col2:val3")):
    """Distinct-value counts for the filter rail (indication, application_type,
    embedding_status).

    Counts have to be produced the same way /data produces its rows or the
    rail lies: group the filtered chunks into documents, drop any document
    with a FAILED chunk, take MIN(col) per document (same pick the table
    shows), then count documents. Counting raw chunk rows here instead is
    what made the rail show 248 next to a table that returned nothing.
    """
    parsed = _parse_filters(filters)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for col in FILTERABLE_COLUMNS:
        other_filters = {k: v for k, v in parsed.items() if k != col}
        where_sql, params = _build_where(other_filters, {})
        rows = _run(
            f"""
            SELECT value, COUNT(*) AS count
            FROM (
                SELECT {_facet_value_sql(col, agg='MIN')} AS value
                FROM {TABLE}
                {where_sql}
                GROUP BY document_id
                HAVING COUNT(*) FILTER (WHERE UPPER(embedding_status) = 'FAILED') = 0
            ) d
            GROUP BY value
            ORDER BY count DESC
            """,
            params,
        )
        out[col] = rows
    return out


@nipo_router.get("/data")
def nipo_data(
    filters: Optional[str] = Query(None),
    search_filename: str = Query(""),
    search_title: str = Query(""),
    search_protocol_number: str = Query(""),
    search_document_id: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    """One row per document_id, aggregated from doc_chunks. Documents with
    at least one FAILED chunk are excluded entirely."""
    parsed = _parse_filters(filters)
    search = {
        "filename": search_filename or "",
        "title": search_title or "",
        "protocol_number": search_protocol_number or "",
        "document_id": search_document_id or "",
    }
    where_sql, params = _build_where(parsed, search)

    total = _run(
        f"""
        SELECT COUNT(*) AS c FROM (
            SELECT document_id
            FROM {TABLE}
            {where_sql}
            GROUP BY document_id
            HAVING COUNT(*) FILTER (WHERE UPPER(embedding_status) = 'FAILED') = 0
        ) sub
        """, params
    )[0]["c"]

    sortable = set(DOC_COLUMNS)
    order_col = sort_col if sort_col in sortable else "last_created_at"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    offset = (page - 1) * page_size

    rows = _run(
        f"""
        SELECT {DOC_AGG_SELECT}
        FROM {TABLE}
        {where_sql}
        GROUP BY document_id
        HAVING COUNT(*) FILTER (WHERE UPPER(embedding_status) = 'FAILED') = 0
        ORDER BY "{order_col}" {order_dir} NULLS LAST
        LIMIT %s OFFSET %s
        """,
        params + [page_size, offset],
    )

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


@nipo_router.get("/data/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def nipo_data_download(
    filters: Optional[str] = Query(None),
    search_filename: str = Query(""),
    search_title: str = Query(""),
    search_protocol_number: str = Query(""),
    search_document_id: str = Query(""),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    """Streams every matching document (not just the page on screen) as CSV.
    Documents with at least one FAILED chunk are excluded entirely."""
    parsed = _parse_filters(filters)
    search = {
        "filename": search_filename or "",
        "title": search_title or "",
        "protocol_number": search_protocol_number or "",
        "document_id": search_document_id or "",
    }
    where_sql, params = _build_where(parsed, search)

    sortable = set(DOC_COLUMNS)
    order_col = sort_col if sort_col in sortable else "last_created_at"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"

    sql = f"""
        SELECT {DOC_AGG_SELECT}
        FROM {TABLE}
        {where_sql}
        GROUP BY document_id
        HAVING COUNT(*) FILTER (WHERE UPPER(embedding_status) = 'FAILED') = 0
        ORDER BY "{order_col}" {order_dir} NULLS LAST
    """

    def generate():
        conn = _connect()
        conn.autocommit = True
        try:
            with conn.cursor(name="nipo_download_cursor") as cur:
                cur.itersize = DOWNLOAD_CHUNK_ROWS
                cur.execute(sql, params)
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(DOC_COLUMNS)
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
        headers={"Content-Disposition": 'attachment; filename="nipo_documents.csv"'},
    )


@nipo_router.get("/data/{document_id}")
def nipo_data_detail(document_id: str, chunk_limit: int = Query(50, ge=1, le=500)):
    """Document-level aggregate plus a page of its chunks (row-click detail
    panel). chunk_text is included but chunk_vector never is - it's a raw
    embedding array, useless in the UI and heavy to ship over the wire."""
    doc_rows = _run(
        f'SELECT {DOC_AGG_SELECT} FROM {TABLE} WHERE "document_id" = %s GROUP BY document_id',
        [document_id],
    )
    if not doc_rows:
        raise HTTPException(status_code=404, detail="Document not found")

    chunks = _run(
        f"""
        SELECT chunk_id, chunk_number, chunk_text, embedding_status, embedding_error, created_at
        FROM {TABLE}
        WHERE "document_id" = %s
        ORDER BY chunk_number ASC
        LIMIT %s
        """,
        [document_id, chunk_limit],
    )

    doc = doc_rows[0]
    doc["chunks"] = chunks
    return doc