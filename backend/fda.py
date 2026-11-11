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

fda_router = APIRouter(prefix="/api/fda", tags=["FDA"])

_config: Optional[Dict[str, Any]] = None

DATABASE = "regulatory"
SCHEMA = "vector_embedding"
TABLE = f"{SCHEMA}.crl_ai_matches"

# One row per FDA CRL letter <-> Pink Sheet row match, already flat - no
# grouping needed (unlike Nipo's doc_chunks).
ALL_COLUMNS = [
    "id", "fda_id", "fda_file_name", "fda_status", "fda_letter_date", "fda_sponsor", "fda_product",
    "pink_row_num", "pink_sponsor_product", "pink_indication", "pink_issues", "pink_activity", "pink_date_announced",
    "method", "sim_score", "llm_decision", "llm_rationale", "created_at",
]

FILTERABLE_COLUMNS = ["llm_decision", "method", "fda_status"]
SEARCHABLE_COLUMNS = ["fda_sponsor", "fda_product", "pink_sponsor_product", "pink_indication", "fda_file_name"]

# ----------------------------------------------------------------------------
# "CRL Letters" dashboard - second FDA dashboard, reads vector_embedding.
# crl_letters directly (the raw ingested CRL PDFs, before AI matching against
# Pink Sheet). Business-facing columns only - local_path, meta_json (a JSON
# mirror of columns already shown) and method_version (always 'crl_v1' today,
# nothing to slice by) are left out of what gets shown to end users.
LETTERS_TABLE = f"{SCHEMA}.crl_letters"

LETTERS_ALL_COLUMNS = [
    "id", "file_name", "status", "sponsor", "product_name", "application_type",
    "submission_id", "letter_date", "notes", "letter_text", "processed_at",
]

LETTERS_FILTERABLE_COLUMNS = ["status", "application_type", "id"]
LETTERS_SEARCHABLE_COLUMNS = ["sponsor", "product_name", "file_name", "submission_id"]

# ----------------------------------------------------------------------------
# "Pink Sheet Letters" dashboard - third FDA dashboard, reads vector_embedding.
# crl_pink_letters: one row per Pink Sheet CRL writeup (the source crl_ai_matches
# matches CRL letters against). No id/primary key column on this table, so
# there's no per-row detail endpoint - the row-click detail panel just shows
# the already-fetched row, same as every other explorer's detail modal.
# All 5 columns are business text already, so nothing gets hidden here - but
# 2 of them ("Indication", "Issues") follow a consistent "{category} - text"
# shape, so that leading category is pulled out with SPLIT_PART for two
# derived (non-real-column) filters: therapeutic_area and issue_category.
PINK_TABLE = f"{SCHEMA}.crl_pink_letters"

# Real Postgres column names - contain spaces/an ampersand, so every
# reference to these must go through the existing f'"{c}"' quoting.
PINK_ALL_COLUMNS = ["Sponsor & Product", "Indication", "Issues", "Date Announced", "Activity"]
PINK_SEARCHABLE_COLUMNS = ["Sponsor & Product", "Indication", "Issues", "Activity"]
PINK_VIRTUAL_FILTERABLE_COLUMNS = ["therapeutic_area", "issue_category"]

# ----------------------------------------------------------------------------
# "eCTD Documents" dashboard - fourth FDA dashboard, reads vector_embedding.
# ectd_docs: ~1M rows, one row per document in the eCTD (electronic Common
# Technical Document) submission structure. Every column is shown - facets
# for the lower-cardinality/categorical columns, free-text search for the
# high-cardinality ones (titles, identifiers, file paths).
ECTD_TABLE = f"{SCHEMA}.ectd_docs"

# Real Postgres column names - "Link" and "Path" are capitalized/no
# underscore, so (like Pink Sheet Letters) every reference to those two
# goes through the existing f'"{c}"' quoting.
ECTD_ALL_COLUMNS = [
    "Link", "r_object_id", "object_name", "a_status", "jj_title", "r_version_label", "jj_number",
    "i_latest_flag", "r_link_cnt", "product_code", "jj_prod_disp_name", "inn_names",
    "product_generic_name", "product_trade_name", "primary_group", "subgroup", "artifact_name",
    "jj_artifact_short_name", "ectd_section", "jj_ectd_leafname", "jj_ectd_leaftitle", "Path", "nas_path",
]

# Checkbox facets - columns with a manageable number of distinct values.
ECTD_FILTERABLE_COLUMNS = [
    "a_status", "primary_group", "subgroup", "i_latest_flag", "r_link_cnt",
    "product_code", "jj_prod_disp_name", "inn_names", "product_generic_name", "product_trade_name",
    "artifact_name", "jj_artifact_short_name", "ectd_section", "jj_ectd_leafname", "jj_ectd_leaftitle",
]

# Free-text search - identifiers, titles and file paths, too high-cardinality
# for a checkbox facet to be useful.
ECTD_SEARCHABLE_COLUMNS = [
    "jj_title", "jj_number", "r_version_label", "r_object_id", "object_name", "Link", "Path", "nas_path",
]

# ----------------------------------------------------------------------------
# "Milvexian Metadata" dashboard - sixth FDA dashboard, reads
# vector_embedding.milvexian_metadata (~776K rows). Same shape as ectd_docs -
# one row per Documentum-managed document, this time scoped to the
# milvexian program - plus one extra column, a_content_type (e.g. 'msw15',
# 'msw14'), which isn't on ectd_docs. Unlike ectd_docs there's a single Path
# column here rather than separate Link/Path columns. Every column is shown,
# same split as eCTD: checkbox facets for the lower-cardinality columns,
# free-text search for identifiers/titles/file paths.
MILVEXIAN_TABLE = f"{SCHEMA}.milvexian_metadata"

# "Path" is capitalized/no underscore - goes through the existing
# f'"{c}"' quoting like every other column reference.
MILVEXIAN_ALL_COLUMNS = [
    "Path", "r_object_id", "object_name", "a_content_type", "a_status", "jj_title", "r_version_label",
    "jj_number", "i_latest_flag", "r_link_cnt", "product_code", "jj_prod_disp_name", "inn_names",
    "product_generic_name", "product_trade_name", "primary_group", "subgroup", "artifact_name",
    "jj_artifact_short_name", "ectd_section", "jj_ectd_leafname", "jj_ectd_leaftitle", "nas_path",
]

# Checkbox facets - columns with a manageable number of distinct values.
MILVEXIAN_FILTERABLE_COLUMNS = [
    "a_status", "a_content_type", "primary_group", "subgroup", "i_latest_flag", "r_link_cnt",
    "product_code", "jj_prod_disp_name", "inn_names", "product_generic_name", "product_trade_name",
    "artifact_name", "jj_artifact_short_name", "ectd_section", "jj_ectd_leafname", "jj_ectd_leaftitle",
]

# Free-text search - identifiers, titles and file paths, too high-cardinality
# for a checkbox facet to be useful.
MILVEXIAN_SEARCHABLE_COLUMNS = [
    "jj_title", "jj_number", "r_version_label", "r_object_id", "object_name", "Path", "nas_path",
]

# ----------------------------------------------------------------------------
# "FDA CRL Letters" dashboard - fifth FDA dashboard, reads
# vector_embedding.fda_crl_letters (~2.5M rows). A richer re-ingest of
# crl_letters: adds application_number, normalized sponsor_norm/product_norm,
# indication and evidence_quote, and - unlike crl_letters' free-text
# letter_date - a real `crl_date_iso` DATE column, so the "by year" chart and
# the derived "year" facet below use EXTRACT() instead of the SPLIT_PART hack
# the older table needs. Every column is shown in the data table; join_key
# (an internal join hash), meta_json (duplicates other columns as JSON) and
# method_version (every row is 'crl_v2') are the only ones with no filter -
# nothing meaningful to search or facet on a random hash/JSON blob/constant.
FDA_LETTERS_TABLE = f"{SCHEMA}.fda_crl_letters"

FDA_LETTERS_ALL_COLUMNS = [
    "id", "file_name", "local_path", "status", "letter_text", "submission_id", "application_type",
    "product_name", "letter_date", "sponsor", "notes", "application_number", "crl_date_iso",
    "sponsor_norm", "product_norm", "indication", "evidence_quote", "join_key", "meta_json",
    "method_version", "processed_at",
]

# Checkbox facets - status/application_type/indication are naturally low-
# cardinality; sponsor_norm/product_norm are the normalized (deduplicated)
# forms of sponsor/product_name, so they group better than the raw text.
FDA_LETTERS_FILTERABLE_COLUMNS = ["status", "application_type", "indication", "sponsor_norm", "product_norm"]

# Free-text search - raw sponsor/product text, identifiers and long-form
# fields where a facet wouldn't be useful.
FDA_LETTERS_SEARCHABLE_COLUMNS = [
    "sponsor", "product_name", "submission_id", "application_number", "file_name",
    "local_path", "letter_date", "notes", "evidence_quote", "letter_text",
]

MAX_PAGE_SIZE = 250
DOWNLOAD_CHUNK_ROWS = 5_000

# ----------------------------------------------------------------------------
# "RIM Facility Index" dashboard - seventh FDA dashboard, reads
# vector_embedding.rimdocs_facility_index_unified (~74M rows) - AI-extracted
# facility records (name, address, responsibility, confidence) pulled out of
# RIM documents. Every column is shown except the raw embedding vector
# itself (a pgvector column - hundreds/thousands of floats, not something
# anyone reads or filters on) which is instead shown as its dimension count.
# address_json is a JSON blob, but its city/state are also pulled out as
# their own derived filters, since "what state is this facility in" is a
# real business question this table can answer that no other FDA dashboard
# covers.
FACILITY_TABLE = f"{SCHEMA}.rimdocs_facility_index_unified"

FACILITY_ALL_COLUMNS = [
    "id", "file_name", "mime_type", "nas_path", "raw_content", "status",
    "processed_at", "doc_sha256", "facility_name", "address_full", "address_json", "responsibility",
    "evidence", "model_confidence", "rule_confidence", "confidence", "placebo_label",
    "method_version", "extracted_at",
]

# Checkbox facets - columns with a manageable number of distinct values.
FACILITY_FILTERABLE_COLUMNS = ["status", "placebo_label", "responsibility", "mime_type"]
# Derived from address_json - not real columns, built separately in facets/data below.
FACILITY_VIRTUAL_FILTERABLE_COLUMNS = ["city", "state"]
# Free-text search - identifiers, titles, addresses and long-form fields
# where a facet wouldn't be useful.
FACILITY_SEARCHABLE_COLUMNS = ["facility_name", "address_full", "file_name", "doc_sha256", "nas_path", "raw_content"]


def set_fda_dependencies(config: Optional[Dict[str, Any]]):
    """Called once from app.py with the 'postgres_query_runner' section of
    dq_config.json - not a dedicated 'fda' section."""
    global _config
    _config = config
    if not _config:
        logger.warning("⚠️ postgres_query_runner config not found in dq_config.json - FDA explorer endpoints will be unavailable")


def _require_config() -> Dict[str, Any]:
    if not _config:
        raise HTTPException(
            status_code=503,
            detail="FDA explorer is not configured - missing 'postgres_query_runner' section in dq_config.json",
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
            application_name="dqe_fda_explorer",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"FDA database connection failed: {e}")
        raise HTTPException(status_code=502, detail="Could not connect to the data source. Please try again later.")


# ============================================================================
# Warm, TTL-refreshed connection - same shape as the other explorer modules.
# ============================================================================

class _FdaConnectionManager:
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
                    logger.info("FDA connection stale, reconnecting")
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = _connect()
            self._conn.autocommit = True
            self._created_at = time.time()
            return self._conn


_conn_manager = _FdaConnectionManager()


def _to_json_safe(val):
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, Decimal):
        return float(val)
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
    if isinstance(val, (dict, list)):
        # jsonb columns (e.g. meta_json) - psycopg2 hands these back
        # pre-parsed; re-serialize to proper JSON text for display.
        try:
            import json
            return json.dumps(val, default=str)
        except Exception:
            return str(val)
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


def _render_sql_for_display(sql: str, params: Optional[list] = None) -> str:
    """Inlines bound params into the SQL text for the UI's 'View SQL' button.
    Never used for execution - _run always uses the parameterized version,
    this is purely so a human can read what actually ran. Params are
    positional (%s), so they're substituted in the order they appear."""
    rendered = sql
    for value in (params or []):
        if isinstance(value, str):
            literal = "'" + value.replace("'", "''") + "'"
        elif value is None:
            literal = "NULL"
        else:
            literal = str(value)
        rendered = rendered.replace("%s", literal, 1)
    return rendered.strip()


def _parse_filters(filters: Optional[str], filterable_cols: List[str] = FILTERABLE_COLUMNS) -> Dict[str, List[str]]:
    """Same 'COL:val1||val2,COL2:val3' shape used by nipo.py / haq.py."""
    parsed: Dict[str, List[str]] = {}
    if not filters:
        return parsed
    for part in filters.split(","):
        if ":" not in part:
            continue
        col, _, vals = part.partition(":")
        col = col.strip()
        if col not in filterable_cols or not vals:
            continue
        parsed[col] = [v for v in vals.split("||") if v != ""]
    return parsed


# Label the facet rails show for rows where a column is NULL or blank. It's a
# real, selectable value, so the facet SELECT and the WHERE builder have to
# derive it from the exact same expression - a plain UPPER("col") comparison
# never matches NULL, which made picking "(unspecified)" return zero rows.
UNSPECIFIED = "(unspecified)"


def _facet_expr(sql: str) -> str:
    """Normalized filter value for any SQL expression: uppercased and trimmed,
    with NULL and blank both folding into the UNSPECIFIED sentinel. The
    sentinel goes on after UPPER() so it stays lowercase and round-trips
    exactly as the UI sends it back."""
    return f"COALESCE(NULLIF(UPPER(TRIM({sql}::text)), ''), '{UNSPECIFIED}')"


def _facet_value_sql(col: str) -> str:
    """Same, for a plain column name."""
    return _facet_expr(f'"{col}"')


def _facet_params(vals: List[str]) -> list:
    """Values coming back from the facet rail, normalized to match
    _facet_value_sql - the sentinel passes through untouched."""
    return [UNSPECIFIED if v.strip().lower() == UNSPECIFIED else v.upper() for v in vals]


def _build_where(filters: Dict[str, List[str]], search: Dict[str, str], searchable_cols: List[str] = SEARCHABLE_COLUMNS) -> tuple[str, list]:
    clauses = []
    params: list = []

    for col, vals in filters.items():
        if not vals:
            continue
        if col == "id":
            # bigint column - exact numeric match, no UPPER()/text cast.
            id_vals = [int(v) for v in vals if v.lstrip("-").isdigit()]
            if not id_vals:
                continue
            placeholders = ", ".join(["%s"] * len(id_vals))
            clauses.append(f'"id" IN ({placeholders})')
            params.extend(id_vals)
        else:
            placeholders = ", ".join(["%s"] * len(vals))
            clauses.append(f'{_facet_value_sql(col)} IN ({placeholders})')
            params.extend(_facet_params(vals))

    for col, term in search.items():
        if col not in searchable_cols or not term:
            continue
        clauses.append(f'"{col}" ILIKE %s')
        params.append(f"%{term}%")

    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where_sql, params


# ============================================================================
# Endpoints
# ============================================================================

@fda_router.get("/summary")
def fda_summary():
    """KPI strip for the FDA dashboard."""
    sql = f"""
        SELECT
            COUNT(*) AS total_matches,
            COUNT(*) FILTER (WHERE UPPER(llm_decision) = 'YES') AS matches_confirmed,
            COUNT(*) FILTER (WHERE UPPER(llm_decision) = 'NO') AS matches_rejected,
            COUNT(DISTINCT fda_sponsor) AS distinct_sponsors,
            COUNT(DISTINCT method) AS distinct_methods,
            AVG(sim_score) AS avg_sim_score,
            MAX(created_at) AS last_created_at
        FROM {TABLE}
    """
    rows = _run(sql)
    row = rows[0] if rows else {}
    total = row.get("total_matches") or 0
    confirmed = row.get("matches_confirmed") or 0
    row["match_rate"] = round((confirmed / total) * 100, 1) if total else None
    if row.get("avg_sim_score") is not None:
        row["avg_sim_score"] = round(float(row["avg_sim_score"]), 3)
    row["sql"] = _render_sql_for_display(sql)
    return row


@fda_router.get("/charts/decision")
def fda_decision_chart():
    """Match count grouped by llm_decision (yes/no)."""
    sql = f"""
        SELECT COALESCE(UPPER(llm_decision), '(unspecified)') AS decision, COUNT(*) AS count
        FROM {TABLE}
        GROUP BY UPPER(llm_decision)
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/charts/method")
def fda_method_chart():
    """Match count grouped by matching method."""
    sql = f"""
        SELECT COALESCE(method, '(unspecified)') AS method, COUNT(*) AS count
        FROM {TABLE}
        GROUP BY method
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/charts/top-sponsors")
def fda_top_sponsors_chart(limit: int = Query(10, ge=1, le=50)):
    """Sponsors with the most matches, from fda_sponsor."""
    sql = f"""
        SELECT fda_sponsor, COUNT(*) AS count
        FROM {TABLE}
        WHERE fda_sponsor IS NOT NULL AND fda_sponsor <> ''
        GROUP BY fda_sponsor
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/facets")
def fda_facets(filters: Optional[str] = Query(None, description="col:val1||val2,col2:val3")):
    """Distinct-value counts for the filter rail (llm_decision, method, fda_status)."""
    parsed = _parse_filters(filters)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for col in FILTERABLE_COLUMNS:
        other_filters = {k: v for k, v in parsed.items() if k != col}
        where_sql, params = _build_where(other_filters, {})
        rows = _run(
            f'SELECT {_facet_value_sql(col)} AS value, COUNT(*) AS count FROM {TABLE} {where_sql} '
            f'GROUP BY 1 ORDER BY count DESC',
            params,
        )
        out[col] = rows
    return out


@fda_router.get("/data")
def fda_data(
    filters: Optional[str] = Query(None),
    search_fda_sponsor: str = Query(""),
    search_fda_product: str = Query(""),
    search_pink_sponsor_product: str = Query(""),
    search_pink_indication: str = Query(""),
    search_fda_file_name: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    parsed = _parse_filters(filters)
    search = {
        "fda_sponsor": search_fda_sponsor or "",
        "fda_product": search_fda_product or "",
        "pink_sponsor_product": search_pink_sponsor_product or "",
        "pink_indication": search_pink_indication or "",
        "fda_file_name": search_fda_file_name or "",
    }
    where_sql, params = _build_where(parsed, search)

    total = _run(f"SELECT COUNT(*) AS c FROM {TABLE} {where_sql}", params)[0]["c"]

    order_col = sort_col if sort_col in ALL_COLUMNS else "created_at"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    offset = (page - 1) * page_size
    cols_sql = ", ".join(f'"{c}"' for c in ALL_COLUMNS)
    data_sql = (
        f'SELECT {cols_sql} FROM {TABLE} {where_sql} '
        f'ORDER BY "{order_col}" {order_dir} NULLS LAST LIMIT %s OFFSET %s'
    )
    data_params = params + [page_size, offset]
    rows = _run(data_sql, data_params)

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "sql": _render_sql_for_display(data_sql, data_params),
    }


@fda_router.get("/data/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def fda_data_download(
    filters: Optional[str] = Query(None),
    search_fda_sponsor: str = Query(""),
    search_fda_product: str = Query(""),
    search_pink_sponsor_product: str = Query(""),
    search_pink_indication: str = Query(""),
    search_fda_file_name: str = Query(""),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    """Streams every matching row (not just the page on screen) as CSV."""
    parsed = _parse_filters(filters)
    search = {
        "fda_sponsor": search_fda_sponsor or "",
        "fda_product": search_fda_product or "",
        "pink_sponsor_product": search_pink_sponsor_product or "",
        "pink_indication": search_pink_indication or "",
        "fda_file_name": search_fda_file_name or "",
    }
    where_sql, params = _build_where(parsed, search)

    order_col = sort_col if sort_col in ALL_COLUMNS else "created_at"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    cols_sql = ", ".join(f'"{c}"' for c in ALL_COLUMNS)
    sql = f'SELECT {cols_sql} FROM {TABLE} {where_sql} ORDER BY "{order_col}" {order_dir} NULLS LAST'

    def generate():
        conn = _connect()
        conn.autocommit = True
        try:
            with conn.cursor(name="fda_download_cursor") as cur:
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
        headers={"Content-Disposition": 'attachment; filename="fda_crl_ai_matches.csv"'},
    )


@fda_router.get("/data/{match_id}")
def fda_data_detail(match_id: int):
    """Single match row, full detail (row-click detail panel)."""
    cols_sql = ", ".join(f'"{c}"' for c in ALL_COLUMNS)
    rows = _run(f'SELECT {cols_sql} FROM {TABLE} WHERE "id" = %s', [match_id])
    if not rows:
        raise HTTPException(status_code=404, detail="Match not found")
    return rows[0]


# ============================================================================
# "CRL Letters" dashboard - vector_embedding.crl_letters
# ============================================================================
# letter_date is stored as free-text "M/D/YYYY" (not a real date column), so
# the year breakdown below pulls the year out with SPLIT_PART instead of a
# date cast, and skips any row that doesn't match that shape.

@fda_router.get("/letters/summary")
def fda_letters_summary():
    """KPI strip for the CRL Letters dashboard."""
    sql = f"""
        SELECT
            COUNT(*) AS total_letters,
            COUNT(*) FILTER (WHERE UPPER(status) = 'APPROVED') AS approved_count,
            COUNT(*) FILTER (WHERE UPPER(status) = 'UNAPPROVED') AS unapproved_count,
            COUNT(DISTINCT sponsor) FILTER (WHERE sponsor IS NOT NULL AND sponsor <> '') AS distinct_sponsors,
            COUNT(DISTINCT product_name) FILTER (WHERE product_name IS NOT NULL AND product_name <> '') AS distinct_products,
            MAX(processed_at) AS last_processed_at
        FROM {LETTERS_TABLE}
    """
    rows = _run(sql)
    row = rows[0] if rows else {}
    total = row.get("total_letters") or 0
    approved = row.get("approved_count") or 0
    row["approval_rate"] = round((approved / total) * 100, 1) if total else None
    row["sql"] = _render_sql_for_display(sql)
    return row


@fda_router.get("/letters/charts/status")
def fda_letters_status_chart():
    """Letter count grouped by approval status."""
    sql = f"""
        SELECT COALESCE(UPPER(status), '(unspecified)') AS status, COUNT(*) AS count
        FROM {LETTERS_TABLE}
        GROUP BY UPPER(status)
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/letters/charts/application-type")
def fda_letters_application_type_chart():
    """Letter count grouped by application type (BLA vs NDA)."""
    sql = f"""
        SELECT COALESCE(UPPER(application_type), '(unspecified)') AS application_type, COUNT(*) AS count
        FROM {LETTERS_TABLE}
        GROUP BY UPPER(application_type)
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/letters/charts/top-sponsors")
def fda_letters_top_sponsors_chart(limit: int = Query(10, ge=1, le=50)):
    """Sponsors with the most CRL letters."""
    sql = f"""
        SELECT sponsor, COUNT(*) AS count
        FROM {LETTERS_TABLE}
        WHERE sponsor IS NOT NULL AND sponsor <> ''
        GROUP BY sponsor
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/letters/charts/by-year")
def fda_letters_by_year_chart():
    """Letter count by year, parsed out of the free-text M/D/YYYY letter_date."""
    sql = f"""
        SELECT SPLIT_PART(letter_date, '/', 3) AS year, COUNT(*) AS count
        FROM {LETTERS_TABLE}
        WHERE letter_date ~ '^\\d{{1,2}}/\\d{{1,2}}/\\d{{4}}$'
        GROUP BY year
        ORDER BY year
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


def _year_clause(years: List[str]) -> tuple[str, list]:
    """WHERE fragment for filtering crl_letters by the year parsed out of the
    free-text letter_date column. Returns ('', []) when no years are given."""
    years = [y for y in years if y]
    if not years:
        return "", []
    placeholders = ", ".join(["%s"] * len(years))
    return f"SPLIT_PART(letter_date, '/', 3) IN ({placeholders})", list(years)


@fda_router.get("/letters/facets")
def fda_letters_facets(filters: Optional[str] = Query(None, description="col:val1||val2,col2:val3"), years: Optional[str] = Query(None, description="comma-separated list of years, e.g. 2023,2024")):
    """Distinct-value counts for the filter rail (status, application_type, year)."""
    parsed = _parse_filters(filters, LETTERS_FILTERABLE_COLUMNS)
    parsed_years = [y.strip() for y in (years or "").split(",") if y.strip()]
    out: Dict[str, List[Dict[str, Any]]] = {}

    for col in LETTERS_FILTERABLE_COLUMNS:
        other_filters = {k: v for k, v in parsed.items() if k != col}
        where_sql, params = _build_where(other_filters, {}, LETTERS_SEARCHABLE_COLUMNS)
        year_sql, year_params = _year_clause(parsed_years)
        if year_sql:
            where_sql = f"{where_sql} AND {year_sql}" if where_sql else f"WHERE {year_sql}"
            params = params + year_params
        if col == "id":
            # bigint - every row is its own facet value, so no GROUP BY/COUNT needed.
            rows = _run(
                f'SELECT CAST("id" AS TEXT) AS value, 1 AS count FROM {LETTERS_TABLE} {where_sql} ORDER BY "id" ASC',
                params,
            )
        else:
            rows = _run(
                f'SELECT {_facet_value_sql(col)} AS value, COUNT(*) AS count FROM {LETTERS_TABLE} {where_sql} '
                f'GROUP BY 1 ORDER BY count DESC',
                params,
            )
        out[col] = rows

    # Year facet is derived (not a real column) so it's built separately -
    # every other active filter applies, year selections don't apply to themselves.
    where_sql, params = _build_where(parsed, {}, LETTERS_SEARCHABLE_COLUMNS)
    year_filter_sql = "letter_date ~ '^\\d{1,2}/\\d{1,2}/\\d{4}$'"
    where_sql = f"{where_sql} AND {year_filter_sql}" if where_sql else f"WHERE {year_filter_sql}"
    out["year"] = _run(
        f"SELECT SPLIT_PART(letter_date, '/', 3) AS value, COUNT(*) AS count FROM {LETTERS_TABLE} {where_sql} "
        f"GROUP BY value ORDER BY value DESC",
        params,
    )
    return out


@fda_router.get("/letters/data")
def fda_letters_data(
    filters: Optional[str] = Query(None),
    years: Optional[str] = Query(None, description="comma-separated list of years, e.g. 2023,2024"),
    search_sponsor: str = Query(""),
    search_product_name: str = Query(""),
    search_file_name: str = Query(""),
    search_submission_id: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    parsed = _parse_filters(filters, LETTERS_FILTERABLE_COLUMNS)
    search = {
        "sponsor": search_sponsor or "",
        "product_name": search_product_name or "",
        "file_name": search_file_name or "",
        "submission_id": search_submission_id or "",
    }
    where_sql, params = _build_where(parsed, search, LETTERS_SEARCHABLE_COLUMNS)
    year_sql, year_params = _year_clause([y.strip() for y in (years or "").split(",") if y.strip()])
    if year_sql:
        where_sql = f"{where_sql} AND {year_sql}" if where_sql else f"WHERE {year_sql}"
        params = params + year_params

    total = _run(f"SELECT COUNT(*) AS c FROM {LETTERS_TABLE} {where_sql}", params)[0]["c"]

    order_col = sort_col if sort_col in LETTERS_ALL_COLUMNS else "processed_at"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    offset = (page - 1) * page_size
    cols_sql = ", ".join(f'"{c}"' for c in LETTERS_ALL_COLUMNS)
    data_sql = (
        f'SELECT {cols_sql} FROM {LETTERS_TABLE} {where_sql} '
        f'ORDER BY "{order_col}" {order_dir} NULLS LAST LIMIT %s OFFSET %s'
    )
    data_params = params + [page_size, offset]
    rows = _run(data_sql, data_params)

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "sql": _render_sql_for_display(data_sql, data_params),
    }


@fda_router.get("/letters/data/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def fda_letters_data_download(
    filters: Optional[str] = Query(None),
    years: Optional[str] = Query(None, description="comma-separated list of years, e.g. 2023,2024"),
    search_sponsor: str = Query(""),
    search_product_name: str = Query(""),
    search_file_name: str = Query(""),
    search_submission_id: str = Query(""),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    """Streams every matching CRL letter (not just the page on screen) as CSV."""
    parsed = _parse_filters(filters, LETTERS_FILTERABLE_COLUMNS)
    search = {
        "sponsor": search_sponsor or "",
        "product_name": search_product_name or "",
        "file_name": search_file_name or "",
        "submission_id": search_submission_id or "",
    }
    where_sql, params = _build_where(parsed, search, LETTERS_SEARCHABLE_COLUMNS)
    year_sql, year_params = _year_clause([y.strip() for y in (years or "").split(",") if y.strip()])
    if year_sql:
        where_sql = f"{where_sql} AND {year_sql}" if where_sql else f"WHERE {year_sql}"
        params = params + year_params

    order_col = sort_col if sort_col in LETTERS_ALL_COLUMNS else "processed_at"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    cols_sql = ", ".join(f'"{c}"' for c in LETTERS_ALL_COLUMNS)
    sql = f'SELECT {cols_sql} FROM {LETTERS_TABLE} {where_sql} ORDER BY "{order_col}" {order_dir} NULLS LAST'

    def generate():
        conn = _connect()
        conn.autocommit = True
        try:
            with conn.cursor(name="fda_letters_download_cursor") as cur:
                cur.itersize = DOWNLOAD_CHUNK_ROWS
                cur.execute(sql, params)
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(LETTERS_ALL_COLUMNS)
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
        headers={"Content-Disposition": 'attachment; filename="fda_crl_letters.csv"'},
    )


@fda_router.get("/letters/data/{letter_id}")
def fda_letters_data_detail(letter_id: int):
    """Single CRL letter, full detail (row-click detail panel)."""
    cols_sql = ", ".join(f'"{c}"' for c in LETTERS_ALL_COLUMNS)
    rows = _run(f'SELECT {cols_sql} FROM {LETTERS_TABLE} WHERE "id" = %s', [letter_id])
    if not rows:
        raise HTTPException(status_code=404, detail="Letter not found")
    return rows[0]


# ============================================================================
# "Pink Sheet Letters" dashboard - vector_embedding.crl_pink_letters
# ============================================================================

def _pink_prefix_sql(db_col: str) -> str:
    """The leading '{category} - ...' prefix of a Pink Sheet text column
    (Indication or Issues), normalized. Both the facet rail and the WHERE
    clause build off this one expression so their values line up."""
    return _facet_expr(f"SPLIT_PART({db_col}, ' - ', 1)")


def _pink_prefix_clause(db_col: str, vals: List[str]) -> tuple[str, list]:
    """WHERE fragment matching that prefix. db_col must already be a quoted
    identifier, e.g. '"Indication"'."""
    vals = [v for v in vals if v]
    if not vals:
        return "", []
    placeholders = ", ".join(["%s"] * len(vals))
    return f"{_pink_prefix_sql(db_col)} IN ({placeholders})", _facet_params(vals)


def _pink_apply_virtual_filters(parsed: Dict[str, List[str]], where_sql: str, params: list) -> tuple[str, list]:
    for key, db_col in (("therapeutic_area", '"Indication"'), ("issue_category", '"Issues"')):
        if not parsed.get(key):
            continue
        clause, cparams = _pink_prefix_clause(db_col, parsed[key])
        where_sql = f"{where_sql} AND {clause}" if where_sql else f"WHERE {clause}"
        params = params + cparams
    return where_sql, params


@fda_router.get("/pink-letters/summary")
def fda_pink_letters_summary():
    """KPI strip for the Pink Sheet Letters dashboard."""
    sql = f"""
        SELECT
            COUNT(*) AS total_letters,
            COUNT(*) FILTER (WHERE "Activity" ILIKE '%%Approved%%') AS approved_mention_count,
            COUNT(DISTINCT NULLIF(TRIM(SPLIT_PART("Indication", ' - ', 1)), '')) AS distinct_therapeutic_areas,
            COUNT(DISTINCT NULLIF(TRIM(SPLIT_PART("Issues", ' - ', 1)), '')) AS distinct_issue_categories
        FROM {PINK_TABLE}
    """
    rows = _run(sql)
    row = rows[0] if rows else {}
    total = row.get("total_letters") or 0
    approved = row.get("approved_mention_count") or 0
    row["approved_mention_rate"] = round((approved / total) * 100, 1) if total else None
    row["sql"] = _render_sql_for_display(sql)
    return row


@fda_router.get("/pink-letters/charts/therapeutic-area")
def fda_pink_letters_therapeutic_area_chart(limit: int = Query(12, ge=1, le=50)):
    """Letter count grouped by the therapeutic area prefix of Indication."""
    sql = f"""
        SELECT COALESCE(NULLIF(TRIM(SPLIT_PART("Indication", ' - ', 1)), ''), '(unspecified)') AS area, COUNT(*) AS count
        FROM {PINK_TABLE}
        GROUP BY area
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/pink-letters/charts/issue-category")
def fda_pink_letters_issue_category_chart(limit: int = Query(12, ge=1, le=50)):
    """Letter count grouped by the issue category prefix of Issues."""
    sql = f"""
        SELECT COALESCE(NULLIF(TRIM(SPLIT_PART("Issues", ' - ', 1)), ''), '(unspecified)') AS category, COUNT(*) AS count
        FROM {PINK_TABLE}
        GROUP BY category
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/pink-letters/charts/approval-outcome")
def fda_pink_letters_approval_outcome_chart():
    """Split of letters whose Activity narrative mentions an approval vs not.
    Activity is free text, not a status code, so this is a keyword read of
    the narrative rather than a structured field - a rough signal, not a
    certified approval status."""
    sql = f"""
        SELECT CASE WHEN "Activity" ILIKE '%%Approved%%' THEN 'MENTIONS APPROVAL' ELSE 'NO APPROVAL MENTIONED' END AS outcome, COUNT(*) AS count
        FROM {PINK_TABLE}
        GROUP BY outcome
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/pink-letters/charts/by-year")
def fda_pink_letters_by_year_chart():
    """Letter count by year - the first 4-digit run found in Date Announced,
    which mixes plain dates, ranges and multi-CRL date lists."""
    sql = f"""
        SELECT SUBSTRING("Date Announced" FROM '\\d{{4}}') AS year, COUNT(*) AS count
        FROM {PINK_TABLE}
        WHERE SUBSTRING("Date Announced" FROM '\\d{{4}}') IS NOT NULL
        GROUP BY year
        ORDER BY year
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/pink-letters/facets")
def fda_pink_letters_facets(filters: Optional[str] = Query(None, description="col:val1||val2,col2:val3")):
    """Distinct-value counts for the two derived filters (therapeutic_area, issue_category)."""
    parsed = _parse_filters(filters, PINK_VIRTUAL_FILTERABLE_COLUMNS)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for key, db_col, label in (
        ("therapeutic_area", '"Indication"', "area"),
        ("issue_category", '"Issues"', "category"),
    ):
        other_filters = {k: v for k, v in parsed.items() if k != key}
        where_sql, params = _pink_apply_virtual_filters(other_filters, "", [])
        rows = _run(
            f"SELECT {_pink_prefix_sql(db_col)} AS value, COUNT(*) AS count "
            f"FROM {PINK_TABLE} {where_sql} GROUP BY 1 ORDER BY count DESC",
            params,
        )
        out[key] = rows
    return out


@fda_router.get("/pink-letters/data")
def fda_pink_letters_data(
    filters: Optional[str] = Query(None),
    search_sponsor_product: str = Query(""),
    search_indication: str = Query(""),
    search_issues: str = Query(""),
    search_activity: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    parsed = _parse_filters(filters, PINK_VIRTUAL_FILTERABLE_COLUMNS)
    search = {
        "Sponsor & Product": search_sponsor_product or "",
        "Indication": search_indication or "",
        "Issues": search_issues or "",
        "Activity": search_activity or "",
    }
    where_sql, params = _build_where({}, search, PINK_SEARCHABLE_COLUMNS)
    where_sql, params = _pink_apply_virtual_filters(parsed, where_sql, params)

    total = _run(f"SELECT COUNT(*) AS c FROM {PINK_TABLE} {where_sql}", params)[0]["c"]

    order_col = sort_col if sort_col in PINK_ALL_COLUMNS else "Sponsor & Product"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    offset = (page - 1) * page_size
    cols_sql = ", ".join(f'"{c}"' for c in PINK_ALL_COLUMNS)
    data_sql = (
        f'SELECT {cols_sql} FROM {PINK_TABLE} {where_sql} '
        f'ORDER BY "{order_col}" {order_dir} NULLS LAST LIMIT %s OFFSET %s'
    )
    data_params = params + [page_size, offset]
    rows = _run(data_sql, data_params)

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "sql": _render_sql_for_display(data_sql, data_params),
    }


@fda_router.get("/pink-letters/data/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def fda_pink_letters_data_download(
    filters: Optional[str] = Query(None),
    search_sponsor_product: str = Query(""),
    search_indication: str = Query(""),
    search_issues: str = Query(""),
    search_activity: str = Query(""),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    """Streams every matching Pink Sheet letter (not just the page on screen) as CSV."""
    parsed = _parse_filters(filters, PINK_VIRTUAL_FILTERABLE_COLUMNS)
    search = {
        "Sponsor & Product": search_sponsor_product or "",
        "Indication": search_indication or "",
        "Issues": search_issues or "",
        "Activity": search_activity or "",
    }
    where_sql, params = _build_where({}, search, PINK_SEARCHABLE_COLUMNS)
    where_sql, params = _pink_apply_virtual_filters(parsed, where_sql, params)

    order_col = sort_col if sort_col in PINK_ALL_COLUMNS else "Sponsor & Product"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    cols_sql = ", ".join(f'"{c}"' for c in PINK_ALL_COLUMNS)
    sql = f'SELECT {cols_sql} FROM {PINK_TABLE} {where_sql} ORDER BY "{order_col}" {order_dir} NULLS LAST'

    def generate():
        conn = _connect()
        conn.autocommit = True
        try:
            with conn.cursor(name="fda_pink_letters_download_cursor") as cur:
                cur.itersize = DOWNLOAD_CHUNK_ROWS
                cur.execute(sql, params)
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(PINK_ALL_COLUMNS)
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
        headers={"Content-Disposition": 'attachment; filename="fda_crl_pink_letters.csv"'},
    )


# ============================================================================
# "eCTD Documents" dashboard - vector_embedding.ectd_docs (~1M rows)
# ============================================================================

@fda_router.get("/ectd/summary")
def fda_ectd_summary():
    """KPI strip for the eCTD Documents dashboard."""
    sql = f"""
        SELECT
            COUNT(*) AS total_docs,
            COUNT(*) FILTER (WHERE UPPER(a_status) = 'APPROVED') AS approved_count,
            COUNT(*) FILTER (WHERE UPPER(a_status) = 'DRAFT') AS draft_count,
            COUNT(DISTINCT NULLIF(jj_prod_disp_name, '')) AS distinct_products,
            COUNT(DISTINCT NULLIF(ectd_section, '')) AS distinct_sections
        FROM {ECTD_TABLE}
    """
    rows = _run(sql)
    row = rows[0] if rows else {}
    total = row.get("total_docs") or 0
    approved = row.get("approved_count") or 0
    row["approval_rate"] = round((approved / total) * 100, 1) if total else None
    row["sql"] = _render_sql_for_display(sql)
    return row


@fda_router.get("/ectd/charts/status")
def fda_ectd_status_chart():
    """Document count grouped by status (Draft vs Approved)."""
    sql = f"""
        SELECT COALESCE(UPPER(a_status), '(unspecified)') AS status, COUNT(*) AS count
        FROM {ECTD_TABLE}
        GROUP BY UPPER(a_status)
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/ectd/charts/primary-group")
def fda_ectd_primary_group_chart():
    """Document count grouped by primary group (Drug Substance vs Drug Product)."""
    sql = f"""
        SELECT COALESCE(UPPER(NULLIF(primary_group, '')), '(unspecified)') AS group_label, COUNT(*) AS count
        FROM {ECTD_TABLE}
        GROUP BY group_label
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/ectd/charts/top-products")
def fda_ectd_top_products_chart(limit: int = Query(12, ge=1, le=50)):
    """Products with the most eCTD documents, from jj_prod_disp_name."""
    sql = f"""
        SELECT jj_prod_disp_name, COUNT(*) AS count
        FROM {ECTD_TABLE}
        WHERE jj_prod_disp_name IS NOT NULL AND jj_prod_disp_name <> ''
        GROUP BY jj_prod_disp_name
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/ectd/charts/top-sections")
def fda_ectd_top_sections_chart(limit: int = Query(12, ge=1, le=50)):
    """eCTD sections with the most documents filed against them."""
    sql = f"""
        SELECT ectd_section, COUNT(*) AS count
        FROM {ECTD_TABLE}
        WHERE ectd_section IS NOT NULL AND ectd_section <> ''
        GROUP BY ectd_section
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/ectd/facets")
def fda_ectd_facets(filters: Optional[str] = Query(None, description="col:val1||val2,col2:val3")):
    """Distinct-value counts for the filter rail (a_status, primary_group, subgroup)."""
    parsed = _parse_filters(filters, ECTD_FILTERABLE_COLUMNS)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for col in ECTD_FILTERABLE_COLUMNS:
        other_filters = {k: v for k, v in parsed.items() if k != col}
        where_sql, params = _build_where(other_filters, {}, ECTD_SEARCHABLE_COLUMNS)
        rows = _run(
            f'SELECT {_facet_value_sql(col)} AS value, COUNT(*) AS count FROM {ECTD_TABLE} {where_sql} '
            f'GROUP BY 1 ORDER BY count DESC',
            params,
        )
        out[col] = rows
    return out


@fda_router.get("/ectd/data")
def fda_ectd_data(
    filters: Optional[str] = Query(None),
    search_jj_title: str = Query(""),
    search_jj_number: str = Query(""),
    search_r_version_label: str = Query(""),
    search_r_object_id: str = Query(""),
    search_object_name: str = Query(""),
    search_link: str = Query(""),
    search_path: str = Query(""),
    search_nas_path: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    parsed = _parse_filters(filters, ECTD_FILTERABLE_COLUMNS)
    search = {
        "jj_title": search_jj_title or "",
        "jj_number": search_jj_number or "",
        "r_version_label": search_r_version_label or "",
        "r_object_id": search_r_object_id or "",
        "object_name": search_object_name or "",
        "Link": search_link or "",
        "Path": search_path or "",
        "nas_path": search_nas_path or "",
    }
    where_sql, params = _build_where(parsed, search, ECTD_SEARCHABLE_COLUMNS)

    total = _run(f"SELECT COUNT(*) AS c FROM {ECTD_TABLE} {where_sql}", params)[0]["c"]

    order_col = sort_col if sort_col in ECTD_ALL_COLUMNS else "jj_number"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    offset = (page - 1) * page_size
    cols_sql = ", ".join(f'"{c}"' for c in ECTD_ALL_COLUMNS)
    data_sql = (
        f'SELECT {cols_sql} FROM {ECTD_TABLE} {where_sql} '
        f'ORDER BY "{order_col}" {order_dir} NULLS LAST LIMIT %s OFFSET %s'
    )
    data_params = params + [page_size, offset]
    rows = _run(data_sql, data_params)

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "sql": _render_sql_for_display(data_sql, data_params),
    }


@fda_router.get("/ectd/data/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def fda_ectd_data_download(
    filters: Optional[str] = Query(None),
    search_jj_title: str = Query(""),
    search_jj_number: str = Query(""),
    search_r_version_label: str = Query(""),
    search_r_object_id: str = Query(""),
    search_object_name: str = Query(""),
    search_link: str = Query(""),
    search_path: str = Query(""),
    search_nas_path: str = Query(""),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    """Streams every matching eCTD document (not just the page on screen) as CSV."""
    parsed = _parse_filters(filters, ECTD_FILTERABLE_COLUMNS)
    search = {
        "jj_title": search_jj_title or "",
        "jj_number": search_jj_number or "",
        "r_version_label": search_r_version_label or "",
        "r_object_id": search_r_object_id or "",
        "object_name": search_object_name or "",
        "Link": search_link or "",
        "Path": search_path or "",
        "nas_path": search_nas_path or "",
    }
    where_sql, params = _build_where(parsed, search, ECTD_SEARCHABLE_COLUMNS)

    order_col = sort_col if sort_col in ECTD_ALL_COLUMNS else "jj_number"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    cols_sql = ", ".join(f'"{c}"' for c in ECTD_ALL_COLUMNS)
    sql = f'SELECT {cols_sql} FROM {ECTD_TABLE} {where_sql} ORDER BY "{order_col}" {order_dir} NULLS LAST'

    def generate():
        conn = _connect()
        conn.autocommit = True
        try:
            with conn.cursor(name="fda_ectd_download_cursor") as cur:
                cur.itersize = DOWNLOAD_CHUNK_ROWS
                cur.execute(sql, params)
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(ECTD_ALL_COLUMNS)
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
        headers={"Content-Disposition": 'attachment; filename="fda_ectd_docs.csv"'},
    )


# ============================================================================
# "FDA CRL Letters (v2)" dashboard - vector_embedding.fda_crl_letters
# ============================================================================

@fda_router.get("/fda-letters/summary")
def fda_letters_v2_summary():
    """KPI strip for the FDA CRL Letters (v2) dashboard."""
    sql = f"""
        SELECT
            COUNT(*) AS total_letters,
            COUNT(*) FILTER (WHERE UPPER(status) = 'APPROVED') AS approved_count,
            COUNT(*) FILTER (WHERE UPPER(status) = 'UNAPPROVED') AS unapproved_count,
            COUNT(DISTINCT sponsor) FILTER (WHERE sponsor IS NOT NULL AND sponsor <> '') AS distinct_sponsors,
            COUNT(DISTINCT indication) FILTER (WHERE indication IS NOT NULL AND indication <> '') AS distinct_indications,
            MAX(processed_at) AS last_processed_at
        FROM {FDA_LETTERS_TABLE}
    """
    rows = _run(sql)
    row = rows[0] if rows else {}
    total = row.get("total_letters") or 0
    approved = row.get("approved_count") or 0
    row["approval_rate"] = round((approved / total) * 100, 1) if total else None
    row["sql"] = _render_sql_for_display(sql)
    return row


@fda_router.get("/fda-letters/charts/status")
def fda_letters_v2_status_chart():
    """Letter count grouped by approval status."""
    sql = f"""
        SELECT COALESCE(UPPER(status), '(unspecified)') AS status, COUNT(*) AS count
        FROM {FDA_LETTERS_TABLE}
        GROUP BY UPPER(status)
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/fda-letters/charts/application-type")
def fda_letters_v2_application_type_chart():
    """Letter count grouped by application type (BLA vs NDA)."""
    sql = f"""
        SELECT COALESCE(UPPER(application_type), '(unspecified)') AS application_type, COUNT(*) AS count
        FROM {FDA_LETTERS_TABLE}
        GROUP BY UPPER(application_type)
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/fda-letters/charts/top-sponsors")
def fda_letters_v2_top_sponsors_chart(limit: int = Query(10, ge=1, le=50)):
    """Sponsors with the most CRL letters."""
    sql = f"""
        SELECT sponsor, COUNT(*) AS count
        FROM {FDA_LETTERS_TABLE}
        WHERE sponsor IS NOT NULL AND sponsor <> ''
        GROUP BY sponsor
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/fda-letters/charts/top-indications")
def fda_letters_v2_top_indications_chart(limit: int = Query(10, ge=1, le=50)):
    """Indications with the most CRL letters - a dimension only available on
    this enriched table, not on the original crl_letters."""
    sql = f"""
        SELECT indication, COUNT(*) AS count
        FROM {FDA_LETTERS_TABLE}
        WHERE indication IS NOT NULL AND indication <> ''
        GROUP BY indication
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/fda-letters/charts/by-year")
def fda_letters_v2_by_year_chart():
    """Letter count by year, from the real crl_date_iso DATE column (no
    text-parsing needed here, unlike crl_letters' free-text letter_date)."""
    sql = f"""
        SELECT EXTRACT(YEAR FROM crl_date_iso)::int AS year, COUNT(*) AS count
        FROM {FDA_LETTERS_TABLE}
        WHERE crl_date_iso IS NOT NULL
        GROUP BY year
        ORDER BY year
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


def _fda_letters_year_clause(years: List[str]) -> tuple[str, list]:
    """WHERE fragment for filtering fda_crl_letters by year - a real DATE
    column here, so this is a plain EXTRACT(), no text parsing needed."""
    years = [y for y in years if y]
    if not years:
        return "", []
    placeholders = ", ".join(["%s"] * len(years))
    return f"EXTRACT(YEAR FROM crl_date_iso)::int IN ({placeholders})", [int(y) for y in years if y.lstrip("-").isdigit()]


@fda_router.get("/fda-letters/facets")
def fda_letters_v2_facets(filters: Optional[str] = Query(None, description="col:val1||val2,col2:val3"), years: Optional[str] = Query(None, description="comma-separated list of years, e.g. 2023,2024")):
    """Distinct-value counts for the filter rail (status, application_type, indication, sponsor_norm, product_norm, year)."""
    parsed = _parse_filters(filters, FDA_LETTERS_FILTERABLE_COLUMNS)
    parsed_years = [y.strip() for y in (years or "").split(",") if y.strip()]
    out: Dict[str, List[Dict[str, Any]]] = {}

    for col in FDA_LETTERS_FILTERABLE_COLUMNS:
        other_filters = {k: v for k, v in parsed.items() if k != col}
        where_sql, params = _build_where(other_filters, {}, FDA_LETTERS_SEARCHABLE_COLUMNS)
        year_sql, year_params = _fda_letters_year_clause(parsed_years)
        if year_sql:
            where_sql = f"{where_sql} AND {year_sql}" if where_sql else f"WHERE {year_sql}"
            params = params + year_params
        rows = _run(
            f'SELECT {_facet_value_sql(col)} AS value, COUNT(*) AS count FROM {FDA_LETTERS_TABLE} {where_sql} '
            f'GROUP BY 1 ORDER BY count DESC',
            params,
        )
        out[col] = rows

    # Year facet is derived (not a real filter column) so it's built
    # separately - every other active filter applies, year selections don't.
    where_sql, params = _build_where(parsed, {}, FDA_LETTERS_SEARCHABLE_COLUMNS)
    year_filter_sql = "crl_date_iso IS NOT NULL"
    where_sql = f"{where_sql} AND {year_filter_sql}" if where_sql else f"WHERE {year_filter_sql}"
    out["year"] = _run(
        f"SELECT EXTRACT(YEAR FROM crl_date_iso)::int::text AS value, COUNT(*) AS count FROM {FDA_LETTERS_TABLE} {where_sql} "
        f"GROUP BY value ORDER BY value DESC",
        params,
    )
    return out


@fda_router.get("/fda-letters/data")
def fda_letters_v2_data(
    filters: Optional[str] = Query(None),
    years: Optional[str] = Query(None, description="comma-separated list of years, e.g. 2023,2024"),
    search_id: str = Query(""),
    search_sponsor: str = Query(""),
    search_product_name: str = Query(""),
    search_submission_id: str = Query(""),
    search_application_number: str = Query(""),
    search_file_name: str = Query(""),
    search_local_path: str = Query(""),
    search_letter_date: str = Query(""),
    search_notes: str = Query(""),
    search_evidence_quote: str = Query(""),
    search_letter_text: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    parsed = _parse_filters(filters, FDA_LETTERS_FILTERABLE_COLUMNS)
    search = {
        "sponsor": search_sponsor or "",
        "product_name": search_product_name or "",
        "submission_id": search_submission_id or "",
        "application_number": search_application_number or "",
        "file_name": search_file_name or "",
        "local_path": search_local_path or "",
        "letter_date": search_letter_date or "",
        "notes": search_notes or "",
        "evidence_quote": search_evidence_quote or "",
        "letter_text": search_letter_text or "",
    }
    where_sql, params = _build_where(parsed, search, FDA_LETTERS_SEARCHABLE_COLUMNS)
    year_sql, year_params = _fda_letters_year_clause([y.strip() for y in (years or "").split(",") if y.strip()])
    if year_sql:
        where_sql = f"{where_sql} AND {year_sql}" if where_sql else f"WHERE {year_sql}"
        params = params + year_params
    if search_id:
        id_clause = 'CAST("id" AS TEXT) ILIKE %s'
        where_sql = f"{where_sql} AND {id_clause}" if where_sql else f"WHERE {id_clause}"
        params = params + [f"%{search_id}%"]

    total = _run(f"SELECT COUNT(*) AS c FROM {FDA_LETTERS_TABLE} {where_sql}", params)[0]["c"]

    order_col = sort_col if sort_col in FDA_LETTERS_ALL_COLUMNS else "processed_at"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    offset = (page - 1) * page_size
    cols_sql = ", ".join(f'"{c}"' for c in FDA_LETTERS_ALL_COLUMNS)
    data_sql = (
        f'SELECT {cols_sql} FROM {FDA_LETTERS_TABLE} {where_sql} '
        f'ORDER BY "{order_col}" {order_dir} NULLS LAST LIMIT %s OFFSET %s'
    )
    data_params = params + [page_size, offset]
    rows = _run(data_sql, data_params)

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "sql": _render_sql_for_display(data_sql, data_params),
    }


@fda_router.get("/fda-letters/data/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def fda_letters_v2_data_download(
    filters: Optional[str] = Query(None),
    years: Optional[str] = Query(None, description="comma-separated list of years, e.g. 2023,2024"),
    search_id: str = Query(""),
    search_sponsor: str = Query(""),
    search_product_name: str = Query(""),
    search_submission_id: str = Query(""),
    search_application_number: str = Query(""),
    search_file_name: str = Query(""),
    search_local_path: str = Query(""),
    search_letter_date: str = Query(""),
    search_notes: str = Query(""),
    search_evidence_quote: str = Query(""),
    search_letter_text: str = Query(""),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    """Streams every matching CRL letter (not just the page on screen) as CSV."""
    parsed = _parse_filters(filters, FDA_LETTERS_FILTERABLE_COLUMNS)
    search = {
        "sponsor": search_sponsor or "",
        "product_name": search_product_name or "",
        "submission_id": search_submission_id or "",
        "application_number": search_application_number or "",
        "file_name": search_file_name or "",
        "local_path": search_local_path or "",
        "letter_date": search_letter_date or "",
        "notes": search_notes or "",
        "evidence_quote": search_evidence_quote or "",
        "letter_text": search_letter_text or "",
    }
    where_sql, params = _build_where(parsed, search, FDA_LETTERS_SEARCHABLE_COLUMNS)
    year_sql, year_params = _fda_letters_year_clause([y.strip() for y in (years or "").split(",") if y.strip()])
    if year_sql:
        where_sql = f"{where_sql} AND {year_sql}" if where_sql else f"WHERE {year_sql}"
        params = params + year_params
    if search_id:
        id_clause = 'CAST("id" AS TEXT) ILIKE %s'
        where_sql = f"{where_sql} AND {id_clause}" if where_sql else f"WHERE {id_clause}"
        params = params + [f"%{search_id}%"]

    order_col = sort_col if sort_col in FDA_LETTERS_ALL_COLUMNS else "processed_at"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    cols_sql = ", ".join(f'"{c}"' for c in FDA_LETTERS_ALL_COLUMNS)
    sql = f'SELECT {cols_sql} FROM {FDA_LETTERS_TABLE} {where_sql} ORDER BY "{order_col}" {order_dir} NULLS LAST'

    def generate():
        conn = _connect()
        conn.autocommit = True
        try:
            with conn.cursor(name="fda_letters_v2_download_cursor") as cur:
                cur.itersize = DOWNLOAD_CHUNK_ROWS
                cur.execute(sql, params)
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(FDA_LETTERS_ALL_COLUMNS)
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
        headers={"Content-Disposition": 'attachment; filename="fda_crl_letters_extended.csv"'},
    )


# ============================================================================
# "Milvexian Metadata" dashboard - vector_embedding.milvexian_metadata
# ============================================================================

@fda_router.get("/milvexian/summary")
def fda_milvexian_summary():
    """KPI strip for the Milvexian Metadata dashboard."""
    sql = f"""
        SELECT
            COUNT(*) AS total_docs,
            COUNT(*) FILTER (WHERE UPPER(a_status) = 'APPROVED') AS approved_count,
            COUNT(*) FILTER (WHERE UPPER(a_status) = 'DRAFT') AS draft_count,
            COUNT(DISTINCT NULLIF(jj_prod_disp_name, '')) AS distinct_products,
            COUNT(DISTINCT NULLIF(ectd_section, '')) AS distinct_sections,
            COUNT(DISTINCT NULLIF(a_content_type, '')) AS distinct_content_types
        FROM {MILVEXIAN_TABLE}
    """
    rows = _run(sql)
    row = rows[0] if rows else {}
    total = row.get("total_docs") or 0
    approved = row.get("approved_count") or 0
    row["approval_rate"] = round((approved / total) * 100, 1) if total else None
    row["sql"] = _render_sql_for_display(sql)
    return row


@fda_router.get("/milvexian/charts/status")
def fda_milvexian_status_chart():
    """Document count grouped by status (Draft vs Approved)."""
    sql = f"""
        SELECT COALESCE(UPPER(a_status), '(unspecified)') AS status, COUNT(*) AS count
        FROM {MILVEXIAN_TABLE}
        GROUP BY UPPER(a_status)
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/milvexian/charts/content-type")
def fda_milvexian_content_type_chart():
    """Document count grouped by content type (e.g. msw15, msw14)."""
    sql = f"""
        SELECT COALESCE(UPPER(NULLIF(a_content_type, '')), '(unspecified)') AS content_type_label, COUNT(*) AS count
        FROM {MILVEXIAN_TABLE}
        GROUP BY content_type_label
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/milvexian/charts/primary-group")
def fda_milvexian_primary_group_chart():
    """Document count grouped by primary group (Drug Substance vs Drug Product)."""
    sql = f"""
        SELECT COALESCE(UPPER(NULLIF(primary_group, '')), '(unspecified)') AS group_label, COUNT(*) AS count
        FROM {MILVEXIAN_TABLE}
        GROUP BY group_label
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/milvexian/charts/top-products")
def fda_milvexian_top_products_chart(limit: int = Query(12, ge=1, le=50)):
    """Products with the most documents, from jj_prod_disp_name."""
    sql = f"""
        SELECT jj_prod_disp_name, COUNT(*) AS count
        FROM {MILVEXIAN_TABLE}
        WHERE jj_prod_disp_name IS NOT NULL AND jj_prod_disp_name <> ''
        GROUP BY jj_prod_disp_name
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/milvexian/charts/top-sections")
def fda_milvexian_top_sections_chart(limit: int = Query(12, ge=1, le=50)):
    """eCTD sections with the most documents filed against them."""
    sql = f"""
        SELECT ectd_section, COUNT(*) AS count
        FROM {MILVEXIAN_TABLE}
        WHERE ectd_section IS NOT NULL AND ectd_section <> ''
        GROUP BY ectd_section
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/milvexian/facets")
def fda_milvexian_facets(filters: Optional[str] = Query(None, description="col:val1||val2,col2:val3")):
    """Distinct-value counts for the filter rail."""
    parsed = _parse_filters(filters, MILVEXIAN_FILTERABLE_COLUMNS)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for col in MILVEXIAN_FILTERABLE_COLUMNS:
        other_filters = {k: v for k, v in parsed.items() if k != col}
        where_sql, params = _build_where(other_filters, {}, MILVEXIAN_SEARCHABLE_COLUMNS)
        rows = _run(
            f'SELECT {_facet_value_sql(col)} AS value, COUNT(*) AS count FROM {MILVEXIAN_TABLE} {where_sql} '
            f'GROUP BY 1 ORDER BY count DESC',
            params,
        )
        out[col] = rows
    return out


@fda_router.get("/milvexian/data")
def fda_milvexian_data(
    filters: Optional[str] = Query(None),
    search_jj_title: str = Query(""),
    search_jj_number: str = Query(""),
    search_r_version_label: str = Query(""),
    search_r_object_id: str = Query(""),
    search_object_name: str = Query(""),
    search_path: str = Query(""),
    search_nas_path: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    parsed = _parse_filters(filters, MILVEXIAN_FILTERABLE_COLUMNS)
    search = {
        "jj_title": search_jj_title or "",
        "jj_number": search_jj_number or "",
        "r_version_label": search_r_version_label or "",
        "r_object_id": search_r_object_id or "",
        "object_name": search_object_name or "",
        "Path": search_path or "",
        "nas_path": search_nas_path or "",
    }
    where_sql, params = _build_where(parsed, search, MILVEXIAN_SEARCHABLE_COLUMNS)

    total = _run(f"SELECT COUNT(*) AS c FROM {MILVEXIAN_TABLE} {where_sql}", params)[0]["c"]

    order_col = sort_col if sort_col in MILVEXIAN_ALL_COLUMNS else "jj_number"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    offset = (page - 1) * page_size
    cols_sql = ", ".join(f'"{c}"' for c in MILVEXIAN_ALL_COLUMNS)
    data_sql = (
        f'SELECT {cols_sql} FROM {MILVEXIAN_TABLE} {where_sql} '
        f'ORDER BY "{order_col}" {order_dir} NULLS LAST LIMIT %s OFFSET %s'
    )
    data_params = params + [page_size, offset]
    rows = _run(data_sql, data_params)

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "sql": _render_sql_for_display(data_sql, data_params),
    }


@fda_router.get("/milvexian/data/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def fda_milvexian_data_download(
    filters: Optional[str] = Query(None),
    search_jj_title: str = Query(""),
    search_jj_number: str = Query(""),
    search_r_version_label: str = Query(""),
    search_r_object_id: str = Query(""),
    search_object_name: str = Query(""),
    search_path: str = Query(""),
    search_nas_path: str = Query(""),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
):
    """Streams every matching document (not just the page on screen) as CSV."""
    parsed = _parse_filters(filters, MILVEXIAN_FILTERABLE_COLUMNS)
    search = {
        "jj_title": search_jj_title or "",
        "jj_number": search_jj_number or "",
        "r_version_label": search_r_version_label or "",
        "r_object_id": search_r_object_id or "",
        "object_name": search_object_name or "",
        "Path": search_path or "",
        "nas_path": search_nas_path or "",
    }
    where_sql, params = _build_where(parsed, search, MILVEXIAN_SEARCHABLE_COLUMNS)

    order_col = sort_col if sort_col in MILVEXIAN_ALL_COLUMNS else "jj_number"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    cols_sql = ", ".join(f'"{c}"' for c in MILVEXIAN_ALL_COLUMNS)
    sql = f'SELECT {cols_sql} FROM {MILVEXIAN_TABLE} {where_sql} ORDER BY "{order_col}" {order_dir} NULLS LAST'

    def generate():
        conn = _connect()
        conn.autocommit = True
        try:
            with conn.cursor(name="fda_milvexian_download_cursor") as cur:
                cur.itersize = DOWNLOAD_CHUNK_ROWS
                cur.execute(sql, params)
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(MILVEXIAN_ALL_COLUMNS)
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
        headers={"Content-Disposition": 'attachment; filename="fda_milvexian_metadata.csv"'},
    )


# ============================================================================
# "RIM Facility Index" dashboard - vector_embedding.rimdocs_facility_index_unified
# ============================================================================

def _facility_prefix_clause(json_col: str, key: str, vals: List[str]) -> tuple[str, list]:
    """WHERE fragment matching a derived value pulled out of address_json
    (city or state), case-insensitive."""
    vals = [v for v in vals if v]
    if not vals:
        return "", []
    placeholders = ", ".join(["%s"] * len(vals))
    return f"UPPER({json_col}->>'{key}') IN ({placeholders})", [v.upper() for v in vals]


def _facility_apply_virtual_filters(parsed: Dict[str, List[str]], where_sql: str, params: list) -> tuple[str, list]:
    for key in ("city", "state"):
        if not parsed.get(key):
            continue
        clause, cparams = _facility_prefix_clause("address_json", key, parsed[key])
        where_sql = f"{where_sql} AND {clause}" if where_sql else f"WHERE {clause}"
        params = params + cparams
    return where_sql, params


@fda_router.get("/facility-index/summary")
def fda_facility_summary():
    """KPI strip for the RIM Facility Index dashboard."""
    sql = f"""
        SELECT
            COUNT(*) AS total_records,
            COUNT(DISTINCT NULLIF(facility_name, '')) AS distinct_facilities,
            COUNT(DISTINCT doc_sha256) AS distinct_documents,
            AVG(confidence) AS avg_confidence,
            COUNT(*) FILTER (WHERE LOWER(placebo_label) = 'yes') AS placebo_count
        FROM {FACILITY_TABLE}
    """
    rows = _run(sql)
    row = rows[0] if rows else {}
    total = row.get("total_records") or 0
    placebo = row.get("placebo_count") or 0
    row["placebo_rate"] = round((placebo / total) * 100, 1) if total else None
    if row.get("avg_confidence") is not None:
        row["avg_confidence"] = round(float(row["avg_confidence"]) * 100, 1)
    row["sql"] = _render_sql_for_display(sql)
    return row


@fda_router.get("/facility-index/charts/top-facilities")
def fda_facility_top_facilities_chart(limit: int = Query(12, ge=1, le=50)):
    """Facilities mentioned in the most extracted records."""
    sql = f"""
        SELECT facility_name, COUNT(*) AS count
        FROM {FACILITY_TABLE}
        WHERE facility_name IS NOT NULL AND facility_name <> ''
        GROUP BY facility_name
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/facility-index/charts/responsibility")
def fda_facility_responsibility_chart(limit: int = Query(12, ge=1, le=50)):
    """Records grouped by facility responsibility (manufacture, testing, etc)."""
    sql = f"""
        SELECT responsibility, COUNT(*) AS count
        FROM {FACILITY_TABLE}
        WHERE responsibility IS NOT NULL AND responsibility <> ''
        GROUP BY responsibility
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/facility-index/charts/placebo-label")
def fda_facility_placebo_label_chart():
    """Split of records flagged as placebo-related vs not."""
    sql = f"""
        SELECT COALESCE(UPPER(placebo_label), '(unspecified)') AS label, COUNT(*) AS count
        FROM {FACILITY_TABLE}
        GROUP BY UPPER(placebo_label)
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/facility-index/charts/top-cities")
def fda_facility_top_cities_chart(limit: int = Query(12, ge=1, le=50)):
    """Cities with the most facility records, pulled out of address_json."""
    sql = f"""
        SELECT address_json->>'city' AS city, COUNT(*) AS count
        FROM {FACILITY_TABLE}
        WHERE address_json->>'city' IS NOT NULL AND address_json->>'city' <> ''
        GROUP BY city
        ORDER BY count DESC
        LIMIT %s
    """
    params = [limit]
    return {"rows": _run(sql, params), "sql": _render_sql_for_display(sql, params)}


@fda_router.get("/facility-index/charts/confidence-band")
def fda_facility_confidence_band_chart():
    """Records grouped into High/Medium/Low confidence bands - a quick read
    on how much of the extraction is safe to trust vs. needs manual review."""
    sql = f"""
        SELECT
            CASE
                WHEN confidence >= 0.9 THEN 'HIGH (0.90+)'
                WHEN confidence >= 0.7 THEN 'MEDIUM (0.70-0.89)'
                WHEN confidence IS NOT NULL THEN 'LOW (<0.70)'
                ELSE '(unspecified)'
            END AS band,
            COUNT(*) AS count
        FROM {FACILITY_TABLE}
        GROUP BY band
        ORDER BY count DESC
    """
    return {"rows": _run(sql), "sql": _render_sql_for_display(sql)}


@fda_router.get("/facility-index/facets")
def fda_facility_facets(filters: Optional[str] = Query(None, description="col:val1||val2,col2:val3")):
    """Distinct-value counts for the filter rail (status, placebo_label, responsibility, mime_type, city, state)."""
    parsed = _parse_filters(filters, FACILITY_FILTERABLE_COLUMNS)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for col in FACILITY_FILTERABLE_COLUMNS:
        other_filters = {k: v for k, v in parsed.items() if k != col}
        where_sql, params = _build_where(other_filters, {}, FACILITY_SEARCHABLE_COLUMNS)
        rows = _run(
            f'SELECT {_facet_value_sql(col)} AS value, COUNT(*) AS count FROM {FACILITY_TABLE} {where_sql} '
            f'GROUP BY 1 ORDER BY count DESC LIMIT 200',
            params,
        )
        out[col] = rows

    # city/state are derived from address_json (not real columns), so they're
    # built separately - every real-column filter applies to them.
    parsed_virtual = _parse_filters(filters, FACILITY_VIRTUAL_FILTERABLE_COLUMNS)
    base_where_sql, base_params = _build_where(parsed, {}, FACILITY_SEARCHABLE_COLUMNS)
    for key in ("city", "state"):
        other_virtual = {k: v for k, v in parsed_virtual.items() if k != key}
        where_sql, params = _facility_apply_virtual_filters(other_virtual, base_where_sql, list(base_params))
        json_clause = f"address_json->>'{key}' IS NOT NULL AND address_json->>'{key}' <> ''"
        where_sql = f"{where_sql} AND {json_clause}" if where_sql else f"WHERE {json_clause}"
        out[key] = _run(
            f"SELECT UPPER(address_json->>'{key}') AS value, COUNT(*) AS count FROM {FACILITY_TABLE} {where_sql} "
            f"GROUP BY UPPER(address_json->>'{key}') ORDER BY count DESC LIMIT 200",
            params,
        )
    return out


@fda_router.get("/facility-index/data")
def fda_facility_data(
    filters: Optional[str] = Query(None),
    search_facility_name: str = Query(""),
    search_address_full: str = Query(""),
    search_file_name: str = Query(""),
    search_doc_sha256: str = Query(""),
    search_nas_path: str = Query(""),
    search_raw_content: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    parsed = _parse_filters(filters, FACILITY_FILTERABLE_COLUMNS)
    parsed_virtual = _parse_filters(filters, FACILITY_VIRTUAL_FILTERABLE_COLUMNS)
    search = {
        "facility_name": search_facility_name or "",
        "address_full": search_address_full or "",
        "file_name": search_file_name or "",
        "doc_sha256": search_doc_sha256 or "",
        "nas_path": search_nas_path or "",
        "raw_content": search_raw_content or "",
    }
    where_sql, params = _build_where(parsed, search, FACILITY_SEARCHABLE_COLUMNS)
    where_sql, params = _facility_apply_virtual_filters(parsed_virtual, where_sql, params)

    total = _run(f"SELECT COUNT(*) AS c FROM {FACILITY_TABLE} {where_sql}", params)[0]["c"]

    order_col = sort_col if sort_col in FACILITY_ALL_COLUMNS else "processed_at"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    offset = (page - 1) * page_size
    cols_sql = (
        ", ".join(f'"{c}"' for c in FACILITY_ALL_COLUMNS)
        + ", address_json->>'city' AS city, address_json->>'state' AS state"
        + ", vector_dims(embedding) AS embedding_dims"
    )
    data_sql = (
        f'SELECT {cols_sql} FROM {FACILITY_TABLE} {where_sql} '
        f'ORDER BY "{order_col}" {order_dir} NULLS LAST LIMIT %s OFFSET %s'
    )
    data_params = params + [page_size, offset]
    rows = _run(data_sql, data_params)

    return {
        "rows": rows,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "sql": _render_sql_for_display(data_sql, data_params),
    }


@fda_router.get("/facility-index/data/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
def fda_facility_data_download(
    filters: Optional[str] = Query(None),
    search_facility_name: str = Query(""),
    search_address_full: str = Query(""),
    search_file_name: str = Query(""),
    search_doc_sha256: str = Query(""),
    search_nas_path: str = Query(""),
    search_raw_content: str = Query(""),
    sort_col: Optional[str] = Query(None),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
):
    """Streams every matching facility record (not just the page on screen) as CSV."""
    parsed = _parse_filters(filters, FACILITY_FILTERABLE_COLUMNS)
    parsed_virtual = _parse_filters(filters, FACILITY_VIRTUAL_FILTERABLE_COLUMNS)
    search = {
        "facility_name": search_facility_name or "",
        "address_full": search_address_full or "",
        "file_name": search_file_name or "",
        "doc_sha256": search_doc_sha256 or "",
        "nas_path": search_nas_path or "",
        "raw_content": search_raw_content or "",
    }
    where_sql, params = _build_where(parsed, search, FACILITY_SEARCHABLE_COLUMNS)
    where_sql, params = _facility_apply_virtual_filters(parsed_virtual, where_sql, params)

    order_col = sort_col if sort_col in FACILITY_ALL_COLUMNS else "processed_at"
    order_dir = "ASC" if sort_dir == "asc" else "DESC"
    download_columns = FACILITY_ALL_COLUMNS + ["city", "state", "embedding_dims"]
    cols_sql = (
        ", ".join(f'"{c}"' for c in FACILITY_ALL_COLUMNS)
        + ", address_json->>'city' AS city, address_json->>'state' AS state"
        + ", vector_dims(embedding) AS embedding_dims"
    )
    sql = f'SELECT {cols_sql} FROM {FACILITY_TABLE} {where_sql} ORDER BY "{order_col}" {order_dir} NULLS LAST'

    def generate():
        conn = _connect()
        conn.autocommit = True
        try:
            with conn.cursor(name="fda_facility_download_cursor") as cur:
                cur.itersize = DOWNLOAD_CHUNK_ROWS
                cur.execute(sql, params)
                buf = io.StringIO()
                writer = csv.writer(buf)
                writer.writerow(download_columns)
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
        headers={"Content-Disposition": 'attachment; filename="fda_rim_facility_index.csv"'},
    )