import asyncio
import io
import json
import logging
import math
import re
import textwrap
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict

import pandas as pd
import pytz
import xlsxwriter
from fastapi import APIRouter, HTTPException, Query, Depends

from auth import require_permission, bdc_datasets
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)
EASTERN = pytz.timezone("America/New_York")

# ── Injected dependencies (set once at startup by Backend.py) ────────────────
_bdc_config_manager = None
_bdc_connection_manager = None
# callable() -> raw snowflake.connector connection
# Used to open short-lived parallel write connections without touching the
# shared connection pool managed by BDCConnectionManager.
_connection_factory = None

# callable() -> raw snowflake.connector connection
# Uses write-specific credentials (e.g. DEV_WRITE from the config).
# All Snowflake write operations (DDL, INSERT, registry) use this factory so
# that reads and writes are performed under separate credential sets.
_write_connection_factory = None

# In-memory Excel cache: { token: (expires_at, bytes) }. A token can be used
# more than once on purpose, but only for a while: these used to stay until a
# restart, so every prepared workbook (often many MB) stayed in memory.
_CONSOLIDATED_TTL = 30 * 60
_consolidated_cache: Dict[str, tuple] = {}
_consolidated_lock = threading.Lock()


def _cache_put(token: str, data: bytes) -> None:
    now = time.time()
    with _consolidated_lock:
        for k in [k for k, (exp, _) in _consolidated_cache.items() if exp <= now]:
            del _consolidated_cache[k]
        _consolidated_cache[token] = (now + _CONSOLIDATED_TTL, data)


def _cache_get(token: str):
    with _consolidated_lock:
        hit = _consolidated_cache.get(token)
    return hit[1] if hit and hit[0] > time.time() else None

# ── Snowflake persistence ─────────────────────────────────────────────────────

REGISTRY_TABLE       = "CONSOLIDATED_REPORT_REGISTRY"
DELTA_REGISTRY_TABLE = "CONSOLIDATED_DELTA_REPORT_REGISTRY"


def _ensure_registry_table(conn, full_registry_name: str):
    """Create the consolidated report registry table if it does not already exist.

    ``conn`` must be a raw ``snowflake.connector`` connection (not a
    SnowflakeClient wrapper) opened under write credentials.
    """
    cur = conn.cursor()
    try:
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS {full_registry_name} (
                REPORT_NAME             VARCHAR(500)   NOT NULL PRIMARY KEY,
                GENERATED_AT            TIMESTAMP_TZ   NOT NULL,
                TOTAL_ROWS              NUMBER         NOT NULL,
                TOTAL_ROWS_BEFORE_DEDUP NUMBER         NOT NULL,
                DUPLICATES_REMOVED      NUMBER         NOT NULL,
                RULES_COUNT             NUMBER         NOT NULL,
                COLUMNS_LIST            VARCHAR(65535) NOT NULL,
                RULE_COLOR_MAP          VARCHAR(65535) NOT NULL,
                RULE_COUNTS             VARCHAR(65535) NOT NULL,
                RULES_JSON              VARCHAR(65535) NOT NULL
            )
        """)
    finally:
        cur.close()


def _write_report_to_snowflake_gen(
        sf,
        database: str,
        schema: str,
        report_name: str,
        result_df: pd.DataFrame,
        total_before: int,
        rules: list,
        rule_row_counts: dict,
        rule_color_map: dict,
        registry_table_name: str = REGISTRY_TABLE,
):
    """
    Generator version of the Snowflake write — yields (pct, label) progress
    tuples in the 82-96 range so callers can stream progress via SSE.

    Persist a generated consolidated report to Snowflake:
      1. Ensure the registry table exists.
      2. CREATE the per-report data table.
      3. INSERT all rows using parallel worker connections.
      4. Register metadata in CONSOLIDATED_REPORT_REGISTRY.

    Row insertion uses up to MAX_WORKERS independent Snowflake connections writing
    non-overlapping row slices concurrently, which avoids the single-cursor
    sequential bottleneck on large datasets.
    """
    full_report_table   = f"{database}.{schema}.{report_name}"
    full_registry_table = f"{database}.{schema}.{registry_table_name}"

    # ── Open a dedicated write connection ─────────────────────────────────────
    # All DDL and INSERT operations use write credentials (_write_connection_factory
    # / DEV_WRITE in the config).  The ``sf`` argument is kept for any future
    # read-only lookups that may be added inside this generator.
    if _write_connection_factory is None:
        raise RuntimeError(
            "No write_connection_factory configured — cannot persist report to Snowflake."
        )
    write_conn = _write_connection_factory()

    try:
        yield (82, "Ensuring registry table…")
        _ensure_registry_table(write_conn, full_registry_table)

        # ── Create the target table ───────────────────────────────────────────
        yield (83, "Creating report table in Snowflake…")
        col_ddl = ", ".join(
            f'"{c.upper().replace(" ", "_")}" VARCHAR'
            for c in result_df.columns
        )
        cur = write_conn.cursor()
        try:
            cur.execute(f"CREATE TABLE {full_report_table} ({col_ddl})")
        finally:
            cur.close()

        # ── Serialise all rows once on the calling thread ─────────────────────
        yield (84, "Serializing rows…")
        cols = [c.upper().replace(" ", "_") for c in result_df.columns]
        col_names = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join(["%s"] * len(cols))
        insert_sql = f"INSERT INTO {full_report_table} ({col_names}) VALUES ({placeholders})"

        # ── Vectorized serialization ──────────────────────────────────────────
        import numpy as _np

        # Step 1 — one C-level pass: box everything to Python objects,
        # NaN / NaT / pd.NA → None.  Same as the original fast path.
        arr = result_df.to_numpy(dtype=object, na_value=None)

        # Step 2 — datetime columns: overwrite the pd.Timestamp cells already
        # in arr with ISO strings.  result_df.iloc[:, _ci] is a zero-copy
        # Series view; dt.strftime() runs in Cython on that single column.
        # No DataFrame copy anywhere — only one column's worth of memory per
        # datetime column is allocated.
        for _ci, _dt in enumerate(result_df.dtypes):
            if pd.api.types.is_datetime64_any_dtype(_dt):
                arr[:, _ci] = (
                    result_df.iloc[:, _ci]
                    .dt.strftime('%Y-%m-%d %H:%M:%S')
                    .to_numpy(dtype=object, na_value=None)
                )

        # Step 3 — object columns only: stringify any rare non-primitive
        # values (Decimal, custom types, etc.).  All-string columns are free.
        _obj_col_idxs = [i for i, dt in enumerate(result_df.dtypes) if dt == object]
        for _ci in _obj_col_idxs:
            _col = arr[:, _ci]
            _bad = [
                _ri for _ri, _v in enumerate(_col)
                if _v is not None and not isinstance(_v, (str, int, float, bool))
            ]
            for _ri in _bad:
                _col[_ri] = str(_col[_ri])

        all_rows = list(map(tuple, arr))

        total_rows = len(all_rows)

        # ── Decide worker count ───────────────────────────────────────────────
        # One worker per ~25 000 rows, capped at 8.  Falls back to 1 if no
        # write connection factory was injected (legacy path).
        MAX_WORKERS = 8
        ROWS_PER_CHUNK = 25_000
        BATCH_PER_EXEC = 10_000  # rows per executemany call inside each worker

        can_parallel = _write_connection_factory is not None and total_rows > ROWS_PER_CHUNK
        n_workers = min(MAX_WORKERS, math.ceil(total_rows / ROWS_PER_CHUNK)) if can_parallel else 1

        # Split rows into equal-sized chunks (one per worker)
        chunk_size = math.ceil(total_rows / n_workers)
        chunks = [all_rows[i: i + chunk_size] for i in range(0, total_rows, chunk_size)]

        logger.info(
            f"Snowflake write: {total_rows:,} rows → {len(chunks)} worker(s) "
            f"× ~{chunk_size:,} rows each"
        )

        def _write_chunk(chunk: list, worker_idx: int) -> int:
            """
            Open a dedicated Snowflake write connection, insert *chunk* in
            batches, then close.  Returns the number of rows written.
            """
            conn = _write_connection_factory()
            try:
                cur = conn.cursor()
                try:
                    for start in range(0, len(chunk), BATCH_PER_EXEC):
                        cur.executemany(insert_sql, chunk[start: start + BATCH_PER_EXEC])
                finally:
                    cur.close()
            finally:
                conn.close()
            logger.debug(f"Worker {worker_idx}: wrote {len(chunk):,} rows")
            return len(chunk)

        # ── Insert rows with per-batch/per-worker progress ────────────────────
        # pct range: 85 → 93  (8 points for the insert phase)
        if n_workers == 1:
            cur = write_conn.cursor()
            try:
                rows_done = 0
                for start in range(0, total_rows, BATCH_PER_EXEC):
                    cur.executemany(insert_sql, all_rows[start: start + BATCH_PER_EXEC])
                    rows_done = min(start + BATCH_PER_EXEC, total_rows)
                    pct = int(85 + (rows_done / total_rows) * 8) if total_rows > 0 else 93
                    yield (pct, f"Saving rows… ({rows_done:,} / {total_rows:,})")
            finally:
                cur.close()
        else:
            completed_rows = 0
            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                futures = {
                    pool.submit(_write_chunk, chunk, idx): idx
                    for idx, chunk in enumerate(chunks)
                }
                for future in as_completed(futures):
                    completed_rows += future.result()  # re-raises any worker exception
                    pct = int(85 + (completed_rows / total_rows) * 8) if total_rows > 0 else 93
                    yield (pct, f"Saving rows… ({completed_rows:,} / {total_rows:,})")

        # ── Insert registry entry ─────────────────────────────────────────────
        yield (94, "Registering report metadata…")
        columns_json = json.dumps(list(result_df.columns))
        rcm_json = json.dumps(
            {rid: list(pair) for rid, pair in (rule_color_map or {}).items()}
        )
        rule_counts_json = json.dumps(rule_row_counts)
        rules_json = json.dumps([list(r) for r in rules])

        cur = write_conn.cursor()
        try:
            cur.execute(f"""
                INSERT INTO {full_registry_table} (
                    REPORT_NAME, GENERATED_AT, TOTAL_ROWS,
                    TOTAL_ROWS_BEFORE_DEDUP, DUPLICATES_REMOVED, RULES_COUNT,
                    COLUMNS_LIST, RULE_COLOR_MAP, RULE_COUNTS, RULES_JSON
                ) VALUES (%s, CURRENT_TIMESTAMP(), %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                report_name,
                len(result_df), total_before,
                total_before - len(result_df), len(rules),
                columns_json, rcm_json, rule_counts_json, rules_json,
            ))
        finally:
            cur.close()

        yield (96, "Report saved to Snowflake.")

    finally:
        write_conn.close()


def set_bdc_dependencies(
        config_manager,
        connection_manager,
        connection_factory=None,
        write_connection_factory=None,
):
    global _bdc_config_manager, _bdc_connection_manager, _connection_factory, _write_connection_factory
    _bdc_config_manager = config_manager
    _bdc_connection_manager = connection_manager
    _connection_factory = connection_factory
    # Falls back to the read factory so existing callers that don't supply
    # write creds keep working, but write operations will then run under the
    # same credentials as reads (the previous behaviour).
    _write_connection_factory = write_connection_factory or connection_factory


from excel_utils import apply_excel_type_formatting
import diff_color_rules as _dcr

consolidated_router = APIRouter(
    prefix="/api/consolidated-report",
    tags=["Consolidated Report"],
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _ts(value) -> str:
    """GENERATED_AT as ISO 8601 with its offset, whole seconds. str() gave
    "2026-05-01 10:03:09.123456-04:00", which not every browser parses."""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value) if value is not None else ""


def _rule_table_name(rule_id: str, rule_title: str) -> str:
    """Results table name for a rule. Must match _safe_table_name in the DQE
    engine (src/dqe/executor.py) exactly, or the portal looks for a table the
    engine never wrote."""
    safe_title = re.sub(r"[^A-Z0-9]", "_", (rule_title or "").strip().upper()).strip("_")
    safe_id = re.sub(r"[^A-Z0-9]", "_", (rule_id or "").strip().upper()).strip("_")
    if safe_title and safe_id and safe_title != safe_id:
        name = f"{safe_title[:200 - len(safe_id) - 1]}_{safe_id}"
    else:
        name = safe_title or safe_id or "UNKNOWN"
    name = re.sub(r"_+", "_", name)[:200]
    if name[0].isdigit():
        name = f"RULE_{name}"
    return name


# The browser pages through the preview itself; past this many rows the
# response gets slow to send and the tab slow to search. The Excel has them all.
PREVIEW_MAX_ROWS = 5000


def _preview(df) -> dict:
    head = df.head(PREVIEW_MAX_ROWS)
    return {
        "columns": list(df.columns),
        "rows": [[_safe_cell(v) for v in r] for r in head.itertuples(index=False)],
        "total": len(df),
        "truncated": len(df) > PREVIEW_MAX_ROWS,
    }


def _is_na(v) -> bool:
    """Safe NaN check that doesn't raise on non-scalar types."""
    try:
        return pd.isna(v)
    except (TypeError, ValueError):
        return False


def _safe_cell(v):
    """Return a JSON-serialisable scalar safe for both xlsxwriter and JSON."""
    if _is_na(v):
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _allowed_ids(auth: dict):
    """Rule IDs the caller may see, None = all. Late import: bdc_admin imports this module."""
    from bdc_admin import allowed_rule_ids
    return allowed_rule_ids(auth)


def cut_report(df: pd.DataFrame, rule_counts: dict, rule_color_map: dict, rules: list, ids):
    """The shared report as someone limited to some rules sees it: only rows a
    rule of theirs flagged, FAILURE_MODE trimmed to those rules, columns that
    only other rules fill dropped, counts redone. ids None = untouched."""
    if ids is None:
        return df, rule_counts, rule_color_map, rules
    if not df.empty and "FAILURE_MODE" in df.columns:
        modes = df["FAILURE_MODE"].map(
            lambda v: ", ".join(r for r in str(v or "").split(", ") if r in ids))
        df = df.assign(FAILURE_MODE=modes)[modes != ""]
        keep = [c for c in df.columns if c == "FAILURE_MODE" or df[c].notna().any()]
        df = df[keep].reset_index(drop=True)
    else:
        df = df.iloc[0:0]
    rule_counts = {k: v for k, v in (rule_counts or {}).items() if k in ids}
    rule_color_map = {k: v for k, v in (rule_color_map or {}).items() if k in ids}
    rules = [r for r in (rules or []) if r and r[0] in ids]
    return df, rule_counts, rule_color_map, rules


def _parse_color_columns(color_columns_str) -> tuple:
    if not color_columns_str:
        return (None, None)
    parts = [p.strip().upper() for p in str(color_columns_str).split(",") if p.strip()]
    return (parts[0] if len(parts) > 0 else None,
            parts[1] if len(parts) > 1 else None)


def _day_diff(v1, v2):
    """Return absolute day difference between two date-like values, or None."""
    try:
        d1 = pd.Timestamp(v1)
        d2 = pd.Timestamp(v2)
        if pd.isna(d1) or pd.isna(d2):
            return None
        return abs((d1.normalize() - d2.normalize()).days)  # calendar days, like DATEDIFF
    except Exception:
        return None


# ── Excel builder ─────────────────────────────────────────────────────────────

def _build_excel_gen(
        result_df: pd.DataFrame,
        rules: list,  # [(rule_id, rule_title, description), ...]
        rule_row_counts: dict,  # { rule_id: int }
        total_before: int,
        rule_color_map: dict = None,  # { rule_id: (col1, col2) }
):
    """Generator: yields (pct, label, None) for progress, (pct, label, bytes) when done."""
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "constant_memory": False})

    # ── Formats ────────────────────────────────────────────────────────────────
    def _fmt(**kw):
        return wb.add_format({"font_name": "Calibri", "font_size": 10, "border": 1, **kw})

    title_fmt = _fmt(bold=True, font_size=15, bg_color="#8c3bbb", font_color="#FFFFFF",
                     border=0, align="left", valign="vcenter")
    section_fmt = _fmt(bold=True, font_size=11, bg_color="#312C2A", font_color="#FFFFFF",
                       border=0, align="left", valign="vcenter")
    label_fmt = _fmt(bold=True, bg_color="#E8E6E3", font_color="#312C2A", valign="top")
    value_fmt = _fmt(bg_color="#FFFFFF", font_color="#312C2A", valign="top")
    meta_fmt = _fmt(italic=True, font_size=9, bg_color="#FAFAFA", font_color="#81766F")
    hdr_fmt = _fmt(bold=True, bg_color="#E8E6E3", font_color="#312C2A", align="center")
    cell_fmt = _fmt(bg_color="#FFFFFF", font_color="#312C2A")
    rule_cell_base = wb.add_format({"bg_color": "#FFFFFF", "border": 1, "font_name": "Calibri", "font_size": 10})
    fmt_green = _fmt(bg_color="#DCFCE7", left=2, left_color="#328714")  # ≤ 7 days
    fmt_yellow = _fmt(bg_color="#FEF9C3", left=2, left_color="#CA8A04")  # 8–15 days
    fmt_red_row = _fmt(bg_color="#FEE2E2", left=2, left_color="#EB1700")  # ≥ 16 days or null date
    hdr_color_fmt = _fmt(bold=True, bg_color="#D1FAE5", font_color="#312C2A", align="center",
                         left=2, left_color="#328714")

    # Semantic font formats for FAILURE_MODE tokens.
    # Green/red are regular weight; orange (8-15d warning) is bold so it pops.
    _rf = {"font_name": "Calibri", "font_size": 10}
    fmt_rule_green = wb.add_format({**_rf, "font_color": "#16A34A"})  # <=7 days  -- regular green
    fmt_rule_orange = wb.add_format({**_rf, "bold": True, "font_color": "#FB923C"})  # 8-15 days -- bold orange
    fmt_rule_red = wb.add_format({**_rf, "font_color": "#DC2626"})  # >=16 days / null -- regular red
    fmt_rule_black = wb.add_format({**_rf, "font_color": "#312C2A"})  # no colour columns
    sep_run_fmt = wb.add_format({"font_name": "Calibri", "font_size": 10, "font_color": "#A39992"})
    single_rule_fmt_base = wb.add_format({"bg_color": "#FFFFFF", "border": 1,
                                          "font_name": "Calibri", "font_size": 10,
                                          "font_color": "#312C2A"})
    # Cache for single-rule bordered formats keyed by (hex, bold)
    _sr_fmt_cache = {}

    def _get_sr_fmt(fc, bold):
        k = (fc, bold)
        if k not in _sr_fmt_cache:
            _sr_fmt_cache[k] = wb.add_format({
                "bg_color": "#FFFFFF", "border": 1,
                "font_name": "Calibri", "font_size": 10,
                "bold": bold, "font_color": fc,
            })
        return _sr_fmt_cache[k]

    # Helper defined ONCE here (outside row loops) — takes row_vals explicitly
    # to avoid closure-over-mutable-variable bugs inside the chunk loop.
    def _rule_font_fmt(rid, row_vals):
        pair = rcm.get(rid)
        if not pair or not pair[0] or not pair[1]:
            return fmt_rule_black
        r_ci1 = col_index.get(pair[0], -1)
        r_ci2 = col_index.get(pair[1], -1)
        if r_ci1 == -1 or r_ci2 == -1:
            return fmt_rule_black
        days = _day_diff(row_vals[r_ci1], row_vals[r_ci2])
        if days is None:  return fmt_rule_red
        if days <= 7:     return fmt_rule_green
        if days <= 15:    return fmt_rule_orange
        return fmt_rule_red

    now_str = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    total_after = len(result_df)
    dupes = total_before - total_after

    # ── Summary sheet ──────────────────────────────────────────────────────────
    ws_sum = wb.add_worksheet("Summary")
    ws_sum.set_column(0, 0, 38)  # left table: label
    ws_sum.set_column(1, 1, 52)  # left table: value
    ws_sum.set_column(2, 2, 6)  # spacer between left and right tables
    ws_sum.set_column(3, 3, 22)  # right table: Rule ID
    ws_sum.set_column(4, 4, 38)  # right table: Title
    ws_sum.set_column(5, 5, 60)  # right table: Description

    ws_sum.set_row(0, 32)
    ws_sum.merge_range(0, 0, 0, 1, "  Consolidated Failed Rows Report", title_fmt)
    # Right-side title spanning Rule Details columns
    ws_sum.merge_range(0, 3, 0, 5, "  Rule Details", title_fmt)
    ws_sum.set_row(1, 4)

    ws_sum.set_row(2, 20)
    ws_sum.merge_range(2, 0, 2, 1, "  Report Overview", section_fmt)
    ws_sum.merge_range(2, 3, 2, 5, "  Rule ID · Title · Description", section_fmt)

    overview_rows = [
        ("Generated At", now_str),
        ("Rules Included", str(len(rules))),
        ("Total Rows (before dedup)", f"{total_before:,}"),
        ("Unique Failed Rows", f"{total_after:,}"),
        ("Duplicate Rows Removed", f"{dupes:,}"),
        ("Columns in Report", str(len(result_df.columns) - 1)),  # excl FAILURE_MODE
    ]
    for i, (lbl, val) in enumerate(overview_rows):
        r = i + 3
        ws_sum.set_row(r, 18)
        ws_sum.write(r, 0, lbl, label_fmt)
        ws_sum.write(r, 1, val, value_fmt)

    # Rule Details header (row 3, right side)
    ws_sum.set_row(3, 18)
    ws_sum.write(3, 3, "Rule ID", label_fmt)
    ws_sum.write(3, 4, "Title", label_fmt)
    ws_sum.write(3, 5, "Description", label_fmt)

    # Rule Details rows — one per rule, starting at row 4
    desc_fmt = wb.add_format({
        "font_name": "Calibri", "font_size": 10, "border": 1,
        "bg_color": "#FFFFFF", "font_color": "#312C2A",
        "valign": "top", "text_wrap": True,
    })
    for i, rule_entry in enumerate(rules):
        rule_id = rule_entry[0]
        rule_title = rule_entry[1]
        rule_desc = rule_entry[2] if len(rule_entry) > 2 else ""
        r = 4 + i
        # Auto-size row height: description column is 60 chars wide, each line ~15px tall,
        # plus padding. Cap at 409 (Excel's max row height).
        chars_per_line = 60
        desc_lines = max(1, -(-len(rule_desc) // chars_per_line))  # ceiling division
        row_h = min(409, max(32, desc_lines * 15 + 8))
        ws_sum.set_row(r, row_h)
        ws_sum.write(r, 3, rule_id, value_fmt)
        ws_sum.write(r, 4, rule_title, value_fmt)
        ws_sum.write(r, 5, rule_desc, desc_fmt)

    sep = len(overview_rows) + 4
    ws_sum.set_row(sep, 4)
    ws_sum.set_row(sep + 1, 20)
    ws_sum.merge_range(sep + 1, 0, sep + 1, 1, "  Per-Rule Row Counts", section_fmt)
    ws_sum.set_row(sep + 2, 16)
    ws_sum.write(sep + 2, 0, "Rule ID", label_fmt)
    ws_sum.write(sep + 2, 1, "Rows Fetched", label_fmt)
    for i, rule_entry in enumerate(rules):
        r = sep + 3 + i
        ws_sum.set_row(r, 16)
        ws_sum.write(r, 0, rule_entry[0], value_fmt)
        ws_sum.write(r, 1, f"{rule_row_counts.get(rule_entry[0], 0):,}", value_fmt)

    footer_r = sep + 3 + len(rules) + 1
    ws_sum.merge_range(footer_r, 0, footer_r, 1,
                       f"  Generated by GRA-SmartHub  ·  {now_str}", meta_fmt)

    # ── Color Legend sheet ────────────────────────────────────────────────────
    ws_leg = wb.add_worksheet("Color Legend")
    _LEG_SWATCH_W, _LEG_LABEL_W, _LEG_DESC_W = 6, 32, 72
    ws_leg.set_column(0, 0, _LEG_SWATCH_W)   # color swatch
    ws_leg.set_column(1, 1, _LEG_LABEL_W)    # label
    ws_leg.set_column(2, 2, _LEG_DESC_W)     # explanation

    leg_title_fmt = _fmt(bold=True, font_size=15, bg_color="#8c3bbb", font_color="#FFFFFF",
                         border=0, align="left", valign="vcenter")
    leg_section_fmt = _fmt(bold=True, font_size=11, bg_color="#312C2A", font_color="#FFFFFF",
                           border=0, align="left", valign="vcenter")
    leg_label_fmt = _fmt(bold=True, bg_color="#F5F4F2", font_color="#312C2A",
                         valign="vcenter", text_wrap=True)
    leg_desc_fmt = _fmt(bg_color="#FFFFFF", font_color="#312C2A",
                        valign="vcenter", text_wrap=True)
    leg_note_fmt = _fmt(italic=True, font_size=9, bg_color="#FAFAFA",
                        font_color="#81766F", border=0)

    _LEG_LINE_H, _LEG_PAD, _LEG_MIN_H = 13, 9, 22

    def _wrapped_lines(text, col_width):
        if text is None or str(text) == "":
            return 1
        usable = max(8, col_width - 2)
        n = 0
        for para in str(text).split("\n"):
            n += len(textwrap.wrap(para, usable)) or 1
        return max(1, n)

    def _leg_height(label, description):
        lines = max(_wrapped_lines(label, _LEG_LABEL_W),
                    _wrapped_lines(description, _LEG_DESC_W))
        return max(_LEG_MIN_H, lines * _LEG_LINE_H + _LEG_PAD)

    def _swatch(bg, border_color=None):
        """Solid-color swatch cell format."""
        kw = {"bg_color": bg, "border": 1}
        if border_color:
            kw.update({"left": 3, "left_color": border_color})
        return _fmt(**kw)

    # Running row counter — every writer below advances it, so inserting or
    # removing an entry never means renumbering the rest of the sheet.
    leg_r = 0

    def _leg_row(swatch_fmt, label, description, label_fmt=None):
        """Swatch + label + description, height fitted to the text."""
        nonlocal leg_r
        ws_leg.set_row(leg_r, _leg_height(label, description))
        ws_leg.write(leg_r, 0, "", swatch_fmt)
        ws_leg.write(leg_r, 1, label, label_fmt or leg_label_fmt)
        ws_leg.write(leg_r, 2, description, leg_desc_fmt)
        leg_r += 1

    def _leg_heading(text):
        nonlocal leg_r
        ws_leg.set_row(leg_r, 20)
        ws_leg.merge_range(leg_r, 0, leg_r, 2, f"  {text}", leg_section_fmt)
        leg_r += 1

    def _leg_spacer(h=4):
        nonlocal leg_r
        ws_leg.set_row(leg_r, h)
        leg_r += 1

    ws_leg.set_row(leg_r, 32)
    ws_leg.merge_range(leg_r, 0, leg_r, 2, "  Color Legend — Consolidated Failed Rows", leg_title_fmt)
    leg_r += 1
    _leg_spacer()

    # ── Section 1: Cell background colors ─────────────────────────────────────
    _leg_heading("Cell Background Color (date-based timeliness)")

    _leg_row(_swatch("#DCFCE7", "#328714"),
             "Green  (≤ 7 days)",
             "The difference between the two comparison date columns is 7 days or fewer. "
             "The row is considered timely / on-track.")

    _leg_row(_swatch("#FEF9C3", "#CA8A04"),
             "Yellow  (8 – 15 days)",
             "The date difference is between 8 and 15 days inclusive. "
             "The row is approaching the threshold and warrants attention.")

    _leg_row(_swatch("#FEE2E2", "#EB1700"),
             "Red  (≥ 16 days or missing date)",
             "The date difference is 16 days or more, or one/both comparison date values "
             "are null/missing. The row is overdue or lacks date information.")

    _leg_row(_swatch("#FFFFFF"),
             "White  (not coloured)",
             "The rule that flagged this row has no color columns configured, and no "
             "standing column rule applies to this cell, so no timeliness coloring is applied.")

    _leg_spacer()

    # ── Section 2: Multi-rule gradient rows ───────────────────────────────────
    _leg_heading("Multi-Rule Rows — Gradient Cell Fill")

    _LEGEND_SWATCH_CELL = (leg_r + 1, 1)
    _LEGEND_SWATCH_STOPS = ['#FEE2E2', '#FEF9C3', '#DCFCE7']  # worst-first, matches data cells
    _leg_row(_swatch("#FEE2E2"),
             "Gradient fill",
             "When a single row failed multiple rules and those rules produce different "
             "timeliness colors, the relevant date cells display a gradient blending each "
             "rule's color. The gradient runs worst-severity first (red → yellow → green). "
             "The cell falls back to the single worst color in viewers that cannot display "
             "gradient fills.")

    _leg_spacer()

    # ── Section 3: FAILURE_MODE column text colors ────────────────────────────
    _leg_heading("FAILURE_MODE Column — Text Color")

    leg_rule_green_fmt = wb.add_format({"font_name": "Calibri", "font_size": 10,
                                        "font_color": "#16A34A", "bg_color": "#FFFFFF",
                                        "border": 1, "valign": "vcenter"})
    leg_rule_orange_fmt = wb.add_format({"font_name": "Calibri", "font_size": 10, "bold": True,
                                         "font_color": "#FB923C", "bg_color": "#FFFFFF",
                                         "border": 1, "valign": "vcenter"})
    leg_rule_red_fmt = wb.add_format({"font_name": "Calibri", "font_size": 10,
                                      "font_color": "#DC2626", "bg_color": "#FFFFFF",
                                      "border": 1, "valign": "vcenter"})
    leg_rule_black_fmt = wb.add_format({"font_name": "Calibri", "font_size": 10,
                                        "font_color": "#312C2A", "bg_color": "#FFFFFF",
                                        "border": 1, "valign": "vcenter"})

    _leg_row(_swatch("#FFFFFF"), "RULE_ID",
             "Green text — the rule's comparison dates are within 7 days (timely).",
             label_fmt=leg_rule_green_fmt)
    _leg_row(_swatch("#FFFFFF"), "RULE_ID",
             "Bold orange text — the rule's comparison dates are 8–15 days apart (warning).",
             label_fmt=leg_rule_orange_fmt)
    _leg_row(_swatch("#FFFFFF"), "RULE_ID",
             "Red text — the rule's comparison dates are ≥ 16 days apart, or a date is "
             "missing (overdue / no data).",
             label_fmt=leg_rule_red_fmt)
    _leg_row(_swatch("#FFFFFF"), "RULE_ID",
             "Black text — the rule has no color columns configured; no timeliness score "
             "is computed.",
             label_fmt=leg_rule_black_fmt)

    _leg_spacer()

    # ── Section 4: Column header highlight ────────────────────────────────────
    _leg_heading("Column Header Highlight")

    _leg_row(_fmt(bold=True, bg_color="#D1FAE5", font_color="#312C2A", align="center",
                  left=2, left_color="#328714"),
             "Green-tinted header",
             "This column is coloured — either it is a date comparison column for at least "
             "one rule, or it is painted by one of the standing column rules listed below.")

    _leg_row(_fmt(bold=True, bg_color="#E8E6E3", font_color="#312C2A", align="center"),
             "Gray header",
             "Standard data column — never coloured by a rule's color columns or by a "
             "standing column rule.")

    _leg_spacer()

    # ── Section 5: How color columns work ─────────────────────────────────────
    _leg_heading("How Color Columns Work")

    _leg_row(_swatch("#FAFAFA"),
             "Color columns",
             "Each rule can optionally declare two date columns (e.g. EVENT_DATE and "
             "PROCESS_DATE). The absolute difference in days between those two columns "
             "for a given row determines the timeliness category: ≤ 7 days = green, "
             "8–15 days = yellow, ≥ 16 days or null = red. Rules without declared "
             "color columns produce no background or text coloring.")

    _leg_spacer()

    # ── Section 6: Standing DIFFERENCE_* column rules ─────────────────────────
    # Only documented when the driving columns are actually in this report.
    _dcr_legend = _dcr.active_rules(_dcr.build_col_index(result_df.columns))
    if _dcr_legend:
        _leg_heading("Standing Column Rules (DIFFERENCE_* columns)")

        _leg_row(_swatch("#FAFAFA"),
                 "How these differ",
                 "These rules apply to every row in the report regardless of which rule "
                 "flagged it, and read a pre-computed DIFFERENCE_* column rather than "
                 "comparing two dates. They colour the same cells using the same green / "
                 "yellow / red scale, and a cell coloured by both a rule's color columns "
                 "and a standing rule shows the usual blended fill.")

        for _spec, _ in _dcr_legend:
            _leg_row(_swatch("#DCFCE7", "#328714"),
                     " · ".join(_spec["targets"]),
                     _dcr.describe(_spec))

        _leg_spacer()

    ws_leg.set_row(leg_r, 14)
    ws_leg.merge_range(leg_r, 0, leg_r, 2,
                       f"  Generated by GRA-SmartHub  ·  {now_str}", leg_note_fmt)
    # ── All Failed Rows sheet ──────────────────────────────────────────────────
    ws = wb.add_worksheet("All Failed Rows")
    final_cols = list(result_df.columns)
    failed_rule_idx = final_cols.index("FAILURE_MODE")

    # Build a lookup: stripped column name → index
    stripped_cols = [c.strip() for c in final_cols]
    col_index = {c: i for i, c in enumerate(stripped_cols)}
    rcm = rule_color_map or {}

    # Highlight every column that appears as a color column in any rule
    color_col_indices = set()
    for col1, col2 in rcm.values():
        if col1 and col1 in col_index: color_col_indices.add(col_index[col1])
        if col2 and col2 in col_index: color_col_indices.add(col_index[col2])

    # Hardcoded DIFFERENCE_* rules — layered on top of COLOR_COLUMNS
    dcr_col_index = _dcr.build_col_index(final_cols)
    dcr_rules = _dcr.active_rules(dcr_col_index)
    color_col_indices |= _dcr.target_col_indices(dcr_col_index)
    _SEV_HEX = {_dcr.GREEN: '#DCFCE7', _dcr.YELLOW: '#FEF9C3', _dcr.RED: '#FEE2E2'}

    # Set column widths: FAILURE_MODE needs to be wide enough for multiple rule tokens
    # (e.g. "1A_Timeliness_v1 · 1B_Accuracy_v1 · 2A_Timeliness_v1 · 2B_Accuracy_v1 · 3A_Timeliness_v1"
    # is ~90 chars). All other columns get a sensible default.
    for c in range(len(final_cols)):
        if c == failed_rule_idx:
            ws.set_column(c, c, 100)  # wide enough for 5+ rule tokens on one line
        else:
            ws.set_column(c, c, 22)  # comfortable default for data columns

    for c, col in enumerate(final_cols):
        is_color_col = c in color_col_indices
        ws.write(0, c, col, hdr_color_fmt if is_color_col else hdr_fmt)

    gradient_cell_map = {}  # (xl_row, xl_col) -> [sorted bg_hex list, worst-first]
    total_rows = len(result_df)
    last_pct = 40
    _BG_PRI = {'#DCFCE7': 0, '#FEF9C3': 1, '#FEE2E2': 2}  # severity order
    CHUNK = 2_000
    for chunk_start in range(0, total_rows, CHUNK):
        chunk = result_df.iloc[chunk_start: chunk_start + CHUNK]
        for ri, row_vals in enumerate(chunk.itertuples(index=False), start=chunk_start + 1):
            vals = list(row_vals)
            fr_val = vals[failed_rule_idx]

            # Build per-cell background colors from each rule's color column pair
            rule_ids = [r.strip() for r in str(fr_val).split(',')] if fr_val else []
            cell_bg_accum = {}  # ci -> [bg_hex, ...]
            for _rid in rule_ids:
                _p = rcm.get(_rid)
                if _p and _p[0] and _p[1]:
                    _ci1 = col_index.get(_p[0], -1)
                    _ci2 = col_index.get(_p[1], -1)
                    if _ci1 != -1 and _ci2 != -1:
                        _days = _day_diff(vals[_ci1], vals[_ci2])
                        _bg = '#DCFCE7' if (_days is not None and _days <= 7) else '#FEF9C3' if (
                                    _days is not None and _days <= 15) else '#FEE2E2'
                        cell_bg_accum.setdefault(_ci1, []).append(_bg)
                        cell_bg_accum.setdefault(_ci2, []).append(_bg)

            # Diff-rule verdicts land in the same accumulator, so a cell hit by
            # both paths merges exactly like any other multi-rule cell.
            if dcr_rules:
                _sev_accum = _dcr.accumulate(vals, dcr_col_index, {}, dcr_rules)
                for _ci, _sevs in _sev_accum.items():
                    cell_bg_accum.setdefault(_ci, []).extend(_SEV_HEX[s] for s in _sevs)

            # xlsxwriter uses worst-severity solid color; gradient applied by openpyxl post-step
            cell_fmts = {}
            for _ci, _bgs in cell_bg_accum.items():
                unique_bgs = list(dict.fromkeys(_bgs))
                worst = max(unique_bgs, key=lambda b: _BG_PRI[b])
                cell_fmts[_ci] = fmt_green if worst == '#DCFCE7' else fmt_yellow if worst == '#FEF9C3' else fmt_red_row
                if len(unique_bgs) > 1:
                    sorted_bgs = sorted(unique_bgs, key=lambda b: -_BG_PRI[b])  # worst first
                    gradient_cell_map[(ri + 1, _ci + 1)] = sorted_bgs  # +1: openpyxl is 1-indexed

            for ci, val in enumerate(vals):
                if ci == failed_rule_idx:
                    fr_text = "" if _is_na(val) else _safe_cell(val)
                    if not fr_text:
                        ws.write(ri, ci, "", rule_cell_base)
                    else:
                        rule_parts = [r.strip() for r in fr_text.split(',') if r.strip()]
                        if len(rule_parts) <= 1:
                            single_rid = rule_parts[0] if rule_parts else fr_text
                            font_fmt = _rule_font_fmt(single_rid, vals)
                            if font_fmt is fmt_rule_green:
                                fc, bold = '#16A34A', False
                            elif font_fmt is fmt_rule_orange:
                                fc, bold = '#FB923C', True
                            elif font_fmt is fmt_rule_red:
                                fc, bold = '#DC2626', False
                            else:
                                fc, bold = '#312C2A', False
                            ws.write(ri, ci, single_rid, _get_sr_fmt(fc, bold))
                        else:
                            rich_args = []
                            for idx, rule in enumerate(rule_parts):
                                if idx > 0:
                                    rich_args.extend([sep_run_fmt, ' · '])
                                rich_args.extend([_rule_font_fmt(rule, vals), rule])
                            ws.write_rich_string(ri, ci, *rich_args, rule_cell_base)
                else:
                    bg_fmt = cell_fmts.get(ci)
                    ws.write(ri, ci, "" if _is_na(val) else _safe_cell(val),
                             bg_fmt if bg_fmt is not None else cell_fmt)

        rows_done = min(chunk_start + CHUNK, total_rows)
        build_pct = int(40 + (rows_done / total_rows) * 50) if total_rows > 0 else 90
        if build_pct > last_pct:
            last_pct = build_pct
            yield (build_pct, f"Building data... ({rows_done:,} / {total_rows:,} rows)", None)

    wb.close()
    buf.seek(0)

    # ── Post-process: date/numeric formatting, then gradient fills ──────────
    # Step 1 — convert date strings to Excel serial numbers (DD-MMM-YYYY) and
    #           numeric strings to proper int/float values.
    buf.seek(0)
    _processed_bytes = apply_excel_type_formatting(buf)

    # Step 2 — apply per-cell gradient fills for multi-rule rows, plus the
    # legend's gradient swatch. FAILURE_MODE rich-text colouring is already in
    # the xlsxwriter output.
    _sheet_gradients = {
        'All Failed Rows': gradient_cell_map,
        'Color Legend': {_LEGEND_SWATCH_CELL: _LEGEND_SWATCH_STOPS},
    }
    if any(_sheet_gradients.values()):
        try:
            import openpyxl as _oxl
            from openpyxl.styles import GradientFill as _GF
            from openpyxl.styles.fills import Stop as _Stop

            yield (93, "Attempting to apply colors...", None)
            # rich_text=True preserves rich strings already written by xlsxwriter.
            _wb2 = _oxl.load_workbook(io.BytesIO(_processed_bytes), rich_text=True)

            def _paint(_cell, bgs):
                n = len(bgs)
                _EPS = 0.001
                stops = []
                for i, bg in enumerate(bgs):
                    argb = 'FF' + bg.lstrip('#').upper()
                    start = round(i / n + (_EPS if i > 0 else 0), 4)
                    end = round((i + 1) / n - (_EPS if i < n - 1 else 0), 4)
                    stops.append(_Stop(color=argb, position=start))
                    stops.append(_Stop(color=argb, position=end))
                _cell.fill = _GF(type='linear', degree=0, stop=stops)

            # Legend swatch first — cheap, and it must be painted even when no
            # data row happens to need a gradient.
            _ws_leg2 = _wb2['Color Legend']
            for (_lr, _lc), _lbgs in _sheet_gradients['Color Legend'].items():
                _paint(_ws_leg2.cell(row=_lr, column=_lc), _lbgs)

            _ws2 = _wb2['All Failed Rows']
            _gf_total = len(gradient_cell_map)
            _gf_items = list(gradient_cell_map.items())
            _GF_CHUNK = 500
            for _gf_start in range(0, _gf_total, _GF_CHUNK):
                _gf_end = min(_gf_start + _GF_CHUNK, _gf_total)
                _gf_pct = int(93 + (_gf_end / _gf_total) * 5) if _gf_total > 0 else 98
                yield (_gf_pct, f"Applying colors now... ({_gf_end:,} / {_gf_total:,} cells)", None)
                for (xl_row, xl_col), bgs in _gf_items[_gf_start:_gf_end]:
                    _paint(_ws2.cell(row=xl_row, column=xl_col), bgs)

            yield (98, "Saving final table... (0s)", None)
            final_buf = io.BytesIO()
            _save_done = threading.Event()
            _save_error = [None]

            def _do_save():
                try:
                    _wb2.save(final_buf)
                except Exception as _se:
                    _save_error[0] = _se
                finally:
                    _save_done.set()

            threading.Thread(target=_do_save, daemon=True).start()

            _elapsed = 0
            while not _save_done.wait(timeout=0.8):
                _elapsed += 1
                yield (98, f"Saving final table... ({_elapsed}s)", None)

            if _save_error[0]:
                raise _save_error[0]

            final_buf.seek(0)
            yield (100, "Done!", final_buf.read())
            return
        except Exception as _e:
            logger.warning(f'Color post-processing failed, falling back: {_e}')

    yield (100, "Done!", _processed_bytes)


# ── SSE endpoint ──────────────────────────────────────────────────────────────

@consolidated_router.get("/prepare")
async def prepare_consolidated_report(
    delta_mode: bool = Query(False, description="When True, generate a bi-weekly delta consolidated report"),
    auth: dict = Depends(require_permission("runBDCOperations")),
):
    # A new report becomes everyone's latest, so it has to cover every dataset
    if bdc_datasets(auth) is not None:
        raise HTTPException(status_code=403, detail="Generating a report covers every dataset; your access is limited to some datasets")

    async def stream():
        try:
            # Report name prefix differs by mode
            now_ts = datetime.now(EASTERN)
            report_prefix = "CONSOLIDATED_DELTA_REPORT_" if delta_mode else "CONSOLIDATED_REPORT_"
            report_name = report_prefix + now_ts.strftime('%Y%m%d_%H%M%S')
            registry_table = DELTA_REGISTRY_TABLE if delta_mode else REGISTRY_TABLE

            # ── Step 1: connect + fetch rule list ─────────────────────────────
            yield _sse({"step": 1, "total": 6, "pct": 2, "label": "Connecting to Snowflake…"})
            await asyncio.sleep(0)

            if not _bdc_connection_manager or not _bdc_config_manager:
                yield _sse({"type": "error", "message": "Business Defined Checks service not available."})
                return

            sf = _bdc_connection_manager.get_client()
            dq_config = _bdc_config_manager.get_dq_config()
            sf_config = _bdc_config_manager.get_snowflake_config()
            database = sf_config.get("database")
            schema = sf_config.get("schema")
            latest_key = dq_config["rule_latest_view"]
            if delta_mode:
                latest_key = latest_key + '_DELTA' if not latest_key.endswith('_DELTA') else latest_key
            rule_view = _bdc_config_manager.get_full_table_name(latest_key)
            rule_input_table = _bdc_config_manager.get_full_table_name(
                dq_config["rule_input_table"]
            )

            rules_result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: sf.execute(f"""
                    SELECT v.RULE_ID, v.RULE_TITLE, i.COLOR_COLUMNS, v.RULE_DESCRIPTION
                    FROM {rule_view} v
                    LEFT JOIN {rule_input_table} i
                        ON v.RULE_ID = i.RULE_ID AND i.IS_ACTIVE = TRUE
                    ORDER BY v.RULE_ID
                """),
            )
            # rules: [(rule_id, rule_title, description), ...]
            # rule_color_map: { rule_id: (col1, col2) }
            rules = [(row[0], row[1], row[3] or "") for row in rules_result.rows]
            rule_color_map = {
                row[0]: _parse_color_columns(row[2])
                for row in rules_result.rows
            }

            if not rules:
                yield _sse({"type": "error", "message": "No active rules found."})
                return

            yield _sse({
                "step": 1, "total": 6, "pct": 5,
                "label": f"Found {len(rules)} rules. Fetching failed rows from Snowflake…",
            })
            await asyncio.sleep(0)

            # ── Step 2: fetch each rule's results table ───────────────────────
            rule_dfs = {}
            rule_row_counts = {}

            for i, (rule_id, rule_title, *_) in enumerate(rules):
                pct = 5 + int((i / len(rules)) * 55)
                yield _sse({
                    "step": 2, "total": 6, "pct": pct,
                    "label": f"Fetching {rule_id} ({i + 1} / {len(rules)})…",
                })
                await asyncio.sleep(0)

                table_name = _rule_table_name(rule_id, rule_title)
                if delta_mode:
                    table_name = f"DELTA_{table_name}"
                fq_table = _bdc_config_manager.core_table(table_name)

                try:
                    cur = sf.conn.cursor()
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, lambda t=fq_table: cur.execute(f"SELECT * FROM {t}")
                        )
                        batches = await asyncio.get_event_loop().run_in_executor(
                            None, lambda: list(cur.fetch_pandas_batches())
                        )
                    finally:
                        cur.close()  # also when the table is missing; it leaked before
                    df = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame()
                except Exception as e:
                    logger.warning(f"Skipping {rule_id} — could not read {fq_table}: {e}")
                    df = pd.DataFrame()

                rule_dfs[rule_id] = df
                rule_row_counts[rule_id] = len(df)

            # ── Step 3: union + dedup + FAILURE_MODE ───────────────────────────
            yield _sse({"step": 3, "total": 6, "pct": 62, "label": "Merging and deduplicating rows…"})
            await asyncio.sleep(0)

            tagged = []
            for rule_id, df in rule_dfs.items():
                if df.empty:
                    continue
                temp = df.copy()
                temp["FAILURE_MODE"] = rule_id
                tagged.append(temp)

            if not tagged:
                yield _sse({"type": "error", "message": "No failed rows found across all rules."})
                return

            combined = await asyncio.get_event_loop().run_in_executor(
                None, lambda: pd.concat(tagged, ignore_index=True, sort=False)
            )

            data_cols = [c for c in combined.columns if c != "FAILURE_MODE"]
            total_before = len(combined)

            yield _sse({"step": 3, "total": 6, "pct": 63,
                        "label": f"Unioned {total_before:,} rows — hashing for deduplication…"})
            await asyncio.sleep(0)

            # Step 3a: hash all data columns
            str_df = await asyncio.get_event_loop().run_in_executor(
                None, lambda: combined[data_cols].astype(str)
            )
            combined = combined.copy()
            combined["__hash__"] = await asyncio.get_event_loop().run_in_executor(
                None, lambda: pd.util.hash_pandas_object(str_df, index=False)
            )

            yield _sse({"step": 3, "total": 6, "pct": 67, "label": f"Hashed {total_before:,} rows — aggregating…"})
            await asyncio.sleep(0)

            # Step 3b: aggregate FAILURE_MODE per hash group
            rule_agg = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: combined.groupby("__hash__", sort=False)["FAILURE_MODE"]
                .agg(lambda x: ", ".join(sorted(set(x.astype(str)))))
            )

            yield _sse({"step": 3, "total": 6, "pct": 71, "label": "Rules aggregated — dropping duplicate rows…"})
            await asyncio.sleep(0)

            # Step 3c: keep first row per hash then merge aggregated rules back
            first_rows = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: combined.drop_duplicates(subset="__hash__", keep="first")[["__hash__"] + data_cols]
            )

            yield _sse({"step": 3, "total": 6, "pct": 74, "label": "Duplicates dropped — finalising merged dataset…"})
            await asyncio.sleep(0)

            result_df = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: first_rows.merge(rule_agg, on="__hash__", how="left")
                .drop(columns="__hash__").reset_index(drop=True)
            )
            total_after = len(result_df)
            duplicates_removed = total_before - total_after

            yield _sse({
                "step": 3, "total": 4, "pct": 76,
                "label": f"Merged {total_before:,} rows → {total_after:,} unique rows…",
            })
            await asyncio.sleep(0)

            # ── Step 4: Write report to Snowflake (streamed) ─────────────────
            yield _sse({"step": 4, "total": 4, "pct": 82, "label": f"Saving {total_after:,} rows to Snowflake…"})
            await asyncio.sleep(0)

            import queue as _queue
            _sf_sentinel = object()
            _sf_q = _queue.Queue()
            _sf_error = [None]

            def _sf_worker():
                try:
                    for pct, label in _write_report_to_snowflake_gen(
                            sf, database, schema, report_name, result_df, total_before, rules, rule_row_counts,
                            rule_color_map, registry_table,
                    ):
                        _sf_q.put((pct, label))
                except Exception as _ex:
                    _sf_error[0] = _ex
                finally:
                    _sf_q.put(_sf_sentinel)

            _sf_t = threading.Thread(target=_sf_worker, daemon=True)
            _sf_t.start()

            sf_failed = False
            while True:
                item = await asyncio.get_event_loop().run_in_executor(None, _sf_q.get)
                if item is _sf_sentinel:
                    break
                pct, label = item
                yield _sse({"step": 4, "total": 4, "pct": pct, "label": label})
                await asyncio.sleep(0)

            _sf_t.join()
            if _sf_error[0]:
                logger.warning(f"Snowflake write failed for {report_name}: {_sf_error[0]}")
                yield _sse(
                    {"step": 4, "total": 4, "pct": 96, "label": "Snowflake save failed (non-critical) — continuing…"})
                sf_failed = True
            await asyncio.sleep(0)

            # ── Finalise: build preview + emit ready ─────────────────────────
            yield _sse({"step": 4, "total": 4, "pct": 97, "label": "Finalizing…"})
            await asyncio.sleep(0)


            yield _sse({
                "type": "ready",
                "pct": 100,
                "report_name": report_name,
                # False when the Snowflake save failed: the rows below are all
                # there is, so history and the Excel download can't find it.
                "persisted": not sf_failed,
                "summary": {
                    "total_rows": total_after,
                    "total_before_dedup": total_before,
                    "duplicates_removed": duplicates_removed,
                    "rules_count": len(rules),
                    "columns_count": len(data_cols),
                    "rule_counts": rule_row_counts,
                    "rule_color_map": {rid: list(pair) for rid, pair in
                                       rule_color_map.items()} if rule_color_map else {},
                },
                "preview": _preview(result_df),
            })

        except Exception as e:
            logger.error(f"Consolidated report error: {e}", exc_info=True)
            yield _sse({"type": "error", "message": f"Unexpected error: {str(e)}"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── Download endpoint ─────────────────────────────────────────────────────────

@consolidated_router.get("/download")
def fetch_consolidated_download(token: str = Query(...), auth: dict = Depends(require_permission("downloadBDCData"))):
    file_bytes = _cache_get(token)
    if file_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="Report not found or token expired. Please regenerate.",
        )
    return StreamingResponse(
        io.BytesIO(file_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="consolidated_failed_rows.xlsx"',
            "Content-Length": str(len(file_bytes)),
        },
    )


# ── /latest — load the most recent persisted report ──────────────────────────

@consolidated_router.get("/latest")
async def get_latest_consolidated_report(
    delta_mode: bool = Query(False, description="When True, fetch from the delta registry"),
    auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    """Return the most recently generated consolidated report."""
    if not _bdc_connection_manager or not _bdc_config_manager:
        raise HTTPException(status_code=503, detail="BDC service not available.")

    sf = _bdc_connection_manager.get_client()
    sf_config = _bdc_config_manager.get_snowflake_config()
    database = sf_config.get("database")
    schema = sf_config.get("schema")
    registry_table = DELTA_REGISTRY_TABLE if delta_mode else REGISTRY_TABLE
    full_reg = f"{database}.{schema}.{registry_table}"

    cur = sf.conn.cursor()
    try:
        cur.execute(f"""
            SELECT REPORT_NAME, GENERATED_AT, TOTAL_ROWS, TOTAL_ROWS_BEFORE_DEDUP,
                   DUPLICATES_REMOVED, RULES_COUNT, RULE_COLOR_MAP, RULE_COUNTS
            FROM {full_reg}
            ORDER BY GENERATED_AT DESC
            LIMIT 1
        """)
        row = cur.fetchone()
    except Exception:
        return {"found": False}
    finally:
        cur.close()

    if not row:
        return {"found": False}

    report_name = row[0]
    generated_at = _ts(row[1])
    total_rows = row[2]
    total_before = row[3]
    duplicates_removed = row[4]
    rules_count = row[5]
    rule_color_map = json.loads(row[6])
    rule_counts = json.loads(row[7])

    full_report = f"{database}.{schema}.{report_name}"
    cur = sf.conn.cursor()
    try:
        await asyncio.get_event_loop().run_in_executor(None, lambda: cur.execute(f"SELECT * FROM {full_report}"))
        batches = await asyncio.get_event_loop().run_in_executor(None, lambda: list(cur.fetch_pandas_batches()))
    finally:
        cur.close()

    df = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame()
    ids = await asyncio.to_thread(_allowed_ids, auth)
    df, rule_counts, rule_color_map, _ = cut_report(df, rule_counts, rule_color_map, [], ids)
    if ids is not None:
        total_rows, rules_count = len(df), len(rule_counts)
        total_before = sum(int(v or 0) for v in rule_counts.values())
        duplicates_removed = max(0, total_before - total_rows)

    return {
        "found": True,
        "report_name": report_name,
        "summary": {
            "total_rows": total_rows,
            "total_before_dedup": total_before,
            "duplicates_removed": duplicates_removed,
            "rules_count": rules_count,
            "rule_color_map": rule_color_map,
            "rule_counts": rule_counts,
            "generated_at": generated_at,
        },
        "preview": _preview(df),
    }


# ── /history — list all persisted reports ─────────────────────────────────────

@consolidated_router.get("/history")
async def get_consolidated_report_history(
    delta_mode: bool = Query(False, description="When True, fetch from the delta registry"),
    auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    """Return all consolidated report registry entries, newest first."""
    if not _bdc_connection_manager or not _bdc_config_manager:
        raise HTTPException(status_code=503, detail="BDC service not available.")

    sf = _bdc_connection_manager.get_client()
    sf_config = _bdc_config_manager.get_snowflake_config()
    database = sf_config.get("database")
    schema = sf_config.get("schema")
    registry_table = DELTA_REGISTRY_TABLE if delta_mode else REGISTRY_TABLE
    full_reg = f"{database}.{schema}.{registry_table}"

    cur = sf.conn.cursor()
    try:
        cur.execute(f"""
            SELECT REPORT_NAME, GENERATED_AT, TOTAL_ROWS, TOTAL_ROWS_BEFORE_DEDUP,
                   DUPLICATES_REMOVED, RULES_COUNT, RULE_COUNTS
            FROM {full_reg}
            ORDER BY GENERATED_AT DESC
        """)
        rows = cur.fetchall()
    except Exception:
        return {"reports": []}
    finally:
        cur.close()

    ids = await asyncio.to_thread(_allowed_ids, auth)
    if ids is not None:
        # Unique rows need the report itself; only the per-rule counts are known here
        def mine(r):
            counts = {k: v for k, v in json.loads(r[6] or "{}").items() if k in ids}
            return (r[0], r[1], None, sum(int(v or 0) for v in counts.values()), None, len(counts))
        rows = [mine(r) for r in rows]

    return {
        "reports": [
            {
                "report_name": r[0],
                "generated_at": _ts(r[1]),
                "total_rows": r[2],
                "total_before_dedup": r[3],
                "duplicates_removed": r[4],
                "rules_count": r[5],
            }
            for r in rows
        ]
    }


# ── /fetch — load a specific persisted report by name ────────────────────────

@consolidated_router.get("/fetch")
async def fetch_consolidated_report(
        report_name: str = Query(..., description="Exact report table name"),
        delta_mode: bool = Query(False, description="When True, look up in the delta registry"),
        auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    """Return row data and metadata for a specific historical consolidated report."""
    if not _bdc_connection_manager or not _bdc_config_manager:
        raise HTTPException(status_code=503, detail="BDC service not available.")

    sf = _bdc_connection_manager.get_client()
    sf_config = _bdc_config_manager.get_snowflake_config()
    database = sf_config.get("database")
    schema = sf_config.get("schema")
    registry_table = DELTA_REGISTRY_TABLE if delta_mode else REGISTRY_TABLE
    full_reg = f"{database}.{schema}.{registry_table}"

    cur = sf.conn.cursor()
    try:
        cur.execute(f"""
            SELECT GENERATED_AT, TOTAL_ROWS, TOTAL_ROWS_BEFORE_DEDUP,
                   DUPLICATES_REMOVED, RULES_COUNT, RULE_COLOR_MAP, RULE_COUNTS
            FROM {full_reg}
            WHERE REPORT_NAME = %s
        """, (report_name,))
        row = cur.fetchone()
    finally:
        cur.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"Report '{report_name}' not found.")

    generated_at = _ts(row[0])
    total_rows = row[1]
    total_before = row[2]
    duplicates_removed = row[3]
    rules_count = row[4]
    rule_color_map = json.loads(row[5])
    rule_counts = json.loads(row[6])

    full_report = f"{database}.{schema}.{report_name}"
    cur = sf.conn.cursor()
    try:
        await asyncio.get_event_loop().run_in_executor(None, lambda: cur.execute(f"SELECT * FROM {full_report}"))
        batches = await asyncio.get_event_loop().run_in_executor(None, lambda: list(cur.fetch_pandas_batches()))
    finally:
        cur.close()

    df = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame()
    ids = await asyncio.to_thread(_allowed_ids, auth)
    df, rule_counts, rule_color_map, _ = cut_report(df, rule_counts, rule_color_map, [], ids)
    if ids is not None:
        total_rows, rules_count = len(df), len(rule_counts)
        total_before = sum(int(v or 0) for v in rule_counts.values())
        duplicates_removed = max(0, total_before - total_rows)

    return {
        "report_name": report_name,
        "summary": {
            "total_rows": total_rows,
            "total_before_dedup": total_before,
            "duplicates_removed": duplicates_removed,
            "rules_count": rules_count,
            "rule_color_map": rule_color_map,
            "rule_counts": rule_counts,
            "generated_at": generated_at,
        },
        "preview": _preview(df),
    }


# ── /prepare-excel — rebuild Excel for any persisted report ──────────────────

@consolidated_router.get("/prepare-excel")
async def prepare_excel_for_report(
        report_name: str = Query(..., description="Exact report table name"),
        delta_mode: bool = Query(False, description="When True, look up in the delta registry"),
        auth: dict = Depends(require_permission("downloadBDCData")),
):
    """Stream Excel-build progress for a historical report and return a download token."""

    async def stream():
        try:
            yield _sse({"pct": 3, "label": "Connecting to Snowflake…"})
            await asyncio.sleep(0)

            if not _bdc_connection_manager or not _bdc_config_manager:
                yield _sse({"type": "error", "message": "BDC service not available."})
                return

            sf = _bdc_connection_manager.get_client()
            sf_config = _bdc_config_manager.get_snowflake_config()
            database = sf_config.get("database")
            schema = sf_config.get("schema")
            registry_table = DELTA_REGISTRY_TABLE if delta_mode else REGISTRY_TABLE
            full_reg = f"{database}.{schema}.{registry_table}"

            cur = sf.conn.cursor()
            try:
                cur.execute(f"""
                    SELECT TOTAL_ROWS, TOTAL_ROWS_BEFORE_DEDUP,
                           RULE_COLOR_MAP, RULE_COUNTS, RULES_JSON
                    FROM {full_reg}
                    WHERE REPORT_NAME = %s
                """, (report_name,))
                row = cur.fetchone()
            finally:
                cur.close()

            if not row:
                yield _sse({"type": "error", "message": f"Report '{report_name}' not found in registry."})
                return

            total_before = row[1]
            rule_color_map = {k: tuple(v) for k, v in json.loads(row[2]).items()}
            rule_counts = json.loads(row[3])
            rules = [tuple(r) for r in json.loads(row[4])]

            yield _sse({"pct": 8, "label": f"Loading {row[0]:,} rows from Snowflake…"})
            await asyncio.sleep(0)

            full_report = f"{database}.{schema}.{report_name}"
            cur = sf.conn.cursor()
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: cur.execute(f"SELECT * FROM {full_report}")
                )
                batches = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: list(cur.fetch_pandas_batches())
                )
            finally:
                cur.close()

            result_df = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame()
            ids = await asyncio.to_thread(_allowed_ids, auth)
            result_df, rule_counts, rule_color_map, rules = cut_report(result_df, rule_counts, rule_color_map, rules, ids)
            if ids is not None:
                total_before = sum(int(v or 0) for v in rule_counts.values())

            yield _sse({"pct": 30, "label": f"Building Excel ({len(result_df):,} rows)…"})
            await asyncio.sleep(0)

            import queue as _queue
            import threading as _threading
            file_bytes = None
            _sentinel = object()
            _q = _queue.Queue()

            def _excel_worker():
                try:
                    for item in _build_excel_gen(
                            result_df, rules, rule_counts, total_before,
                            rule_color_map,
                    ):
                        _q.put(item)
                except Exception as _ex:
                    _q.put(_ex)
                finally:
                    _q.put(_sentinel)

            _t = _threading.Thread(target=_excel_worker, daemon=True)
            _t.start()

            while True:
                item = await asyncio.get_event_loop().run_in_executor(None, _q.get)
                if item is _sentinel:
                    break
                if isinstance(item, Exception):
                    raise item
                pct, label, maybe_bytes = item
                if maybe_bytes is not None:
                    file_bytes = maybe_bytes
                    break
                yield _sse({"pct": pct, "label": label})
                await asyncio.sleep(0)

            _t.join()

            token = str(uuid.uuid4())
            _cache_put(token, file_bytes)

            yield _sse({
                "type": "ready",
                "pct": 100,
                "token": token,
                "filename": f"consolidated_failed_rows_{report_name}.xlsx",
                "size": len(file_bytes),
            })

        except Exception as e:
            logger.error(f"prepare-excel error for {report_name}: {e}", exc_info=True)
            yield _sse({"type": "error", "message": f"Failed to build Excel: {str(e)}"})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )