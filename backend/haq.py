from __future__ import annotations

import csv
import io
import logging
import os
import threading
import time
from decimal import Decimal
from datetime import date, datetime, time as time_, timedelta
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from auth import require_permission
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

haq_router = APIRouter(prefix="/api/haq", tags=["HAQ"])

# Wired in from app.py via set_haq_dependencies(). Reuses the same Postgres
# instance as postgres_query_runner (PGQR_* config/env) - just a different
# database on it, so this only needs its own database name, not its own
# host/user/password.
_config: Optional[Dict[str, Any]] = None

TABLE = "haq_vector_embedding.haq_query_question"  # live table - not chunked_haq_query_question (vector chunks) or the _bkp/_backup copies

ALL_COLUMNS = [
    "id", "ignite_question_answer_id", "query_id", "question_number", "question", "answer",
    "is_strategically_important", "is_esi", "is_ssi", "action_taken", "significance", "outcome",
    "r_responding_type_id", "r_not_responding_justification_id", "not_responding_justification_other",
    "created_by", "created_on", "modified_by", "modified_on", "is_active",
]

# Checkbox-style filters (equality / IN)
FILTERABLE_COLUMNS = ["is_strategically_important", "is_esi", "is_ssi", "action_taken", "significance", "outcome", "is_active"]

# Free-text "contains" filters
SEARCHABLE_COLUMNS = ["question", "answer", "question_number"]

MAX_PAGE_SIZE = 250

# Rows pulled per round trip when streaming the full result set to CSV -
# big enough to keep the server-side cursor busy, small enough that the
# response starts flowing straight away.
DOWNLOAD_CHUNK_ROWS = 5_000


def _cfg_value(key: str, env_var: str, default=None):
    if _config and _config.get(key) not in (None, ""):
        return _config[key]
    if os.environ.get(env_var):
        return os.environ[env_var]
    return default


def set_haq_dependencies(config: Optional[Dict[str, Any]]):
    """Called once from app.py with the 'haq' section of dq_config.json."""
    global _config
    _config = config
    if not _config:
        logger.warning("⚠️ haq config not found - reusing PGQR_* postgres connection details with database override")


def _require_config() -> Dict[str, Any]:
    if not _config and not os.environ.get("PGQR_HOST"):
        raise HTTPException(status_code=503, detail="HAQ is not configured - missing PGQR_* postgres connection details")
    return {
        "host": _cfg_value("host", "PGQR_HOST"),
        "port": int(_cfg_value("port", "PGQR_PORT", 5432)),
        "user": _cfg_value("user", "PGQR_USER"),
        "password": _cfg_value("password", "PGQR_PASSWORD", ""),
        "database": _cfg_value("database", "PGQR_DATABASE", "regulatory"),
        "sslmode": _cfg_value("sslmode", "PGQR_SSLMODE", "prefer"),
        "connect_timeout": int(_cfg_value("connect_timeout_seconds", "PGQR_CONNECT_TIMEOUT", 10)),
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
            application_name="dqe_haq_explorer",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not connect to HAQ Postgres database: {e}")


# ============================================================================
# Warm, TTL-refreshed connection - same shape as the other explorer modules.
# ============================================================================

class _HaqConnectionManager:
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
                    logger.info("HAQ connection stale, reconnecting")
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = _connect()
            self._conn.autocommit = True
            self._created_at = time.time()
            return self._conn


_conn_manager = _HaqConnectionManager()


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
    """Same 'COL:val1||val2,COL2:val3' shape used by unstructured.py."""
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


BOOLEAN_COLUMNS = ("is_strategically_important", "is_esi", "is_ssi", "is_active")

# Label the facet rail shows for rows where a column is NULL. It has to be a
# real, selectable value: the rail previously emitted a raw SQL NULL, which
# the UI stringified to "null" and sent back as a filter that matched nothing
# (and rendered as a blank row for the boolean columns, since React skips
# boolean children).
UNSPECIFIED = "(unspecified)"


def _facet_expr(col: str) -> str:
    """Normalized facet value for a filterable column - text for both the
    string and boolean columns, with NULL and blank folding into the
    UNSPECIFIED sentinel."""
    return f"""COALESCE(NULLIF(TRIM("{col}"::text), ''), '{UNSPECIFIED}')"""


def _build_where(filters: Dict[str, List[str]], search: Dict[str, str]) -> tuple[str, list]:
    clauses = []
    params: list = []

    for col, vals in filters.items():
        if not vals:
            continue
        # Both branches compare against _facet_expr so the values the rail
        # handed out match one-for-one, sentinel included. Booleans go
        # through their text form ('true'/'false') for the same reason.
        if col in BOOLEAN_COLUMNS:
            vals = [
                v if v.strip().lower() == UNSPECIFIED
                else ("true" if v.strip().lower() in ("true", "1", "yes") else "false")
                for v in vals
            ]
        placeholders = ", ".join(["%s"] * len(vals))
        clauses.append(f'{_facet_expr(col)} IN ({placeholders})')
        params.extend(vals)

    for col, term in search.items():
        if col not in SEARCHABLE_COLUMNS or not term:
            continue
        clauses.append(f'"{col}" ILIKE %s')
        params.append(f"%{term}%")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


# ============================================================================
# Endpoints
# ============================================================================

@haq_router.get("/summary")
def haq_summary():
    """KPI counts for the top strip: total, active, and the three strategic flags."""
    rows = _run(f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE is_active) AS active,
            COUNT(*) FILTER (WHERE is_strategically_important) AS strategically_important,
            COUNT(*) FILTER (WHERE is_esi) AS esi,
            COUNT(*) FILTER (WHERE is_ssi) AS ssi
        FROM {TABLE}
    """)
    return rows[0] if rows else {"total": 0, "active": 0, "strategically_important": 0, "esi": 0, "ssi": 0}


@haq_router.get("/facets")
def haq_facets(filters: Optional[str] = Query(None, description="col:val1||val2,col2:val3")):
    """Distinct-value counts for each filterable column, for the facet rail."""
    parsed = _parse_filters(filters)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for col in FILTERABLE_COLUMNS:
        other_filters = {k: v for k, v in parsed.items() if k != col}
        where_sql, params = _build_where(other_filters, {})
        rows = _run(
            f'SELECT {_facet_expr(col)} AS value, COUNT(*) AS count FROM {TABLE} {where_sql} '
            f'GROUP BY 1 ORDER BY count DESC',
            params,
        )
        out[col] = rows
    return out


@haq_router.get("/data")
def haq_data(
    filters: Optional[str] = Query(None),
    question: Optional[str] = Query(None),
    answer: Optional[str] = Query(None),
    question_number: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    parsed = _parse_filters(filters)
    search = {"question": question or "", "answer": answer or "", "question_number": question_number or ""}
    where_sql, params = _build_where(parsed, search)

    total = _run(f"SELECT COUNT(*) AS c FROM {TABLE} {where_sql}", params)[0]["c"]

    order_col = sort_col if sort_col in ALL_COLUMNS else "modified_on"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    offset = (page - 1) * page_size
    cols_sql = ", ".join(f'"{c}"' for c in ALL_COLUMNS)
    rows = _run(
        f'SELECT {cols_sql} FROM {TABLE} {where_sql} '
        f'ORDER BY "{order_col}" {order_dir} NULLS LAST LIMIT %s OFFSET %s',
        params + [page_size, offset],
    )

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }

@haq_router.get("/data/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def haq_data_download(
    filters: Optional[str] = Query(None),
    question: Optional[str] = Query(None),
    answer: Optional[str] = Query(None),
    question_number: Optional[str] = Query(None),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    """Streams every matching question as CSV - the whole filtered set, not
    just the page on screen. Uses a named (server-side) cursor so a large
    export never has to be materialized in memory first."""
    parsed = _parse_filters(filters)
    search = {"question": question or "", "answer": answer or "", "question_number": question_number or ""}
    where_sql, params = _build_where(parsed, search)

    order_col = sort_col if sort_col in ALL_COLUMNS else "modified_on"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    cols_sql = ", ".join(f'"{c}"' for c in ALL_COLUMNS)
    sql = (
        f'SELECT {cols_sql} FROM {TABLE} {where_sql} '
        f'ORDER BY "{order_col}" {order_dir} NULLS LAST'
    )

    def generate():
        conn = _connect()
        conn.autocommit = True
        try:
            with conn.cursor(name="haq_download_cursor") as cur:
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
        headers={"Content-Disposition": 'attachment; filename="haq_questions.csv"'},
    )