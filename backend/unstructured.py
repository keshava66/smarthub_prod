from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import StreamingResponse

from auth import require_permission
from cryptography.hazmat.primitives import serialization

logger = logging.getLogger(__name__)

unstructured_router = APIRouter(prefix="/api/unstructured", tags=["Unstructured"])

# Wired in from app.py via set_unstructured_dependencies()
_config: Optional[Dict[str, Any]] = None

# Table is ~2.8M rows — we never pull it whole. Everything below is done with
# WHERE / GROUP BY / LIMIT in Snowflake, and only a page (or a small aggregate)
# ever crosses the wire.

ALL_COLUMNS = [
    "FILE_NAME", "OBJ_ID", "A_STATUS", "STATUS", "R_VERSION_LABEL", "R_VERSION_LABEL_VIZ",
    "OBJECT_NAME", "ARTIFACT_NAME", "PRODUCT_CODE", "DOMAIN", "JJ_NUMBER", "JJ_TITLE",
    "INSIGHT_FAMILY_CODE", "INSIGHT_FAMILY_NAME", "INSIGHT_THERAPEUTIC_AREA", "CLINICAL_TRIAL_ID",
]

# Equality/IN-style filters (checkbox dropdowns in the UI).
FILTERABLE_COLUMNS = [
    "A_STATUS", "STATUS", "DOMAIN", "PRODUCT_CODE", "ARTIFACT_NAME",
    "INSIGHT_FAMILY_CODE", "INSIGHT_FAMILY_NAME", "INSIGHT_THERAPEUTIC_AREA", "CLINICAL_TRIAL_ID",
]

# Free-text "contains" filters.
SEARCHABLE_COLUMNS = ["FILE_NAME", "OBJECT_NAME", "JJ_TITLE", "ARTIFACT_NAME", "JJ_NUMBER"]

MAX_PAGE_SIZE = 250
MAX_PAGE = 100_000  # keeps OFFSET sane on a ~2.8M row table
DOWNLOAD_CHUNK_ROWS = 10_000  # rows per fetch while streaming a CSV download

CACHE_TTL_SECONDS = 300  # 5 minutes
CACHE_MAX_ENTRIES = 1000

# Base view, reused (as a CTE) by every query below.
BASE_CTE = """
WITH V AS (
    SELECT FILE_NAME,
           OBJ_ID,
           FILE_PATH,
           A_STATUS,
           STATUS,
           R_VERSION_LABEL,
           R_VERSION_LABEL_VIZ,
           OBJECT_NAME,
           ARTIFACT_NAME,
           PRODUCT_CODE,
           DOMAIN,
           JJ_NUMBER,
           JJ_TITLE,
           INSIGHT_FAMILY_CODE,
           INSIGHT_FAMILY_NAME,
           INSIGHT_THERAPEUTIC_AREA,
           CLINICAL_TRIAL_ID,
           INGESTED_AT,
           FILE_SIZE
    FROM (
        SELECT
            ID_IN_SOURCE_SYSTEM,
            CONTENT_LOCATION,
            A_STATUS,
            STATUS,
            OBJECT_NAME,
            GROUP_NAME,
            R_OBJECT_TYPE,
            PRIMARY_GROUP,
            ARTIFACT_NAME,
            CD_MIGRATED_SOURCE,
            DCTM_OBJ_LINK,
            SPLIT(DCTM_OBJ_LINK, '/')[2]::STRING AS PRODUCT_CODE,
            R_VERSION_LABEL,
            FILE_NAME,
            FILE_PATH,
            DOMAIN,
            OBJ_ID,
            JJ_NUMBER,
            JJ_TITLE,
            R_VERSION_LABEL_VIZ,
            A_STATUS_VIZ,
            R_OBJECT_TYPE_VIZ,
            INSIGHT_FAMILY_CODE,
            INSIGHT_THERAPEUTIC_AREA,
            INSIGHT_FAMILY_NAME,
            CLINICAL_TRIAL_ID,
            INGESTED_AT,
            FILE_SIZE
        FROM DEV_RAW.RIMDOCS_CONTROL_STG.ALL_METADATA_JOINED_WITH_TA_CTI
    )
)
"""


def set_unstructured_dependencies(config: Optional[Dict[str, Any]]):
    """Called once from app.py with the 'unstructured' section of dq_config.json."""
    global _config
    _config = config
    if not _config:
        logger.warning("⚠️ unstructured config not found - Unstructured endpoints will be unavailable")


def _load_private_key(private_key_path: str, private_key_password: str) -> bytes:
    if not Path(private_key_path).exists():
        raise FileNotFoundError(f"Private key not found at {private_key_path}")
    with open(private_key_path, "rb") as f:
        key = serialization.load_pem_private_key(
            f.read(),
            password=private_key_password.encode() if private_key_password else None,
        )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


# ============================================================================
# Connection management
# ----------------------------------------------------------------------------
# One warm, TTL-refreshed Snowflake connection shared across requests, instead
# of paying a fresh key-pair handshake + TLS setup on every single API call.
# Same TTL-refresh-with-healthcheck shape as BDCConnectionManager in app.py.
# ============================================================================

class _UnstructuredConnectionManager:
    CONNECTION_TTL = 2 * 60 * 60  # 2 hours
    PING_AFTER = 60  # seconds idle before we health-check again
    CLOSE_GRACE = 120  # > STATEMENT_TIMEOUT, so in-flight queries on a replaced connection can finish

    def __init__(self):
        self._client = None
        self._created_at = 0.0
        self._last_ok = 0.0
        self._lock = threading.Lock()

    def _connect(self):
        # Imported here (not at module top) to dodge a circular import - app.py
        # imports this file, so this file can't import SnowflakeClient from app
        # until app has finished loading.
        from app import SnowflakeClient
        import snowflake.connector

        if not _config:
            raise HTTPException(status_code=503, detail="Unstructured not configured - missing 'unstructured' section in dq_config.json")

        private_key = _load_private_key(
            _config["private_key_path"],
            _config.get("private_key_password", ""),
        )
        conn = snowflake.connector.connect(
            user=_config["user"],
            account=_config["account"],
            private_key=private_key,
            role=_config["role"],
            warehouse=_config["warehouse"],
            database=_config["database"],
            schema=_config["schema"],
            session_parameters={"STATEMENT_TIMEOUT_IN_SECONDS": 60},
        )
        return SnowflakeClient(conn)

    def get_client(self):
        with self._lock:
            expired = (time.time() - self._created_at) > self.CONNECTION_TTL

            if self._client is not None and not expired:
                # Pinging on every call meant parallel requests queued on this lock
                # for a round trip each. Only ping after a quiet spell; a dead
                # connection mid-burst is still caught by _run's reconnect+retry.
                if time.time() - self._last_ok < self.PING_AFTER:
                    self._last_ok = time.time()
                    return self._client
                try:
                    self._client.execute("SELECT 1")
                    self._last_ok = time.time()
                    return self._client
                except Exception:
                    logger.info("Unstructured connection stale, reconnecting")
                    self._close_later(self._client, 0)
            elif self._client is not None:
                # TTL refresh: other requests may still be mid-query on the old
                # connection, so close it once they've had time to finish
                self._close_later(self._client, self.CLOSE_GRACE)

            self._client = self._connect()
            self._created_at = self._last_ok = time.time()
            return self._client

    def reconnect(self, bad):
        """Swap in a fresh connection after `bad` failed. If another request
        already swapped it, reuse theirs instead of opening one more."""
        with self._lock:
            if self._client is bad or self._client is None:
                if bad is not None:
                    self._close_later(bad, self.CLOSE_GRACE)
                self._client = self._connect()
                self._created_at = self._last_ok = time.time()
            return self._client

    @staticmethod
    def _close_later(client, delay):
        def close():
            try:
                client.close()
            except Exception:
                pass
        if delay <= 0:
            close()
        else:
            t = threading.Timer(delay, close)
            t.daemon = True
            t.start()


_conn_manager = _UnstructuredConnectionManager()


def _get_client():
    return _conn_manager.get_client()


# ============================================================================
# Tiny in-process TTL cache for query results
# ============================================================================

_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _cache_key(name: str, params: Dict[str, Any]) -> str:
    raw = json.dumps({"name": name, "params": params}, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ts, payload = entry
        if time.time() - ts > CACHE_TTL_SECONDS:
            return None
        return payload


def _cache_set(key: str, payload: Any):
    with _cache_lock:
        _cache[key] = (time.time(), payload)
        if len(_cache) > CACHE_MAX_ENTRIES:
            oldest_key = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest_key, None)


def _to_json_safe(val):
    if hasattr(val, "isoformat"):
        return val.isoformat()
    if isinstance(val, float):
        # NaN / +-Infinity are legal in Snowflake results but not in JSON -
        # FastAPI's encoder raises "Out of range float values are not JSON
        # compliant". Surface them as null instead of 500ing the whole page.
        if val != val or val == float("inf") or val == float("-inf"):
            return None
    return val


def _parse_filters(filters: Optional[str]) -> Dict[str, List[str]]:
    """`filters` is a JSON object: {"COL": ["val1", "val2"], ...}. (Previously
    a hand-rolled "COL:v1||v2,COL2:v3" format with per-value percent-encoding
    on the frontend - that double-encoded anything with a space/quote/special
    char once URLSearchParams encoded the already-encoded string again, so
    those filters silently matched zero rows. JSON sidesteps both the
    delimiter-collision and the double-encoding problem.)"""
    parsed: Dict[str, List[str]] = {}
    if not filters:
        return parsed
    try:
        raw = json.loads(filters)
    except (ValueError, TypeError):
        return parsed
    if not isinstance(raw, dict):
        return parsed
    for col, values in raw.items():
        col = str(col).strip().upper()
        if col not in FILTERABLE_COLUMNS or not isinstance(values, list):
            continue
        values = [str(v) for v in values if v not in (None, "")]
        if values:
            parsed[col] = values
    return parsed


def _build_where(filters, search_file_name, search_object_name, search_jj_title, search_artifact_name="", search_jj_number=""):
    clauses = []
    params: Dict[str, Any] = {}

    for col, values in filters.items():
        placeholders = []
        for i, v in enumerate(values):
            key = f"{col}_{i}"
            placeholders.append(f"%({key})s")
            params[key] = v
        clauses.append(f'"{col}" IN ({", ".join(placeholders)})')

    if search_file_name:
        clauses.append('"FILE_NAME" ILIKE %(search_file_name)s')
        params["search_file_name"] = f"%{search_file_name}%"
    if search_object_name:
        clauses.append('"OBJECT_NAME" ILIKE %(search_object_name)s')
        params["search_object_name"] = f"%{search_object_name}%"
    if search_jj_title:
        clauses.append('"JJ_TITLE" ILIKE %(search_jj_title)s')
        params["search_jj_title"] = f"%{search_jj_title}%"
    if search_artifact_name:
        clauses.append('"ARTIFACT_NAME" ILIKE %(search_artifact_name)s')
        params["search_artifact_name"] = f"%{search_artifact_name}%"
    if search_jj_number:
        clauses.append('"JJ_NUMBER" ILIKE %(search_jj_number)s')
        params["search_jj_number"] = f"%{search_jj_number}%"
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


# Snowflake errnos that mean "your session is gone, log in again"
_SESSION_EXPIRED = {390111, 390112, 390114}


def _is_connection_error(e: Exception) -> bool:
    """True when a fresh connection could fix it. A bad query or a statement
    timeout would just fail again (and a timeout would make the user wait twice)."""
    from snowflake.connector.errors import ProgrammingError
    if isinstance(e, ProgrammingError):
        return getattr(e, "errno", None) in _SESSION_EXPIRED
    return True


def _run(sql: str, params: Optional[Dict[str, Any]] = None):
    """Execute against the shared connection; on a dropped/expired connection,
    reconnect once and retry."""
    client = _get_client()
    try:
        return client.execute(sql, params or {})
    except (FileNotFoundError, HTTPException):
        raise
    except Exception as e:
        if not _is_connection_error(e):
            raise
        logger.info("Unstructured connection dropped mid-query, reconnecting and retrying once")
        return _conn_manager.reconnect(client).execute(sql, params or {})


def _render_sql_for_display(sql: str, params: Optional[Dict[str, Any]] = None) -> str:
    """Inlines bound params into the SQL text for display in the UI's 'View SQL'
    button. Never used for execution - _run always uses the parameterized
    version above, this is purely so a human can read what actually ran."""
    if not params:
        return sql.strip()
    rendered = sql
    for key, value in params.items():
        if isinstance(value, str):
            literal = "'" + value.replace("'", "''") + "'"
        elif value is None:
            literal = "NULL"
        else:
            literal = str(value)
        rendered = rendered.replace(f"%({key})s", literal)
    return rendered.strip()


@unstructured_router.get("/rows")
def get_rows(
    page: int = Query(1, ge=1, le=MAX_PAGE),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: str = Query("FILE_NAME"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    filters: Optional[str] = Query(None, description="DOMAIN:val1||val2,A_STATUS:val3"),
    search_file_name: str = Query(""),
    search_object_name: str = Query(""),
    search_jj_title: str = Query(""),
    search_artifact_name: str = Query(""),
    search_jj_number: str = Query(""),
    distinct_only: bool = Query(False, description="One row per FILE_NAME instead of one row per version"),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    """One page of rows, filtered/sorted in Snowflake. Never pulls the full table."""
    if sort_col not in ALL_COLUMNS:
        sort_col = "FILE_NAME"

    cache_key = _cache_key("rows", dict(
        page=page, page_size=page_size, sort_col=sort_col, sort_dir=sort_dir,
        filters=filters or "", search_file_name=search_file_name,
        search_object_name=search_object_name, search_jj_title=search_jj_title,
        search_artifact_name=search_artifact_name, search_jj_number=search_jj_number, distinct_only=distinct_only,
    ))
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    parsed_filters = _parse_filters(filters)
    where_sql, params = _build_where(parsed_filters, search_file_name, search_object_name, search_jj_title, search_artifact_name, search_jj_number)

    offset = (page - 1) * page_size
    params["limit"] = page_size
    params["offset"] = offset

    sort_direction = "ASC" if sort_dir == "asc" else "DESC"
    if distinct_only:
        # One row per FILE_NAME - picks the row Snowflake happens to keep for
        # that FILE_NAME (arbitrary among its versions), same as COUNT(DISTINCT
        # FILE_NAME) on the summary card. QUALIFY needs its own reference to
        # V, so the sort has to be re-applied on the deduped result.
        dedup_sql = f'{BASE_CTE} SELECT * FROM V {where_sql} QUALIFY ROW_NUMBER() OVER (PARTITION BY "FILE_NAME" ORDER BY "FILE_NAME") = 1'
        data_sql = f'{dedup_sql} ORDER BY "{sort_col}" {sort_direction} NULLS LAST LIMIT %(limit)s OFFSET %(offset)s'
        count_sql = f'{BASE_CTE} SELECT COUNT(DISTINCT "FILE_NAME") FROM V {where_sql}'
    else:
        data_sql = f'{BASE_CTE} SELECT * FROM V {where_sql} ORDER BY "{sort_col}" {sort_direction} NULLS LAST LIMIT %(limit)s OFFSET %(offset)s'
        count_sql = f"{BASE_CTE} SELECT COUNT(*) FROM V {where_sql}"
    count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}

    try:
        t0 = time.time()
        data_result = _run(data_sql, params)
        count_result = _run(count_sql, count_params)
        elapsed_ms = int((time.time() - t0) * 1000)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unstructured /rows failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    columns = [c.upper() for c in data_result.columns]
    rows = [[_to_json_safe(v) for v in r] for r in data_result.rows]
    total_count = count_result.rows[0][0] if count_result.rows else 0

    payload = {
        "columns": columns,
        "rows": rows,
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, -(-total_count // page_size)),
        "elapsed_ms": elapsed_ms,
        "query_id": data_result.query_id,
        # "sql" = the page-of-rows query. total_pages (and therefore "Current
        # page") is actually driven by count_sql, and elapsed_ms times BOTH
        # queries - so callers that want an accurate picture should use
        # count_sql too, not just sql.
        "sql": _render_sql_for_display(data_sql, params),
        "count_sql": _render_sql_for_display(count_sql, count_params),
        "cached": False,
    }
    _cache_set(cache_key, payload)
    return payload


@unstructured_router.get("/rows/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def download_rows_csv(
    sort_col: str = Query("FILE_NAME"),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_object_name: str = Query(""),
    search_jj_title: str = Query(""),
    search_artifact_name: str = Query(""),
    search_jj_number: str = Query(""),
    distinct_only: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    """Streams every row matching the current filters as CSV - not just the
    page on screen. Fetched from Snowflake in chunks so this stays memory-flat
    even when the filtered set runs into the millions."""
    if sort_col not in ALL_COLUMNS:
        sort_col = "FILE_NAME"

    parsed_filters = _parse_filters(filters)
    where_sql, params = _build_where(parsed_filters, search_file_name, search_object_name, search_jj_title, search_artifact_name, search_jj_number)
    sort_direction = "ASC" if sort_dir == "asc" else "DESC"

    if distinct_only:
        sql = (
            f'{BASE_CTE} SELECT * FROM V {where_sql} '
            f'QUALIFY ROW_NUMBER() OVER (PARTITION BY "FILE_NAME" ORDER BY "FILE_NAME") = 1 '
            f'ORDER BY "{sort_col}" {sort_direction} NULLS LAST'
        )
    else:
        sql = f'{BASE_CTE} SELECT * FROM V {where_sql} ORDER BY "{sort_col}" {sort_direction} NULLS LAST'

    def generate():
        client = _get_client()
        cur = client.conn.cursor()
        try:
            cur.execute(sql, params)
            columns = [c[0] for c in cur.description]
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(columns)
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
            cur.close()

    filename = f"unstructured{'_distinct' if distinct_only else ''}.csv"
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _get_total_size_bytes(bypass_cache: bool = False) -> Optional[int]:
    """All-time SUM(FILE_SIZE) across the whole table, no filters applied -
    the denominator for the 'ingested so far / total' size cards. Deduped to
    one row per FILE_PATH first.

    FILE_PATH can't be referenced directly inside a window PARTITION BY/ORDER
    BY in this table - like OBJ_ID, it's some type Snowflake refuses to accept
    there at all ("is not a valid group by expression"), and the check applies
    to the column's lineage even through wrapping functions. The workaround:
    materialize it into a plain VARCHAR via TO_VARCHAR() in its own nested
    SELECT first, then partition on that already-materialized column in the
    outer query - that breaks the direct reference chain Snowflake is
    checking.

    Cached on its own key since it doesn't vary with filters, so filter
    changes don't force a rescan of the full table just to redraw an
    unrelated number. Returns None (not 0) on failure so the caller can tell
    "unknown" apart from "the table is genuinely empty"."""
    cache_key = _cache_key("total-size-bytes", {})
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached
    try:
        sql = (
            f'{BASE_CTE} '
            f'SELECT SUM(CASE WHEN "PATH_RN" = 1 THEN "FILE_SIZE" ELSE 0 END) FROM ('
            f'  SELECT "FILE_SIZE", ROW_NUMBER() OVER (PARTITION BY "FILE_PATH_KEY" ORDER BY "FILE_PATH_KEY") AS "PATH_RN" '
            f'  FROM (SELECT "FILE_SIZE", TO_VARCHAR("FILE_PATH") AS "FILE_PATH_KEY" FROM V)'
            f')'
        )
        result = _run(sql, {})
        total = int((result.rows[0][0] if result.rows and result.rows[0][0] is not None else 0))
    except Exception as e:
        logger.error(f"Unstructured total-size-bytes lookup failed: {e}")
        return None
    _cache_set(cache_key, total)
    return total


@unstructured_router.get("/summary")
def get_summary(
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_object_name: str = Query(""),
    search_jj_title: str = Query(""),
    search_artifact_name: str = Query(""),
    search_jj_number: str = Query(""),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    """Cheap headline numbers for the overview cards - COUNT(*) / COUNT(DISTINCT ...)
    plus SUM(FILE_SIZE) for the current filters (ingested-so-far) and, separately,
    the all-time SUM(FILE_SIZE) with no filters applied (grand total).

    ingested_size_bytes is deduped to one row per FILE_PATH (materialized via
    TO_VARCHAR in a nested SELECT first - FILE_PATH can't be referenced
    directly in a window PARTITION BY on this table, see _get_total_size_bytes
    for why) so multiple versions of the same object don't inflate the size
    sum, and only counts rows where STATUS = 'SUCCESS' - a document that's
    still RUNNING or came back ERROR hasn't actually finished ingesting, so
    its size shouldn't count toward "ingested so far" even though the row
    itself matches the current filters. total_count / distinct_file_names
    stay unrestricted (every row, every status), same as before - only the
    two size numbers are FILE_PATH-deduped."""
    cache_key = _cache_key("summary", dict(
        filters=filters or "", search_file_name=search_file_name,
        search_object_name=search_object_name, search_jj_title=search_jj_title,
        search_artifact_name=search_artifact_name,
        search_jj_number=search_jj_number,
    ))
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return _summary_for({**cached, "cached": True}, auth)

    parsed_filters = _parse_filters(filters)
    where_sql, params = _build_where(parsed_filters, search_file_name, search_object_name, search_jj_title, search_artifact_name, search_jj_number)
    sql = (
        f'{BASE_CTE} SELECT COUNT(*), COUNT(DISTINCT "FILE_NAME"), '
        f'SUM(CASE WHEN "FILE_RN" = 1 AND UPPER("STATUS") = \'SUCCESS\' THEN "FILE_SIZE" ELSE 0 END) '
        f'FROM ('
        f'  SELECT *, ROW_NUMBER() OVER (PARTITION BY "FILE_PATH_KEY" ORDER BY "FILE_PATH_KEY") AS "FILE_RN" '
        f'  FROM (SELECT *, TO_VARCHAR("FILE_PATH") AS "FILE_PATH_KEY" FROM V {where_sql})'
        f')'
    )

    try:
        result = _run(sql, params)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unstructured /summary failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    total_count, distinct_file_names, ingested_size_bytes = result.rows[0] if result.rows else (0, 0, 0)
    total_size_bytes = _get_total_size_bytes(bypass_cache)

    payload = {
        "total_count": total_count,
        "distinct_file_names": distinct_file_names,
        "ingested_size_bytes": int(ingested_size_bytes or 0),
        "total_size_bytes": total_size_bytes,
        "sql": _render_sql_for_display(sql, params),
        "cached": False,
    }
    _cache_set(cache_key, payload)
    return _summary_for(payload, auth)


def _summary_for(payload: Dict[str, Any], auth: dict) -> Dict[str, Any]:
    """Total size is only for people with viewUnstructuredRimDocsTotals.
    The cache keeps the full payload, so strip it per request."""
    if (auth.get("permissions") or {}).get("viewUnstructuredRimDocsTotals") is True:
        return payload
    return {**payload, "total_size_bytes": None}


@unstructured_router.get("/filter-options")
def get_filter_options(column: str = Query(...), bypass_cache: bool = Query(False), auth: dict = Depends(require_permission("viewUnstructured"))):
    """Distinct values for one filterable column, used to populate a checkbox dropdown."""
    column = column.upper()
    if column not in FILTERABLE_COLUMNS:
        raise HTTPException(status_code=400, detail=f"'{column}' is not a filterable column")

    cache_key = _cache_key("filter-options", {"column": column})
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    sql = f'{BASE_CTE} SELECT DISTINCT "{column}" FROM V WHERE "{column}" IS NOT NULL ORDER BY "{column}"'

    try:
        result = _run(sql)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unstructured /filter-options failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    payload = {"column": column, "values": [r[0] for r in result.rows], "cached": False}
    _cache_set(cache_key, payload)
    return payload


@unstructured_router.get("/chart/domain-status")
def get_domain_status_chart(
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_object_name: str = Query(""),
    search_jj_title: str = Query(""),
    search_artifact_name: str = Query(""),
    search_jj_number: str = Query(""),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    cache_key = _cache_key("chart-domain-status", dict(
        filters=filters or "", search_file_name=search_file_name,
        search_object_name=search_object_name, search_jj_title=search_jj_title,
        search_artifact_name=search_artifact_name,
        search_jj_number=search_jj_number,
    ))
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    parsed_filters = _parse_filters(filters)
    where_sql, params = _build_where(parsed_filters, search_file_name, search_object_name, search_jj_title, search_artifact_name, search_jj_number)
    sql = f'{BASE_CTE} SELECT "DOMAIN", "A_STATUS", COUNT(*) FROM V {where_sql} GROUP BY "DOMAIN", "A_STATUS" ORDER BY "DOMAIN"'

    try:
        result = _run(sql, params)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unstructured /chart/domain-status failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    payload = {"rows": [[r[0], r[1], r[2]] for r in result.rows], "sql": _render_sql_for_display(sql, params), "cached": False}
    _cache_set(cache_key, payload)
    return payload


@unstructured_router.get("/chart/ingestion-status")
def get_ingestion_status_chart(
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_object_name: str = Query(""),
    search_jj_title: str = Query(""),
    search_artifact_name: str = Query(""),
    search_jj_number: str = Query(""),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    """Distinct-file count per STATUS value (the raw ingestion-status column -
    distinct from A_STATUS). Feeds the 'Ingestion Status' KPI donut, which
    sits inside the Distinct Files card - so this counts DISTINCT FILE_NAME,
    not COUNT(*), to match what that card actually means."""
    cache_key = _cache_key("chart-ingestion-status", dict(
        filters=filters or "", search_file_name=search_file_name,
        search_object_name=search_object_name, search_jj_title=search_jj_title,
        search_artifact_name=search_artifact_name,
        search_jj_number=search_jj_number,
    ))
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    parsed_filters = _parse_filters(filters)
    where_sql, params = _build_where(parsed_filters, search_file_name, search_object_name, search_jj_title, search_artifact_name, search_jj_number)
    sql = (
        f'{BASE_CTE} SELECT "STATUS", COUNT(DISTINCT "FILE_NAME") FROM V {where_sql} '
        f'GROUP BY "STATUS" ORDER BY COUNT(DISTINCT "FILE_NAME") DESC'
    )

    try:
        result = _run(sql, params)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unstructured /chart/ingestion-status failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    payload = {"rows": [[r[0], r[1]] for r in result.rows], "sql": _render_sql_for_display(sql, params), "cached": False}
    _cache_set(cache_key, payload)
    return payload


@unstructured_router.get("/chart/domain-distinct-objects")
def get_domain_distinct_objects_chart(
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_object_name: str = Query(""),
    search_jj_title: str = Query(""),
    search_artifact_name: str = Query(""),
    search_jj_number: str = Query(""),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    """Count of distinct OBJ_ID per domain - a row can repeat OBJ_ID
    across versions, so this is a different number than COUNT(*) by domain."""
    cache_key = _cache_key("chart-domain-distinct-objects", dict(
        filters=filters or "", search_file_name=search_file_name,
        search_object_name=search_object_name, search_jj_title=search_jj_title,
        search_artifact_name=search_artifact_name,
        search_jj_number=search_jj_number,
    ))
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    parsed_filters = _parse_filters(filters)
    where_sql, params = _build_where(parsed_filters, search_file_name, search_object_name, search_jj_title, search_artifact_name, search_jj_number)
    sql = (
        f'{BASE_CTE} SELECT "DOMAIN", COUNT(DISTINCT "OBJ_ID") AS CNT FROM V {where_sql} '
        f'GROUP BY "DOMAIN" ORDER BY CNT DESC'
    )

    try:
        result = _run(sql, params)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unstructured /chart/domain-distinct-objects failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    payload = {"rows": [[r[0], r[1]] for r in result.rows], "sql": _render_sql_for_display(sql, params), "cached": False}
    _cache_set(cache_key, payload)
    return payload


@unstructured_router.get("/chart/documents-over-time")
def get_documents_over_time_chart(
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_object_name: str = Query(""),
    search_jj_title: str = Query(""),
    search_artifact_name: str = Query(""),
    search_jj_number: str = Query(""),
    granularity: str = Query("hour"),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    """Distinct-file count bucketed by INGESTED_AT, for the 'documents ingested over
    time' trend line. granularity is 'hour' (default), 'day', 'week', or 'month'."""
    granularity = granularity.lower()
    if granularity not in ("hour", "day", "week", "month"):
        granularity = "hour"

    cache_key = _cache_key("chart-documents-over-time", dict(
        filters=filters or "", search_file_name=search_file_name,
        search_object_name=search_object_name, search_jj_title=search_jj_title,
        search_artifact_name=search_artifact_name,
        search_jj_number=search_jj_number, granularity=granularity,
    ))
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    parsed_filters = _parse_filters(filters)
    where_sql, params = _build_where(parsed_filters, search_file_name, search_object_name, search_jj_title, search_artifact_name, search_jj_number)
    not_null_clause = "AND" if where_sql else "WHERE"
    sql = (
        f'{BASE_CTE} SELECT DATE_TRUNC(\'{granularity}\', "INGESTED_AT") AS PERIOD, '
        f'COUNT(DISTINCT "FILE_NAME") FROM V {where_sql} {not_null_clause} "INGESTED_AT" IS NOT NULL '
        f'GROUP BY PERIOD ORDER BY PERIOD'
    )

    try:
        result = _run(sql, params)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unstructured /chart/documents-over-time failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    payload = {
        "rows": [[r[0].isoformat() if hasattr(r[0], "isoformat") else r[0], r[1]] for r in result.rows],
        "sql": _render_sql_for_display(sql, params),
        "cached": False,
    }
    _cache_set(cache_key, payload)
    return payload


@unstructured_router.get("/chart/therapeutic-area")
def get_therapeutic_area_chart(
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_object_name: str = Query(""),
    search_jj_title: str = Query(""),
    search_artifact_name: str = Query(""),
    search_jj_number: str = Query(""),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    cache_key = _cache_key("chart-therapeutic-area", dict(
        filters=filters or "", search_file_name=search_file_name,
        search_object_name=search_object_name, search_jj_title=search_jj_title,
        search_artifact_name=search_artifact_name,
        search_jj_number=search_jj_number,
    ))
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    parsed_filters = _parse_filters(filters)
    where_sql, params = _build_where(parsed_filters, search_file_name, search_object_name, search_jj_title, search_artifact_name, search_jj_number)
    sql = (
        f'{BASE_CTE} SELECT "INSIGHT_THERAPEUTIC_AREA", COUNT(*) AS CNT FROM V {where_sql} '
        f'GROUP BY "INSIGHT_THERAPEUTIC_AREA" ORDER BY CNT DESC LIMIT 15'
    )

    try:
        result = _run(sql, params)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unstructured /chart/therapeutic-area failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    payload = {"rows": [[r[0], r[1]] for r in result.rows], "sql": _render_sql_for_display(sql, params), "cached": False}
    _cache_set(cache_key, payload)
    return payload


@unstructured_router.get("/chart/therapeutic-area-domain")
def get_therapeutic_area_domain_chart(
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_object_name: str = Query(""),
    search_jj_title: str = Query(""),
    search_artifact_name: str = Query(""),
    search_jj_number: str = Query(""),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    """Record volume per therapeutic area split by domain - the stacked
    companion to /chart/therapeutic-area (same totals, one more dimension).
    The frontend ranks areas by total and keeps the top 15 client-side."""
    cache_key = _cache_key("chart-therapeutic-area-domain", dict(
        filters=filters or "", search_file_name=search_file_name,
        search_object_name=search_object_name, search_jj_title=search_jj_title,
        search_artifact_name=search_artifact_name,
        search_jj_number=search_jj_number,
    ))
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    parsed_filters = _parse_filters(filters)
    where_sql, params = _build_where(parsed_filters, search_file_name, search_object_name, search_jj_title, search_artifact_name, search_jj_number)
    sql = (
        f'{BASE_CTE} SELECT "INSIGHT_THERAPEUTIC_AREA", "DOMAIN", COUNT(*) AS CNT FROM V {where_sql} '
        f'GROUP BY "INSIGHT_THERAPEUTIC_AREA", "DOMAIN" ORDER BY "INSIGHT_THERAPEUTIC_AREA"'
    )

    try:
        result = _run(sql, params)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unstructured /chart/therapeutic-area-domain failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    payload = {"rows": [[r[0], r[1], r[2]] for r in result.rows], "sql": _render_sql_for_display(sql, params), "cached": False}
    _cache_set(cache_key, payload)
    return payload


@unstructured_router.get("/chart/breakdown")
def get_breakdown_chart(
    filters: Optional[str] = Query(None),
    search_file_name: str = Query(""),
    search_object_name: str = Query(""),
    search_jj_title: str = Query(""),
    search_artifact_name: str = Query(""),
    search_jj_number: str = Query(""),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    """DOMAIN -> INSIGHT_THERAPEUTIC_AREA -> ARTIFACT_NAME drill-down, counted
    by distinct OBJ_ID. Follows the same filters/search as the rest of the
    page, same as every other chart endpoint."""
    cache_key = _cache_key("chart-breakdown", dict(
        filters=filters or "", search_file_name=search_file_name,
        search_object_name=search_object_name, search_jj_title=search_jj_title,
        search_artifact_name=search_artifact_name,
        search_jj_number=search_jj_number,
    ))
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}

    parsed_filters = _parse_filters(filters)
    where_sql, params = _build_where(parsed_filters, search_file_name, search_object_name, search_jj_title, search_artifact_name, search_jj_number)
    sql = (
        f'{BASE_CTE} SELECT "DOMAIN", "INSIGHT_THERAPEUTIC_AREA", "ARTIFACT_NAME", '
        f'COUNT(DISTINCT "OBJ_ID") AS CNT FROM V {where_sql} '
        f'GROUP BY "DOMAIN", "INSIGHT_THERAPEUTIC_AREA", "ARTIFACT_NAME" ORDER BY 1, 2, 3'
    )

    try:
        result = _run(sql, params)
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Unstructured /chart/breakdown failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    payload = {"rows": [[r[0], r[1], r[2], r[3]] for r in result.rows], "sql": _render_sql_for_display(sql, params), "cached": False}
    _cache_set(cache_key, payload)
    return payload