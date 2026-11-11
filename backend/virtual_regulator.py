"""Virtual Regulator (Internal) tab - DEV_RAW.UNSTRUCTURED_EXPLORER.SUBX_FINAL.

One row per chunk, and every chunk is repeated once per EDMS version of its
document (VERSION_LABEL / MODIFIED / MODIFIED_BY / RANKING come from that join).
So chunks are always counted as COUNT(DISTINCT CHUNK_ID) and "the" metadata for a
document is its latest version.

The generic endpoints (summary, facets, charts, rows, document, chunk) come from
chunk_explorer.py. The business views on top of them live here:
    /products   study cards (one per STUDY_ID)
    /library    one row per document, latest version, paged
    /versions   version history for one document
"""
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import Depends, Query
from fastapi.responses import StreamingResponse

from auth import require_permission
from chunk_explorer import build_chunk_router
from unstructured import _cache_get, _cache_key, _cache_set, _render_sql_for_display

# parallel Snowflake queries per stream; warehouse runs 8 at once by default
STREAM_WORKERS = 6

DOC_COLUMNS = [
    "DOC_ID", "FILE_NAME", "FILE_PATH", "CHUNK_TOTAL", "FILE_SIZE", "PAGES_TOTAL",
    "STATUS", "LAST_MODIFIED", "INGESTED_AT", "CREATED_AT", "UPDATED_AT",
    "OBJ_ID", "NAME", "RANKING", "EDMS_NUMBER", "TITLE", "VERSION_LABEL",
    "MODIFIED", "MODIFIED_BY", "S3_PATH", "STUDY_ID",
]

# MODIFIED is text like "4/24/2025 11:17"
MOD_TS = (
    "COALESCE(TRY_TO_TIMESTAMP_NTZ(\"MODIFIED\", 'MM/DD/YYYY HH24:MI'), "
    "TRY_TO_TIMESTAMP_NTZ(\"MODIFIED\", 'MM/DD/YYYY HH24:MI:SS'), "
    "TRY_TO_TIMESTAMP_NTZ(\"MODIFIED\"))"
)
VER_NUM = 'TRY_TO_DOUBLE("VERSION_LABEL")'
LATEST = f'{VER_NUM} DESC NULLS LAST, {MOD_TS} DESC NULLS LAST, "CHUNK_INDEX"'
# no literal % here - these strings go through pyformat params
HAS_TABLE = "CONTAINS(LOWER(\"CONTENT_TYPES\"), '\"table\"')"
VERSION_KEY = "\"EDMS_NUMBER\" || '|' || \"VERSION_LABEL\""
# "10.3. Pharmacokinetics" and "11.5 Pharmacokinetics" are the same section to a reader
SECTION_NORM = "TRIM(REGEXP_REPLACE(\"SECTION_TITLE\", '^[0-9]+([.][0-9]+)*[.]?[[:space:]]*', ''))"

CHARTS = {
    "recent-docs": lambda b: (
        f'SELECT "DOC_ID", "STUDY_ID", "EDMS_NUMBER", "TITLE", "VERSION_LABEL", '
        f'"MODIFIED_BY", {MOD_TS} AS TS FROM {b} '
        f'QUALIFY ROW_NUMBER() OVER (PARTITION BY "DOC_ID" ORDER BY {LATEST}) = 1 '
        f"ORDER BY TS DESC NULLS LAST LIMIT 8"
    ),
    "owners": lambda b: (
        f'SELECT "MODIFIED_BY", COUNT(DISTINCT {VERSION_KEY}) AS VERS, COUNT(DISTINCT "DOC_ID") '
        f'FROM {b} WHERE "MODIFIED_BY" IS NOT NULL GROUP BY 1 ORDER BY VERS DESC LIMIT 12'
    ),
    "modified-trend": lambda b: (
        f"SELECT DATE_TRUNC('month', {MOD_TS}) AS P, COUNT(DISTINCT {VERSION_KEY}) "
        f"FROM {b} WHERE {MOD_TS} IS NOT NULL GROUP BY P ORDER BY P"
    ),
    "common-sections": lambda b: (
        f"SELECT ANY_VALUE({SECTION_NORM}) AS S, COUNT(DISTINCT \"DOC_ID\") AS DOCS, "
        f'COUNT(DISTINCT "STUDY_ID") AS PRODS FROM {b} '
        f"WHERE \"SECTION_TITLE\" IS NOT NULL AND LENGTH({SECTION_NORM}) BETWEEN 3 AND 90 "
        f"GROUP BY UPPER({SECTION_NORM}) ORDER BY DOCS DESC, PRODS DESC LIMIT 15"
    ),
}

virtual_regulator_router, set_virtual_regulator_dependencies = build_chunk_router({
    "prefix": "/api/virtual-regulator",
    "tag": "Virtual Regulator",
    "cache_name": "virtual_regulator",
    "default_table": "DEV_RAW.UNSTRUCTURED_EXPLORER.SUBX_FINAL",
    "config_key": "virtual_regulator_table",
    "doc_columns": DOC_COLUMNS,
    "filter_columns": ["STUDY_ID", "MODIFIED_BY", "VERSION_LABEL", "STATUS", "CHUNK_LEVEL", "CONTENT_TYPE"],
    "search_columns": ["EDMS_NUMBER", "TITLE", "NAME", "FILE_NAME", "SECTION_TITLE", "CHUNK_TEXT"],
    "distinct_counts": {"products": "STUDY_ID", "edms_docs": "EDMS_NUMBER", "owners": "MODIFIED_BY"},
    "extra_charts": CHARTS,
    "default_dimension": None,
    "default_levels": None,
    "chunk_count": 'COUNT(DISTINCT "CHUNK_ID")',
    "doc_pick_order": LATEST,
})

router = virtual_regulator_router
ctx = router.chunk_ctx
T, base, run, cached, json_rows = ctx["table"], ctx["base"], ctx["query"], ctx["cached"], ctx["json_rows"]
scope_dep = ctx["scope_dep"]
PERM = require_permission("viewUnstructured")


def as_dicts(res):
    cols = [c.upper() for c in res.columns]
    return [dict(zip(cols, r)) for r in json_rows(res)]


def products_sql(f_cte: str) -> str:
    return (
        f"WITH F AS {f_cte}, "
        f'PG AS (SELECT "STUDY_ID", SUM(P) AS PAGES FROM '
        f'(SELECT "STUDY_ID", "DOC_ID", MAX("PAGES_TOTAL") AS P FROM F GROUP BY 1, 2) GROUP BY 1) '
        f'SELECT F."STUDY_ID", COUNT(DISTINCT "DOC_ID") AS DOCS, COUNT(DISTINCT "EDMS_NUMBER") AS EDMS, '
        f"COUNT(DISTINCT {VERSION_KEY}) AS VERSIONS, ANY_VALUE(PG.PAGES) AS PAGES, "
        f"COUNT(DISTINCT \"DOC_ID\" || '|' || \"SECTION_TITLE\") AS SECTIONS, "
        f'COUNT(DISTINCT IFF({HAS_TABLE}, "CHUNK_ID", NULL)) AS TABLES, '
        f'COUNT(DISTINCT "MODIFIED_BY") AS OWNERS, MAX({MOD_TS}) AS LAST_MOD, '
        f'MAX_BY("TITLE", {MOD_TS}) AS LAST_TITLE, MAX_BY("MODIFIED_BY", {MOD_TS}) AS LAST_BY '
        f'FROM F LEFT JOIN PG ON PG."STUDY_ID" IS NOT DISTINCT FROM F."STUDY_ID" '
        f"GROUP BY 1 ORDER BY DOCS DESC"
    )


@router.get("/products")
def get_products(scope: dict = Depends(scope_dep), bypass_cache: bool = Query(False), auth: dict = Depends(PERM)):
    """One card per study (STUDY_ID)."""
    def build():
        sql = products_sql(base(scope["where"]))
        rows = as_dicts(run(sql, scope["params"], "/products"))
        return {
            "products": rows,
            "sql": _render_sql_for_display(sql, scope["params"]),
        }

    return cached("products", scope["key"], bypass_cache, build)


def _ndjson(obj) -> str:
    return json.dumps(obj, default=str) + "\n"


@router.get("/products/stream")
def stream_products(scope: dict = Depends(scope_dep), bypass_cache: bool = Query(False), auth: dict = Depends(PERM)):
    """Same cards as /products, streamed as NDJSON so the page fills in as data lands:
        {"type": "list", "products": [...]}   study ids + doc counts (one cheap query)
        {"type": "product", "product": {...}} full card, one per study, in parallel, as each finishes
        {"type": "done", "sql": "..."}
    The finished set goes into the /products cache, so a repeat visit is instant."""
    where, params = scope["where"], scope["params"]
    full_sql = products_sql(base(where))
    key = _cache_key("virtual_regulator-products", {**scope["key"], "table": T()})

    def gen():
        hit = None if bypass_cache else _cache_get(key)
        if hit is not None:
            yield _ndjson({"type": "list", "products": hit["products"]})
            yield _ndjson({"type": "done", "sql": hit["sql"], "cached": True})
            return
        try:
            quick = (
                f'SELECT "STUDY_ID", COUNT(DISTINCT "DOC_ID") AS DOCS, COUNT(DISTINCT "EDMS_NUMBER") AS EDMS '
                f"FROM {T()} {where} GROUP BY 1 ORDER BY DOCS DESC"
            )
            studies = as_dicts(run(quick, params, "/products/stream list"))
            yield _ndjson({"type": "list", "products": studies})

            def one(sid):
                f = f'(SELECT * FROM {base(where)} x WHERE "STUDY_ID" IS NOT DISTINCT FROM %(vr_sid)s)'
                rows = as_dicts(run(products_sql(f), {**params, "vr_sid": sid}, "/products/stream card"))
                return rows[0] if rows else None

            done = []
            # ponytail: one query per study, fine for tens of studies; batch them if this grows to hundreds
            pool = ThreadPoolExecutor(max_workers=STREAM_WORKERS)
            try:
                futures = [pool.submit(one, s["STUDY_ID"]) for s in studies]
                for fut in as_completed(futures):
                    try:
                        row = fut.result()
                    except Exception:
                        continue  # that card keeps its quick counts; logged in run()
                    if row:
                        done.append(row)
                        yield _ndjson({"type": "product", "product": row})
            finally:
                # user left the page -> drop studies that haven't started yet
                pool.shutdown(wait=False, cancel_futures=True)

            sql = _render_sql_for_display(full_sql, params)
            if len(done) == len(studies):
                done.sort(key=lambda r: r.get("DOCS") or 0, reverse=True)
                _cache_set(key, {"products": done, "sql": sql})
            yield _ndjson({"type": "done", "sql": sql})
        except Exception as e:
            detail = getattr(e, "detail", None) or "Couldn't load this data. Please try again."
            yield _ndjson({"type": "error", "detail": detail})

    # no-transform/X-Accel-Buffering stop proxies holding the stream until it ends
    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


LIBRARY_SORTS = {
    "modified": "MODIFIED_TS", "title": "TITLE", "edms": "EDMS_NUMBER", "product": "STUDY_ID",
    "pages": "PAGES_TOTAL", "versions": "VERSIONS", "sections": "SECTIONS", "tables": "TABLES",
    "matches": "MATCHING",
}
PAGE_FIRST_SORTS = {"MODIFIED_TS", "TITLE", "EDMS_NUMBER", "STUDY_ID", "PAGES_TOTAL", "MATCHING"}


@router.get("/library")
def get_library(
    page: int = Query(1, ge=1, le=100_000),
    page_size: int = Query(25, ge=1, le=100),
    sort: str = Query("modified"),
    sort_dir: str = Query("desc", pattern="^(asc|desc)$"),
    scope: dict = Depends(scope_dep),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(PERM),
):
    """One row per document in scope, showing its latest version. Section / table /
    version counts are for the whole document, MATCHING is chunks that hit the filters."""
    order_col = LIBRARY_SORTS.get(sort, "MODIFIED_TS")
    direction = "ASC" if sort_dir == "asc" else "DESC"

    def build():
        ids = (
            f"WITH F AS {base(scope['where'])}, "
            f'IDS AS (SELECT "DOC_ID", COUNT(DISTINCT "CHUNK_ID") AS MATCHING FROM F GROUP BY 1), '
            f'L AS (SELECT t."DOC_ID", "STUDY_ID", "EDMS_NUMBER", "TITLE", "NAME", "VERSION_LABEL", '
            f'"MODIFIED_BY", {MOD_TS} AS MODIFIED_TS, "PAGES_TOTAL", "FILE_SIZE", "STATUS", "INGESTED_AT", '
            f'"FILE_NAME", "S3_PATH" FROM {T()} t JOIN IDS ON IDS."DOC_ID" = t."DOC_ID" '
            f'QUALIFY ROW_NUMBER() OVER (PARTITION BY t."DOC_ID" ORDER BY {LATEST}) = 1), '
        )
        agg = (
            f'SELECT t."DOC_ID", COUNT(DISTINCT t."VERSION_LABEL") AS VERSIONS, '
            f'COUNT(DISTINCT t."CHUNK_ID") AS CHUNKS, COUNT(DISTINCT t."SECTION_TITLE") AS SECTIONS, '
            f'COUNT(DISTINCT IFF({HAS_TABLE}, t."CHUNK_ID", NULL)) AS TABLES FROM {T()} t '
        )
        order = lambda tbl: f'ORDER BY {order_col} {direction} NULLS LAST, {tbl}."DOC_ID" '
        if order_col in PAGE_FIRST_SORTS:
            # sort key is on the latest-version row, so cut the page first and only
            # count versions/sections/tables for those 25 docs instead of every doc
            sql = (
                ids
                + f'P AS (SELECT L.*, IDS.MATCHING FROM L JOIN IDS ON IDS."DOC_ID" = L."DOC_ID" '
                f"{order('L')}LIMIT %(limit)s OFFSET %(offset)s), "
                f'A AS ({agg}JOIN P ON P."DOC_ID" = t."DOC_ID" GROUP BY 1) '
                f'SELECT P.*, A.VERSIONS, A.CHUNKS, A.SECTIONS, A.TABLES FROM P JOIN A ON A."DOC_ID" = P."DOC_ID" '
                f"{order('P')}"
            )
        else:
            # sorting by an aggregate needs it for every doc
            sql = (
                ids
                + f'A AS ({agg}JOIN IDS ON IDS."DOC_ID" = t."DOC_ID" GROUP BY 1) '
                f"SELECT L.*, A.VERSIONS, A.CHUNKS, A.SECTIONS, A.TABLES, IDS.MATCHING "
                f'FROM L JOIN A ON A."DOC_ID" = L."DOC_ID" JOIN IDS ON IDS."DOC_ID" = L."DOC_ID" '
                f'ORDER BY {order_col} {direction} NULLS LAST, L."DOC_ID" '
                f"LIMIT %(limit)s OFFSET %(offset)s"
            )
        params = {**scope["params"], "limit": page_size, "offset": (page - 1) * page_size}
        count_sql = f'SELECT COUNT(DISTINCT "DOC_ID") FROM {T()} {scope["where"]}'
        # page and total count side by side instead of one after the other
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows_f = pool.submit(run, sql, params, "/library")
            count_f = pool.submit(run, count_sql, scope["params"], "/library count")
            rows, count = as_dicts(rows_f.result()), count_f.result()
        total = count.rows[0][0] if count.rows else 0
        return {
            "rows": rows,
            "total_count": total,
            "page": page,
            "total_pages": max(1, -(-total // page_size)),
            "sql": _render_sql_for_display(sql, params),
        }

    key = {**scope["key"], "page": page, "page_size": page_size, "sort": order_col, "dir": direction}
    return cached("library", key, bypass_cache, build)


@router.get("/versions")
def get_versions(doc_id: str = Query(..., min_length=1), bypass_cache: bool = Query(False), auth: dict = Depends(PERM)):
    """Version history of one document, newest first."""
    def build():
        sql = (
            f'SELECT "VERSION_LABEL", "MODIFIED", "MODIFIED_BY", MAX("RANKING") AS RANKING, '
            f'ANY_VALUE("TITLE") AS TITLE, ANY_VALUE("NAME") AS NAME, {MOD_TS} AS MODIFIED_TS '
            f'FROM {T()} WHERE "DOC_ID" = %(doc_id)s GROUP BY 1, 2, 3 '
            f"ORDER BY {VER_NUM} DESC NULLS LAST, MODIFIED_TS DESC NULLS LAST"
        )
        return {"versions": as_dicts(run(sql, {"doc_id": doc_id}, "/versions"))}

    return cached("versions", {"doc_id": doc_id}, bypass_cache, build)