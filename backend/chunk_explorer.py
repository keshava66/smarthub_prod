"""Shared router for the chunk-level Snowflake explorers (IMMBOT, Virtual Regulator).

Every table here has one row per chunk with the parent document's fields repeated
on each chunk, so "documents" always means COUNT(DISTINCT DOC_ID). Each explorer
just describes its columns in a spec and gets the same set of endpoints.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from auth import require_permission

# Same Snowflake connection, cache and helpers the RimDocs explorer uses.
from unstructured import (
    _cache_get,
    _cache_key,
    _cache_set,
    _get_client,
    _render_sql_for_display,
    _run,
    _to_json_safe,
)

logger = logging.getLogger(__name__)

CHUNK_COLUMNS = [
    "CHUNK_ID", "CHUNK_INDEX", "CHUNK_LEVEL", "SECTION_TITLE",
    "PAGE_START", "PAGE_END", "CONTENT_TYPES", "CHUNK_TEXT",
]

MAX_PAGE_SIZE = 250
MAX_PAGE = 100_000
PREVIEW_CHARS = 400
DOWNLOAD_CHUNK_ROWS = 10_000
CHUNK_BUCKETS = [(10, "1-10"), (25, "11-25"), (50, "26-50"), (100, "51-100"), (200, "101-200"), (500, "201-500")]

# some views write missing values as the text "nan"
BLANKS = "('', 'nan', 'none', 'null')"

# What counts as "the same chunk" for the Distinct chunks option. Views that join
# chunks to metadata repeat each chunk once per joined row, sometimes under a new
# CHUNK_ID, so match on content rather than the id.
CHUNK_KEY = '"DOC_ID", "CHUNK_INDEX", "CHUNK_LEVEL", HASH("CHUNK_TEXT")'

# charts every explorer gets; a spec can add its own through "extra_charts"
BASE_CHARTS = {"ingestion-status", "chunk-level", "content-types", "over-time", "chunks-per-doc", "top-docs"}


_inflight: Dict[str, threading.Lock] = {}
_inflight_guard = threading.Lock()


def _inflight_lock(key: str) -> threading.Lock:
    with _inflight_guard:
        return _inflight.setdefault(key, threading.Lock())


def _inflight_release(key: str, lock: threading.Lock):
    # drop the entry once nobody holds it so the dict doesn't grow per filter combo
    with _inflight_guard:
        if _inflight.get(key) is lock and not lock.locked():
            _inflight.pop(key, None)


def parallel(*calls):
    """Run a few independent queries at once, return results in order."""
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(fn, *args) for fn, *args in calls]
        return [f.result() for f in futures]


def build_chunk_router(spec: Dict[str, Any]) -> Tuple[APIRouter, Callable[[Optional[Dict[str, Any]]], None]]:
    """spec keys:
        prefix, tag, cache_name, default_table, config_key,
        doc_columns        doc-level columns in the table (everything except the chunk ones)
        filter_columns     checkbox facets (CONTENT_TYPE is a virtual one)
        search_columns     free-text "contains" boxes
        distinct_counts    {summary_key: column} extra COUNT(DISTINCT) numbers for the KPI band
        extra_charts       {name: fn(base_sql) -> sql}
        default_dimension  for /chart/by-dimension (None = no such chart)
        default_levels     for /breakdown (None = no breakdown)
        chunk_count        SQL for "how many chunks" (default COUNT(*)); views that repeat
                           a chunk per joined row pass COUNT(DISTINCT "CHUNK_ID")
        doc_pick_order     ORDER BY used to pick the one row shown per document
    Returns (router, set_dependencies). The router also carries `router.chunk_ctx`
    so a table-specific module can hang extra endpoints off the same scope/cache."""
    name = spec["cache_name"]
    router = APIRouter(prefix=spec["prefix"], tags=[spec["tag"]])
    state = {"table": spec["default_table"]}

    doc_columns: List[str] = spec["doc_columns"]
    all_columns = doc_columns + CHUNK_COLUMNS
    filter_columns: List[str] = spec["filter_columns"]
    groupable = [c for c in filter_columns if c != "CONTENT_TYPE"]
    # breakdown adds documents up the tree, so only per-document columns work there
    breakdown_columns = [c for c in groupable if c != "CHUNK_LEVEL"]
    search_columns: List[str] = spec["search_columns"]
    distinct_counts: Dict[str, str] = spec.get("distinct_counts", {})
    extra_charts: Dict[str, Callable[[str], str]] = spec.get("extra_charts", {})
    default_dimension: Optional[str] = spec.get("default_dimension")
    default_levels: Optional[List[str]] = spec.get("default_levels")
    chunk_count: str = spec.get("chunk_count", "COUNT(*)")
    doc_pick_order: str = spec.get("doc_pick_order", '"CHUNK_INDEX"')

    def set_dependencies(config: Optional[Dict[str, Any]]):
        """Called from app.py with the 'unstructured' config section."""
        table = (config or {}).get(spec["config_key"]) or spec["default_table"]
        if not re.fullmatch(r"[A-Za-z0-9_$.]+", table):
            logger.warning(f"{spec['config_key']} '{table}' looks wrong, using {spec['default_table']}")
            table = spec["default_table"]
        state["table"] = table

    def T() -> str:
        return state["table"]

    # ---------------- filters / search ----------------

    def load_obj(raw: Optional[str]) -> Dict[str, Any]:
        if not raw:
            return {}
        try:
            val = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return val if isinstance(val, dict) else {}

    def build_where(filters_raw: Optional[str], search_raw: Optional[str]) -> Tuple[str, Dict[str, Any]]:
        clauses, params = [], {}
        for col, values in load_obj(filters_raw).items():
            col = str(col).upper()
            if col not in filter_columns or not isinstance(values, list):
                continue
            values = [str(v).lower() if isinstance(v, bool) else str(v) for v in values if v not in (None, "")]
            if not values:
                continue
            if col == "CONTENT_TYPE":
                ors = []
                for i, v in enumerate(values):
                    params[f"f_ct_{i}"] = f'%"{v}"%'
                    ors.append(f'"CONTENT_TYPES" ILIKE %(f_ct_{i})s')
                clauses.append(f"({' OR '.join(ors)})")
                continue
            holders = []
            for i, v in enumerate(values):
                params[f"f_{col}_{i}"] = v
                holders.append(f"%(f_{col}_{i})s")
            # TO_VARCHAR so number / boolean columns match the string values the UI sends
            clauses.append(f'TO_VARCHAR("{col}") IN ({", ".join(holders)})')

        for col, text in load_obj(search_raw).items():
            col = str(col).upper()
            text = str(text or "").strip()
            if col not in search_columns or not text:
                continue
            params[f"s_{col}"] = f"%{text}%"
            clauses.append(f'"{col}" ILIKE %(s_{col})s')

        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params

    def scope_dep(
        filters: Optional[str] = Query(None, description='JSON: {"STATUS": ["SUCCESS"], ...}'),
        search: Optional[str] = Query(None, description='JSON: {"FILE_NAME": "0901", ...}'),
    ):
        where_sql, params = build_where(filters, search)
        return {"where": where_sql, "params": params, "key": {"filters": filters or "", "search": search or ""}}

    def base(where_sql: str) -> str:
        return f"(SELECT * FROM {T()} {where_sql})"

    def query(sql: str, params: Dict[str, Any], label: str):
        try:
            return _run(sql, params)
        except FileNotFoundError as e:
            raise HTTPException(status_code=503, detail=str(e))
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"{spec['tag']} {label} failed: {e}")
            raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    def cached(key_name: str, key_parts: Dict[str, Any], bypass: bool, build):
        key = _cache_key(f"{name}-{key_name}", {**key_parts, "table": T()})
        if not bypass:
            hit = _cache_get(key)
            if hit is not None:
                return {**hit, "cached": True}
        # single-flight: identical requests arriving together (two users, a
        # remount, a double click) wait for the first one instead of all
        # running the same Snowflake query
        lock = _inflight_lock(key)
        try:
            with lock:
                if not bypass:
                    hit = _cache_get(key)
                    if hit is not None:
                        return {**hit, "cached": True}
                payload = build()
                _cache_set(key, payload)
                return {**payload, "cached": False}
        finally:
            _inflight_release(key, lock)

    def json_rows(result) -> List[List[Any]]:
        return [[_to_json_safe(v) for v in r] for r in result.rows]

    # ---------------- summary / facets ----------------

    @router.get("/summary")
    def get_summary(
        scope: dict = Depends(scope_dep),
        bypass_cache: bool = Query(False),
        auth: dict = Depends(require_permission("viewUnstructured")),
    ):
        """Headline numbers. Size / pages are summed once per document, not per chunk."""
        def build():
            extras = list(distinct_counts.items())
            extra_sql = "".join(f', (SELECT COUNT(DISTINCT "{col}") FROM F)' for _, col in extras)
            sql = (
                f"WITH F AS {base(scope['where'])}, "
                f'D AS (SELECT "DOC_ID", MAX("FILE_SIZE") AS FS, MAX("PAGES_TOTAL") AS PG, '
                f'MAX(UPPER("STATUS")) AS ST FROM F GROUP BY "DOC_ID") '
                f'SELECT (SELECT {chunk_count} FROM F), (SELECT COUNT(DISTINCT "FILE_NAME") FROM F), '
                f"COUNT(*), SUM(FS), SUM(PG), SUM(IFF(ST = 'SUCCESS', 1, 0)){extra_sql} FROM D"
            )
            res = query(sql, scope["params"], "/summary")
            r = res.rows[0] if res.rows else [0] * (6 + len(extras))
            out = {
                "chunks": r[0] or 0,
                "files": r[1] or 0,
                "documents": r[2] or 0,
                "size_bytes": int(r[3] or 0),
                "pages": int(r[4] or 0),
                "success_documents": int(r[5] or 0),
                "sql": _render_sql_for_display(sql, scope["params"]),
            }
            for i, (key, _) in enumerate(extras):
                out[key] = r[6 + i] or 0
            return out

        return cached("summary", scope["key"], bypass_cache, build)

    @router.get("/filter-options")
    def get_filter_options(
        column: str = Query(...),
        bypass_cache: bool = Query(False),
        auth: dict = Depends(require_permission("viewUnstructured")),
    ):
        """Distinct values + document count for one facet (whole table, not filtered)."""
        column = column.upper()
        if column not in filter_columns:
            raise HTTPException(status_code=400, detail=f"'{column}' is not a filterable column")

        def build():
            if column == "CONTENT_TYPE":
                sql = (
                    f'SELECT f.value::STRING AS V, COUNT(DISTINCT t."DOC_ID") AS CNT '
                    f'FROM {T()} t, LATERAL FLATTEN(input => TRY_PARSE_JSON(t."CONTENT_TYPES")) f '
                    f"GROUP BY V ORDER BY CNT DESC"
                )
            else:
                sql = (
                    f'SELECT TO_VARCHAR("{column}") AS V, COUNT(DISTINCT "DOC_ID") AS CNT FROM {T()} '
                    f'WHERE "{column}" IS NOT NULL AND LOWER(TRIM(TO_VARCHAR("{column}"))) NOT IN {BLANKS} '
                    f"GROUP BY V ORDER BY CNT DESC LIMIT 1000"
                )
            res = query(sql, {}, "/filter-options")
            return {"column": column, "values": [{"value": r[0], "count": r[1]} for r in res.rows]}

        return cached("filter-options", {"column": column}, bypass_cache, build)

    # ---------------- charts ----------------

    def chart_sql(chart: str, b: str, granularity: str, dimension: str) -> str:
        if chart in extra_charts:
            return extra_charts[chart](b)
        if chart == "ingestion-status":
            return f'SELECT "STATUS", COUNT(DISTINCT "DOC_ID") AS CNT FROM {b} GROUP BY 1 ORDER BY CNT DESC'
        if chart == "chunk-level":
            return f'SELECT "CHUNK_LEVEL", {chunk_count} AS CNT FROM {b} GROUP BY 1 ORDER BY CNT DESC'
        if chart == "content-types":
            return (
                f"SELECT f.value::STRING AS V, {chunk_count} AS CNT FROM {b} s, "
                f'LATERAL FLATTEN(input => TRY_PARSE_JSON(s."CONTENT_TYPES")) f '
                f"GROUP BY V ORDER BY CNT DESC"
            )
        if chart == "over-time":
            return (
                f"SELECT DATE_TRUNC('{granularity}', \"INGESTED_AT\") AS P, COUNT(DISTINCT \"DOC_ID\") "
                f'FROM {b} WHERE "INGESTED_AT" IS NOT NULL GROUP BY P ORDER BY P'
            )
        if chart == "chunks-per-doc":
            cases = " ".join(f"WHEN N <= {hi} THEN '{label}'" for hi, label in CHUNK_BUCKETS)
            return (
                f"SELECT CASE {cases} ELSE '500+' END AS B, MIN(N) AS ORD, COUNT(*) "
                f'FROM (SELECT "DOC_ID", {chunk_count} AS N FROM {b} GROUP BY "DOC_ID") '
                f"GROUP BY B ORDER BY ORD"
            )
        if chart == "top-docs":
            # biggest documents by chunk count, with pages alongside
            return (
                f'SELECT ANY_VALUE("FILE_NAME"), MAX("CHUNK_TOTAL") AS CH, MAX("PAGES_TOTAL"), "DOC_ID" '
                f'FROM {b} GROUP BY "DOC_ID" ORDER BY CH DESC NULLS LAST LIMIT 15'
            )
        if chart == "by-dimension" and default_dimension:
            return (
                f'SELECT "{dimension}", COUNT(DISTINCT "DOC_ID") AS DOCS, {chunk_count} AS CHUNKS '
                f"FROM {b} GROUP BY 1 ORDER BY DOCS DESC LIMIT 15"
            )
        raise HTTPException(status_code=404, detail=f"Unknown chart '{chart}'")

    @router.get("/chart/{chart}")
    def get_chart(
        chart: str,
        granularity: str = Query("day"),
        dimension: str = Query(""),
        scope: dict = Depends(scope_dep),
        bypass_cache: bool = Query(False),
        auth: dict = Depends(require_permission("viewUnstructured")),
    ):
        granularity = granularity.lower()
        if granularity not in ("hour", "day", "week", "month"):
            granularity = "day"
        dimension = dimension.upper()
        if dimension not in groupable:
            dimension = default_dimension or ""

        def build():
            sql = chart_sql(chart, base(scope["where"]), granularity, dimension)
            res = query(sql, scope["params"], f"/chart/{chart}")
            rows = json_rows(res)
            if chart == "chunks-per-doc":
                rows = [[r[0], r[2]] for r in rows]  # drop the sort helper column
            return {"rows": rows, "sql": _render_sql_for_display(sql, scope["params"])}

        key = {**scope["key"], "chart": chart, "granularity": granularity, "dimension": dimension}
        return cached("chart", key, bypass_cache, build)

    # ---------------- data table ----------------

    def rows_sql(mode: str, where_sql: str, sort_col: str, sort_dir: str, full_text: bool,
                 distinct_chunks: bool = False) -> Tuple[str, str]:
        direction = "ASC" if sort_dir == "asc" else "DESC"
        if mode == "documents":
            doc_cols = ", ".join(f'"{c}"' for c in doc_columns)
            inner = (
                f'SELECT {doc_cols}, {chunk_count} OVER (PARTITION BY "DOC_ID") AS "MATCHING_CHUNKS" '
                f"FROM {T()} {where_sql} "
                f'QUALIFY ROW_NUMBER() OVER (PARTITION BY "DOC_ID" ORDER BY {doc_pick_order}) = 1'
            )
            data_sql = f'SELECT * FROM ({inner}) ORDER BY "{sort_col}" {direction} NULLS LAST, "DOC_ID"'
            count_sql = f'SELECT COUNT(DISTINCT "DOC_ID") FROM {T()} {where_sql}'
        else:
            cols = [f'"{c}"' for c in all_columns if c != "CHUNK_TEXT"]
            cols.append('"CHUNK_TEXT"' if full_text else f'LEFT("CHUNK_TEXT", {PREVIEW_CHARS}) AS "CHUNK_TEXT"')
            if distinct_chunks:
                # one row per chunk; only this chunk list changes, nothing else on the page
                inner = (
                    f"SELECT {', '.join(cols)} FROM {T()} {where_sql} "
                    f"QUALIFY ROW_NUMBER() OVER (PARTITION BY {CHUNK_KEY} ORDER BY \"CHUNK_ID\") = 1"
                )
                data_sql = f'SELECT * FROM ({inner}) ORDER BY "{sort_col}" {direction} NULLS LAST, "DOC_ID", "CHUNK_INDEX"'
                count_sql = f"SELECT COUNT(*) FROM (SELECT DISTINCT {CHUNK_KEY} FROM {T()} {where_sql})"
            else:
                data_sql = (
                    f"SELECT {', '.join(cols)} FROM {T()} {where_sql} "
                    f'ORDER BY "{sort_col}" {direction} NULLS LAST, "DOC_ID", "CHUNK_INDEX"'
                )
                count_sql = f"SELECT COUNT(*) FROM {T()} {where_sql}"
        return data_sql, count_sql

    def check_sort(mode: str, sort_col: str) -> str:
        allowed = doc_columns + ["MATCHING_CHUNKS"] if mode == "documents" else all_columns
        sort_col = sort_col.upper()
        return sort_col if sort_col in allowed else "FILE_NAME"

    @router.get("/rows")
    def get_rows(
        mode: str = Query("documents", pattern="^(documents|chunks)$"),
        page: int = Query(1, ge=1, le=MAX_PAGE),
        page_size: int = Query(50, ge=1, le=MAX_PAGE_SIZE),
        sort_col: str = Query("FILE_NAME"),
        sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
        distinct_chunks: bool = Query(False, description="chunks mode only: drop repeated chunks"),
        scope: dict = Depends(scope_dep),
        bypass_cache: bool = Query(False),
        auth: dict = Depends(require_permission("viewUnstructured")),
    ):
        """One page of documents (one row per DOC_ID) or chunks. CHUNK_TEXT is cut
        to a preview here - /chunk returns the full text."""
        sort_col = check_sort(mode, sort_col)

        def build():
            data_sql, count_sql = rows_sql(mode, scope["where"], sort_col, sort_dir, full_text=False,
                                           distinct_chunks=distinct_chunks and mode == "chunks")
            params = {**scope["params"], "limit": page_size, "offset": (page - 1) * page_size}
            data_sql = f"{data_sql} LIMIT %(limit)s OFFSET %(offset)s"
            t0 = time.time()
            data, count = parallel(
                (query, data_sql, params, "/rows"),
                (query, count_sql, scope["params"], "/rows count"),
            )
            total = count.rows[0][0] if count.rows else 0
            return {
                "columns": [c.upper() for c in data.columns],
                "rows": json_rows(data),
                "total_count": total,
                "page": page,
                "page_size": page_size,
                "total_pages": max(1, -(-total // page_size)),
                "elapsed_ms": int((time.time() - t0) * 1000),
                "query_id": data.query_id,
                "sql": _render_sql_for_display(data_sql, params),
                "count_sql": _render_sql_for_display(count_sql, scope["params"]),
            }

        key = {**scope["key"], "mode": mode, "page": page, "page_size": page_size, "sort_col": sort_col, "sort_dir": sort_dir,
               "distinct_chunks": distinct_chunks and mode == "chunks"}
        return cached("rows", key, bypass_cache, build)

    @router.get("/rows/download", dependencies=[Depends(require_permission("downloadUnstructuredData"))])
    def download_rows(
        mode: str = Query("documents", pattern="^(documents|chunks)$"),
        sort_col: str = Query("FILE_NAME"),
        sort_dir: str = Query("asc", pattern="^(asc|desc)$"),
        distinct_chunks: bool = Query(False),
        scope: dict = Depends(scope_dep),
        auth: dict = Depends(require_permission("viewUnstructured")),
    ):
        """Every matching row as CSV (full chunk text), streamed in batches."""
        sort_col = check_sort(mode, sort_col)
        sql, _ = rows_sql(mode, scope["where"], sort_col, sort_dir, full_text=True,
                          distinct_chunks=distinct_chunks and mode == "chunks")
        params = scope["params"]

        def generate():
            cur = _get_client().conn.cursor()
            try:
                cur.execute(sql, params)
                buf = io.StringIO()
                csv.writer(buf).writerow([c[0] for c in cur.description])
                yield buf.getvalue()
                while True:
                    batch = cur.fetchmany(DOWNLOAD_CHUNK_ROWS)
                    if not batch:
                        break
                    buf = io.StringIO()
                    w = csv.writer(buf)
                    for row in batch:
                        w.writerow(["" if v is None else _to_json_safe(v) for v in row])
                    yield buf.getvalue()
            finally:
                cur.close()

        return StreamingResponse(
            generate(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{name}_{mode}.csv"'},
        )

    @router.get("/document")
    def get_document(
        doc_id: str = Query(..., min_length=1),
        distinct_chunks: bool = Query(False, description="drop repeated chunks from the outline"),
        bypass_cache: bool = Query(False),
        auth: dict = Depends(require_permission("viewUnstructured")),
    ):
        """Metadata for one document plus its chunk outline (preview text only)."""
        def build():
            params = {"doc_id": doc_id}
            doc_cols = ", ".join(f'"{c}"' for c in doc_columns)
            meta_sql = (
                f'SELECT {doc_cols} FROM {T()} WHERE "DOC_ID" = %(doc_id)s '
                f"ORDER BY {doc_pick_order} LIMIT 1"
            )
            chunks_sql = (
                f'SELECT "CHUNK_ID", "CHUNK_INDEX", "CHUNK_LEVEL", "SECTION_TITLE", "PAGE_START", "PAGE_END", '
                f'"CONTENT_TYPES", LEFT("CHUNK_TEXT", 280) AS "PREVIEW" FROM {T()} '
                f'WHERE "DOC_ID" = %(doc_id)s '
                + (f'QUALIFY ROW_NUMBER() OVER (PARTITION BY {CHUNK_KEY} ORDER BY "CHUNK_ID") = 1 ' if distinct_chunks else "")
                + 'ORDER BY "CHUNK_INDEX" LIMIT 5000'
            )
            meta, chunks = parallel(
                (query, meta_sql, params, "/document"),
                (query, chunks_sql, params, "/document chunks"),
            )
            if not meta.rows:
                raise HTTPException(status_code=404, detail="Document not found")
            cols = [c.upper() for c in chunks.columns]
            return {
                "document": dict(zip([c.upper() for c in meta.columns], [_to_json_safe(v) for v in meta.rows[0]])),
                "chunks": [dict(zip(cols, r)) for r in json_rows(chunks)],
                "sql": _render_sql_for_display(chunks_sql, params),
            }

        return cached("document", {"doc_id": doc_id, "distinct_chunks": distinct_chunks}, bypass_cache, build)

    @router.get("/chunk")
    def get_chunk(
        chunk_id: str = Query(..., min_length=1),
        auth: dict = Depends(require_permission("viewUnstructured")),
    ):
        """One chunk with its full CHUNK_TEXT."""
        def build():
            sql = f'SELECT * FROM {T()} WHERE "CHUNK_ID" = %(chunk_id)s LIMIT 1'
            res = query(sql, {"chunk_id": chunk_id}, "/chunk")
            if not res.rows:
                raise HTTPException(status_code=404, detail="Chunk not found")
            return {"chunk": dict(zip([c.upper() for c in res.columns], [_to_json_safe(v) for v in res.rows[0]]))}

        return cached("chunk", {"chunk_id": chunk_id}, False, build)

    # ---------------- breakdown ----------------

    if default_levels:
        @router.get("/breakdown")
        def get_breakdown(
            levels: str = Query(",".join(default_levels), description="three comma separated columns"),
            scope: dict = Depends(scope_dep),
            bypass_cache: bool = Query(False),
            auth: dict = Depends(require_permission("viewUnstructured")),
        ):
            """Three-level drill-down, counted by distinct DOC_ID (plus chunk count)."""
            cols = [c.strip().upper() for c in levels.split(",") if c.strip()]
            if len(cols) != 3 or any(c not in breakdown_columns for c in cols) or len(set(cols)) != 3:
                raise HTTPException(status_code=400, detail=f"levels needs three different columns from {breakdown_columns}")

            def build():
                a, b, c = (f'"{x}"' for x in cols)
                sql = (
                    f'SELECT {a}, {b}, {c}, COUNT(DISTINCT "DOC_ID"), {chunk_count} FROM {base(scope["where"])} '
                    f"GROUP BY 1, 2, 3 ORDER BY 1, 2, 3 LIMIT 20000"
                )
                res = query(sql, scope["params"], "/breakdown")
                return {"levels": cols, "rows": json_rows(res), "sql": _render_sql_for_display(sql, scope["params"])}

            return cached("breakdown", {**scope["key"], "levels": ",".join(cols)}, bypass_cache, build)

    router.chunk_ctx = {
        "table": T,
        "scope_dep": scope_dep,
        "base": base,
        "query": query,
        "cached": cached,
        "json_rows": json_rows,
        "chunk_key": CHUNK_KEY,
    }
    return router, set_dependencies