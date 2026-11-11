"""
Quarterly DQ Report
-------------------
  Fetches all source rows from the TARGET_TABLE source table, tags each row
  with a FAILURE_MODE (comma-separated rule IDs) by matching against the
  rule failed-row tables, and writes a colour-coded Excel report.

  Failed rows are colour-coded by timeliness; non-failed rows are plain white
  with a blank FAILURE_MODE column.

Usage:
    python Email_quarterly_report.py
    python Email_quarterly_report.py --output-dir /path/to/reports
    python Email_quarterly_report.py --config /custom/path/dq_config.json
    python Email_quarterly_report.py --skip-snowflake   # Excel only, no Snowflake writes
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pytz
import xlsxwriter

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("EMAIL_QUARTERLY_REPORT")

EASTERN = pytz.timezone("America/New_York")

def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        log.error(f"Config file not found: {config_path}")
        sys.exit(1)
    with open(config_path) as f:
        return json.load(f)


def _load_private_key(pk_path: str, pk_password: str):
    from cryptography.hazmat.primitives import serialization
    path = Path(pk_path)
    if not path.exists():
        raise FileNotFoundError(f"Private key not found: {pk_path}")
    with open(path, "rb") as f:
        key = serialization.load_pem_private_key(
            f.read(),
            password=pk_password.encode() if pk_password else None,
        )
    return key.private_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def open_snowflake_connection(sf_cfg: dict):
    """Open a raw snowflake.connector connection from a config block."""
    import snowflake.connector
    pk_bytes = _load_private_key(
        sf_cfg["private_key_path"],
        sf_cfg.get("private_key_password", ""),
    )
    return snowflake.connector.connect(
        user=sf_cfg["user"],
        account=sf_cfg["account"],
        private_key=pk_bytes,
        role=sf_cfg["role"],
        warehouse=sf_cfg["warehouse"],
        database=sf_cfg["database"],
        schema=sf_cfg.get("schema", "PUBLIC"),
    )


def sf_execute(conn, sql: str, params=None) -> Tuple[list, list]:
    """Execute SQL and return (rows, columns). Uses a fresh cursor each time."""
    cur = conn.cursor()
    try:
        cur.execute(sql, params or {})
        rows = cur.fetchall() if cur.description else []
        cols = [c[0] for c in cur.description] if cur.description else []
        return rows, cols
    finally:
        cur.close()


def _rule_table_name(rule_id: str, rule_title: str) -> str:
    """Derive the DQ_CORE table name for a rule — mirrors executor.py / consolidated_report.py."""
    safe_title = re.sub(r"[^A-Z0-9]", "_", (rule_title or rule_id).strip().upper()).strip("_")
    safe_id    = re.sub(r"[^A-Z0-9]", "_", (rule_id or "").strip().upper()).strip("_")
    name = f"{safe_title}_{safe_id}" if safe_id else safe_title
    if name and name[0].isdigit():
        name = f"RULE_{name}"
    return name


def _parse_color_columns(cc_str) -> Tuple[Optional[str], Optional[str]]:
    if not cc_str:
        return None, None
    parts = [p.strip().upper() for p in str(cc_str).split(",") if p.strip()]
    return (parts[0] if parts else None, parts[1] if len(parts) > 1 else None)


def _is_na(v) -> bool:
    try:
        return pd.isna(v)
    except (TypeError, ValueError):
        return False


def _safe_cell(v):
    if _is_na(v):
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    return str(v)


def _day_diff(v1, v2) -> Optional[int]:
    try:
        d1, d2 = pd.Timestamp(v1), pd.Timestamp(v2)
        if pd.isna(d1) or pd.isna(d2):
            return None
        return abs((d1 - d2).days)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Date input — resolve FROM_DATE / TO_DATE from DQ_DATE_INPUT
# ─────────────────────────────────────────────────────────────────────────────

def fetch_date_input(read_conn, database: str) -> Tuple[str, str]:
    """
    Query DQ_CORE.DQ_DATE_INPUT for the active quarterly date range.

    Filters: REPORT_SET = 'RTE_REPORT_QUARTERLY' AND IS_ACTIVE = TRUE
    Returns: (from_date, to_date) as pd.Timestamps.

    Exits with a clear error if:
      - 0 rows match  → nothing to process
      - 2+ rows match → ambiguous; script cannot safely pick one
    """
    tbl = f"{database}.DQ_CORE.DQ_DATE_INPUT"
    log.info(f"Fetching active quarterly date range from {tbl} …")
    rows, _ = sf_execute(read_conn, f"""
        SELECT FROM_DATE, TO_DATE
        FROM {tbl}
        WHERE REPORT_SET = 'RTE_REPORT_QUARTERLY'
          AND IS_ACTIVE  = TRUE
    """)

    if not rows:
        log.error(
            "No active row found in DQ_DATE_INPUT for "
            "REPORT_SET='RTE_REPORT_QUARTERLY' with IS_ACTIVE=TRUE — "
            "cannot determine report date range. Aborting."
        )
        sys.exit(1)

    if len(rows) > 1:
        log.error(
            f"Expected exactly 1 active row in DQ_DATE_INPUT for "
            f"REPORT_SET='RTE_REPORT_QUARTERLY', but found {len(rows)}:"
        )
        for r in rows:
            log.error(f"    FROM_DATE={r[0]}  TO_DATE={r[1]}")
        log.error(
            "Set IS_ACTIVE=FALSE on all but one row and re-run. Aborting."
        )
        sys.exit(1)

    from_date = pd.Timestamp(rows[0][0])
    to_date   = pd.Timestamp(rows[0][1])
    log.info(f"  → FROM_DATE: {from_date.date()}  TO_DATE: {to_date.date()}")
    return from_date, to_date


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — fetch rules
# ─────────────────────────────────────────────────────────────────────────────

def fetch_rules(read_conn, bdc_cfg: dict) -> Tuple[List[tuple], Dict[str, Tuple]]:
    """
    Returns:
        rules          – [(rule_id, rule_title, description), ...]
        rule_color_map – {rule_id: (col1, col2)}
    """
    database       = bdc_cfg["database"]
    schema         = bdc_cfg["schema"]
    rule_view      = f"{database}.{schema}.{bdc_cfg['rule_latest_view']}"
    rule_input_tbl = f"{database}.{schema}.{bdc_cfg['rule_input_table']}"

    log.info("Fetching active rules …")
    rows, _ = sf_execute(read_conn, f"""
        SELECT v.RULE_ID, v.RULE_TITLE, i.COLOR_COLUMNS, v.RULE_DESCRIPTION
        FROM {rule_view} v
        INNER JOIN {rule_input_tbl} i
            ON v.RULE_ID = i.RULE_ID
        WHERE i.IS_ACTIVE = TRUE
        ORDER BY v.RULE_ID
    """)

    rules          = [(r[0], r[1], r[3] or "") for r in rows]
    rule_color_map = {r[0]: _parse_color_columns(r[2]) for r in rows}

    log.info(f"  → {len(rules)} rules found")
    return rules, rule_color_map


def fetch_active_rules(read_conn, bdc_cfg: dict) -> Tuple[List[tuple], Dict[str, Tuple]]:
    """
    Returns rules where TARGET_TABLE IS NOT NULL in the rule input table.

    Returns:
        rules           – [(rule_id, rule_title, description), ...]
        rule_color_map  – {rule_id: (col1, col2)}
        target_table_map – {rule_id: TARGET_TABLE value}
    """
    database       = bdc_cfg["database"]
    schema         = bdc_cfg["schema"]
    rule_view      = f"{database}.{schema}.{bdc_cfg['rule_latest_view']}"
    rule_input_tbl = f"{database}.{schema}.{bdc_cfg['rule_input_table']}"

    log.info("Fetching rules opted-in for quarterly processing (TARGET_TABLE IS NOT NULL) …")
    rows, _ = sf_execute(read_conn, f"""
        SELECT v.RULE_ID, v.RULE_TITLE, i.COLOR_COLUMNS, v.RULE_DESCRIPTION,
               i.TARGET_TABLE
        FROM {rule_view} v
        INNER JOIN {rule_input_tbl} i
            ON v.RULE_ID = i.RULE_ID
        WHERE i.IS_ACTIVE      = TRUE
          AND i.TARGET_TABLE IS NOT NULL
        ORDER BY v.RULE_ID
    """)

    rules             = [(r[0], r[1], r[3] or "") for r in rows]
    rule_color_map    = {r[0]: _parse_color_columns(r[2]) for r in rows}
    target_table_map  = {r[0]: r[4] for r in rows}  # {rule_id: TARGET_TABLE value}

    log.info(f"  → {len(rules)} rule(s) found")
    if not rules:
        log.warning(
            "  No rules have TARGET_TABLE set in the rule input table. "
            "Ensure the TARGET_TABLE column exists and is populated for "
            "any rules you want included in the quarterly report."
        )
    return rules, rule_color_map, target_table_map


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — fetch each rule's failed rows
# ─────────────────────────────────────────────────────────────────────────────

def fetch_rule_dataframes(
    read_conn,
    rules: list,
    database: str,
) -> Tuple[Dict[str, pd.DataFrame], Dict[str, int]]:

    rule_dfs: Dict[str, pd.DataFrame] = {}
    rule_row_counts: Dict[str, int]   = {}

    for i, (rule_id, rule_title, *_) in enumerate(rules, 1):
        table_name = _rule_table_name(rule_id, rule_title)
        fq_table   = f"{database}.DQ_CORE.{table_name}"
        log.info(f"  [{i}/{len(rules)}] {rule_id}  →  {fq_table}")
        try:
            cur = read_conn.cursor()
            cur.execute(f"SELECT * FROM {fq_table}")
            batches = list(cur.fetch_pandas_batches())
            cur.close()
            df = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame()
        except Exception as exc:
            log.warning(f"    Skipped — {exc}")
            df = pd.DataFrame()
        rule_dfs[rule_id]        = df
        rule_row_counts[rule_id] = len(df)

    return rule_dfs, rule_row_counts


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — union, dedup, tag FAILURE_MODE
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_for_hash(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize a DataFrame so that equivalent values always produce the same
    string representation before hashing:
      - All nulls (None, NaN, NaT, "nan", "none", "nat", "") → ""
      - Timestamps / datetime columns → "YYYY-MM-DD HH:MM:SS" (no timezone)
      - Numeric-looking strings → stripped of trailing zeros (e.g. "1.0" → "1")
      - All other values → str(v).strip()
    """
    out = df.copy()
    for col in out.columns:
        series = out[col]
        if pd.api.types.is_datetime64_any_dtype(series):
            out[col] = (
                series
                .dt.tz_localize(None)
                .dt.floor("s")
                .dt.strftime("%Y-%m-%d %H:%M:%S")
                .fillna("")
            )
        else:
            def _norm(v):
                if v is None:
                    return ""
                try:
                    if pd.isna(v):
                        return ""
                except (TypeError, ValueError):
                    pass
                s = str(v).strip()
                if s.lower() in ("nan", "none", "nat", "null", "<na>"):
                    return ""
                # Normalise numeric strings: "1.0" → "1", "3.50" → "3.5"
                try:
                    f = float(s)
                    return str(int(f)) if f == int(f) else str(f)
                except (ValueError, OverflowError):
                    pass
                # Normalise date-like strings to a common format
                try:
                    ts = pd.Timestamp(s)
                    if ts is not pd.NaT:
                        return ts.floor("s").strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass
                return s
            out[col] = series.map(_norm)
    return out


def build_result_dataframe(
    rule_dfs: Dict[str, pd.DataFrame],
    source_df: pd.DataFrame,
) -> Tuple[pd.DataFrame, int]:
    """
    Returns (result_df, total_rows).

    result_df contains ALL rows from source_df.  Rows that appear in one or
    more rule failed-row tables are tagged with a comma-separated
    FAILURE_MODE; all other rows get an empty FAILURE_MODE and no colour coding
    in the Excel output.

    Matching is done per-rule: for each rule's table, only the columns
    that also exist in source_df are used to fingerprint the failed rows.  The
    source rows are hashed on that *same per-rule column set* so the fingerprints
    are directly comparable.  Both sides are normalised before hashing so that
    dtype differences (Timestamp vs string, NaN vs None, "1.0" vs "1") don't
    cause false mismatches.
    """
    total_rows = len(source_df)
    result = source_df.copy()
    result["FAILURE_MODE"] = ""

    source_cols_upper: Dict[str, str] = {c.upper(): c for c in source_df.columns}

    has_failures = any(not df.empty for df in rule_dfs.values())
    if not has_failures:
        log.info("  No failed rows found — all source rows will have blank FAILURE_MODE.")
        return result, total_rows

    # row_rule_ids: source-row index → list of rule_ids that flagged it
    row_rule_ids: Dict[int, List[str]] = {}

    for rule_id, df in rule_dfs.items():
        if df.empty:
            continue

        # Only keep columns that exist in the source table (case-insensitive)
        match_cols_rule   = [c for c in df.columns if c.upper() in source_cols_upper]
        match_cols_source = [source_cols_upper[c.upper()] for c in match_cols_rule]

        if not match_cols_rule:
            log.warning(
                f"  Rule {rule_id}: no columns overlap with source table "
                f"— cannot fingerprint; rule skipped."
            )
            continue

        log.info(
            f"  Rule {rule_id}: matching on {len(match_cols_rule)} column(s): "
            f"{match_cols_rule[:5]}{'…' if len(match_cols_rule) > 5 else ''}"
        )

        # Normalise both sides before hashing so dtype differences don't matter
        norm_rule   = _normalize_for_hash(df[match_cols_rule])
        norm_source = _normalize_for_hash(result[match_cols_source].rename(
            columns={s: d for s, d in zip(match_cols_source, match_cols_rule)}
        ))

        failed_fp_set = set(
            pd.util.hash_pandas_object(norm_rule, index=False)
        )
        source_fp = pd.util.hash_pandas_object(norm_source, index=False)

        matched = 0
        for idx, h in source_fp.items():
            if h in failed_fp_set:
                row_rule_ids.setdefault(idx, []).append(rule_id)
                matched += 1
        log.info(f"  Rule {rule_id}: {matched:,} source row(s) matched.")

    # Write FAILURE_MODE back onto result
    for idx, rule_ids in row_rule_ids.items():
        result.at[idx, "FAILURE_MODE"] = ", ".join(sorted(set(rule_ids)))

    failed_count = int((result["FAILURE_MODE"] != "").sum())
    log.info(
        f"  Tagged {failed_count:,} failed row(s) out of {total_rows:,} total source row(s) "
        f"({total_rows - failed_count:,} non-failed rows included with blank FAILURE_MODE)."
    )
    return result, total_rows


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — fetch source snapshot from TARGET_TABLE source tables
# ─────────────────────────────────────────────────────────────────────────────

def fetch_source_snapshot(
    read_conn,
    target_table_map: Dict[str, str],
    database: str,
) -> pd.DataFrame:
    """
    Fetch the full dataset from each unique source table listed in
    target_table_map ({rule_id: TARGET_TABLE}).  Deduplicates by table name
    so the same source table is only fetched once, then unions all results
    into a single DataFrame.
    """
    # Unique source tables (multiple rules may share the same source table)
    unique_tables: Dict[str, str] = {}  # {table_name: rule_id for logging}
    for rule_id, tbl in target_table_map.items():
        if tbl and tbl not in unique_tables:
            unique_tables[tbl] = rule_id

    log.info(f"  Fetching source data from {len(unique_tables)} unique table(s) …")
    frames: List[pd.DataFrame] = []

    for i, (tbl_name, rule_id) in enumerate(unique_tables.items(), 1):
        # Support both fully-qualified and unqualified table names
        fq_table = tbl_name if "." in tbl_name else f"{database}.DQ_CORE.{tbl_name}"
        log.info(f"  [{i}/{len(unique_tables)}] {fq_table}")
        try:
            cur = read_conn.cursor()
            cur.execute(f"SELECT * FROM {fq_table}")
            batches = list(cur.fetch_pandas_batches())
            cur.close()
            df = pd.concat(batches, ignore_index=True) if batches else pd.DataFrame()
            log.info(f"    → {len(df):,} rows")
            frames.append(df)
        except Exception as exc:
            log.warning(f"    Skipped — {exc}")

    if not frames:
        log.warning("  No source data fetched — returning empty DataFrame.")
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    log.info(f"  Source snapshot total: {len(combined):,} rows across {len(frames)} table(s)")
    return combined


def write_source_snapshot_to_snowflake(
    write_conn,
    database: str,
    snapshot_name: str,
    source_df: pd.DataFrame,
    write_connection_factory,
) -> None:
    """
    Persist the source snapshot DataFrame to Snowflake as
    EMAIL_QUARTERLY_SOURCE_SNAPSHOT_<ts> in DQ_CORE, using the same
    parallel-insert pattern as write_to_snowflake().
    """
    if source_df.empty:
        log.warning("  Source snapshot is empty — skipping Snowflake write.")
        return

    full_table = f"{database}.DQ_CORE.{snapshot_name}"
    log.info(f"Creating source snapshot table {full_table} …")

    col_ddl = ", ".join(
        f'"{c.upper().replace(" ", "_")}" VARCHAR'
        for c in source_df.columns
    )
    cur = write_conn.cursor()
    try:
        cur.execute(f"CREATE TABLE {full_table} ({col_ddl})")
    finally:
        cur.close()

    log.info(f"Serialising {len(source_df):,} rows …")
    cols         = [c.upper().replace(" ", "_") for c in source_df.columns]
    col_names    = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    insert_sql   = f"INSERT INTO {full_table} ({col_names}) VALUES ({placeholders})"

    arr = source_df.to_numpy(dtype=object, na_value=None)
    for _ci, _dt in enumerate(source_df.dtypes):
        if pd.api.types.is_datetime64_any_dtype(_dt):
            arr[:, _ci] = (
                source_df.iloc[:, _ci]
                .dt.strftime("%Y-%m-%d %H:%M:%S")
                .to_numpy(dtype=object, na_value=None)
            )
    _obj_col_idxs = [i for i, dt in enumerate(source_df.dtypes) if dt == object]
    for _ci in _obj_col_idxs:
        _col = arr[:, _ci]
        _bad = [_ri for _ri, _v in enumerate(_col)
                if _v is not None and not isinstance(_v, (str, int, float, bool))]
        for _ri in _bad:
            _col[_ri] = str(_col[_ri])
    all_rows   = list(map(tuple, arr))
    total_rows = len(all_rows)

    if total_rows == 0:
        log.info(f"  No rows to insert — source snapshot table created empty: {full_table}")
        return

    MAX_WORKERS    = 8
    ROWS_PER_CHUNK = 5_000
    BATCH_PER_EXEC = 5_000
    can_parallel   = total_rows > ROWS_PER_CHUNK
    n_workers      = min(MAX_WORKERS, math.ceil(total_rows / ROWS_PER_CHUNK)) if can_parallel else 1
    chunk_size     = math.ceil(total_rows / n_workers)
    chunks         = [all_rows[i: i + chunk_size] for i in range(0, total_rows, chunk_size)]

    log.info(f"Inserting rows: {total_rows:,} → {n_workers} worker(s) × ~{chunk_size:,} rows …")

    def _write_chunk(chunk: list, idx: int) -> int:
        conn = write_connection_factory()
        try:
            c = conn.cursor()
            try:
                for start in range(0, len(chunk), BATCH_PER_EXEC):
                    c.executemany(insert_sql, chunk[start: start + BATCH_PER_EXEC])
            finally:
                c.close()
        finally:
            conn.close()
        return len(chunk)

    if n_workers == 1:
        cur = write_conn.cursor()
        try:
            done = 0
            for start in range(0, total_rows, BATCH_PER_EXEC):
                cur.executemany(insert_sql, all_rows[start: start + BATCH_PER_EXEC])
                done = min(start + BATCH_PER_EXEC, total_rows)
                log.info(f"  Inserted {done:,} / {total_rows:,} rows")
        finally:
            cur.close()
    else:
        completed = 0
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(_write_chunk, chunk, i): i
                       for i, chunk in enumerate(chunks)}
            for future in as_completed(futures):
                completed += future.result()
                log.info(f"  Inserted {completed:,} / {total_rows:,} rows")

    log.info(f"  ✅ Source snapshot table written: {full_table}")




# ─────────────────────────────────────────────────────────────────────────────
# Registry — write a run row to CONSOLIDATED_REPORT_REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

def write_registry_row_to_snowflake(
    write_conn,
    database:        str,
    report_name:     str,
    run_timestamp:   str,
    result_df:       "pd.DataFrame",
    source_df:       "pd.DataFrame",
    rules:           list,
    rule_color_map:  Dict[str, Tuple],
    rule_row_counts: Dict[str, int],
    registry_table:  str = "CONSOLIDATED_REPORT_REGISTRY",
) -> None:
    """
    Append one row to the consolidated report registry table so every
    quarterly-report run is tracked alongside delta-report runs.

    Columns written (matching the existing registry schema):
        REPORT_NAME, GENERATED_AT, TOTAL_ROWS, TOTAL_ROWS_BEFORE_DEDUP,
        DUPLICATES_REMOVED, RULES_COUNT, COLUMNS_LIST, RULE_COLOR_MAP,
        RULE_COUNTS, RULES_JSON
    """
    full_table = f"{database}.DQ_CORE.{registry_table}"
    log.info(f"Writing registry row to {full_table} …")

    # Snowflake rejects timezone abbreviations like "EDT" / "EST".
    # Use a numeric UTC offset (e.g. "2026-05-14 21:35:48 -0400") instead.
    generated_at = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %z")

    # Serialise structured fields to JSON strings
    columns_list_json = json.dumps(list(result_df.columns))
    rule_color_map_json = json.dumps(
        {rid: list(v) for rid, v in rule_color_map.items()}
    )
    rule_counts_json = json.dumps(rule_row_counts)
    rules_json = json.dumps([[r[0], r[1], r[2] if len(r) > 2 else ""] for r in rules])

    total_rows             = len(result_df)
    total_rows_before_dedup = len(source_df)   # quarterly report does no dedup
    duplicates_removed     = 0

    insert_sql = f"""
        INSERT INTO {full_table} (
            REPORT_NAME, GENERATED_AT, TOTAL_ROWS, TOTAL_ROWS_BEFORE_DEDUP,
            DUPLICATES_REMOVED, RULES_COUNT, COLUMNS_LIST, RULE_COLOR_MAP,
            RULE_COUNTS, RULES_JSON
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    row = (
        report_name,
        generated_at,
        total_rows,
        total_rows_before_dedup,
        duplicates_removed,
        len(rules),
        columns_list_json,
        rule_color_map_json,
        rule_counts_json,
        rules_json,
    )

    cur = write_conn.cursor()
    try:
        cur.execute(insert_sql, row)
        log.info(f"  ✅ Registry row written: {report_name}")
    except Exception as exc:
        log.warning(f"  Registry write FAILED — {exc}  (report still saved; continuing)")
    finally:
        cur.close()


# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — build Excel report
# ─────────────────────────────────────────────────────────────────────────────

def build_report_excel(
    result_df:      pd.DataFrame,
    from_date:      "pd.Timestamp",
    to_date:        "pd.Timestamp",
    rules:          list,
    rule_color_map: Dict[str, Tuple],
    output_path:    Path,
) -> None:
    """Write the colour-coded quarterly report Excel workbook to *output_path*.

    result_df contains all source rows; failed rows have FAILURE_MODE populated
    and receive colour coding, non-failed rows are plain white.
    """
    log.info(f"Building quarterly report Excel workbook ({len(result_df):,} total rows) …")

    buf = io.BytesIO()
    wb  = xlsxwriter.Workbook(buf, {"in_memory": True, "constant_memory": False})

    # ── Formats ───────────────────────────────────────────────────────────────
    def _fmt(**kw):
        return wb.add_format({"font_name": "Calibri", "font_size": 10, "border": 1, **kw})

    title_fmt   = _fmt(bold=True, font_size=15, bg_color="#8c3bbb", font_color="#FFFFFF",
                       border=0, align="left", valign="vcenter")
    section_fmt = _fmt(bold=True, font_size=11, bg_color="#312C2A", font_color="#FFFFFF",
                       border=0, align="left", valign="vcenter")
    label_fmt   = _fmt(bold=True, bg_color="#E8E6E3", font_color="#312C2A", valign="top")
    value_fmt   = _fmt(bg_color="#FFFFFF", font_color="#312C2A", valign="top")
    meta_fmt    = _fmt(italic=True, font_size=9, bg_color="#FAFAFA", font_color="#81766F", border=0)
    hdr_fmt     = _fmt(bold=True, bg_color="#E8E6E3", font_color="#312C2A", align="center")
    cell_fmt    = _fmt(bg_color="#FFFFFF", font_color="#312C2A")
    hdr_color_fmt = _fmt(bold=True, bg_color="#D1FAE5", font_color="#312C2A",
                         align="center", left=2, left_color="#328714")

    # Row background formats (same timeliness palette)
    fmt_green    = _fmt(bg_color="#DCFCE7", left=2, left_color="#328714")
    fmt_yellow   = _fmt(bg_color="#FEF9C3", left=2, left_color="#CA8A04")
    fmt_red_row  = _fmt(bg_color="#FEE2E2", left=2, left_color="#EB1700")

    # FAILURE_MODE text formats
    _rf = {"font_name": "Calibri", "font_size": 10}
    fmt_rule_green  = wb.add_format({**_rf, "font_color": "#16A34A"})
    fmt_rule_orange = wb.add_format({**_rf, "bold": True, "font_color": "#FB923C"})
    fmt_rule_red    = wb.add_format({**_rf, "font_color": "#DC2626"})
    fmt_rule_black  = wb.add_format({**_rf, "font_color": "#312C2A"})
    sep_run_fmt     = wb.add_format({"font_name": "Calibri", "font_size": 10,
                                     "font_color": "#A39992"})
    rule_cell_base  = wb.add_format({"bg_color": "#FFFFFF", "border": 1,
                                     "font_name": "Calibri", "font_size": 10})

    _sr_fmt_cache: dict = {}
    def _get_sr_fmt(fc, bold):
        k = (fc, bold)
        if k not in _sr_fmt_cache:
            _sr_fmt_cache[k] = wb.add_format({
                "bg_color": "#FFFFFF", "border": 1,
                "font_name": "Calibri", "font_size": 10,
                "bold": bold, "font_color": fc,
            })
        return _sr_fmt_cache[k]

    now_str    = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    rcm        = rule_color_map or {}
    _BG_PRI    = {"#DCFCE7": 0, "#FEF9C3": 1, "#FEE2E2": 2}

    # ── Shared extra formats for Summary ──────────────────────────────────────
    sheet_hdr_fmt  = _fmt(bold=True, bg_color="#E8E6E3", font_color="#312C2A",
                          align="center", valign="vcenter")
    sheet_name_fmt = _fmt(bold=True, bg_color="#F5F4F2", font_color="#312C2A", valign="vcenter")
    sheet_desc_fmt = _fmt(bg_color="#FFFFFF", font_color="#312C2A", valign="vcenter", text_wrap=True)
    rule_desc_wrap = _fmt(bg_color="#FFFFFF", font_color="#312C2A", valign="top", text_wrap=True)

    # ── Summary sheet ──────────────────────────────────────────────────────────
    ws_sum = wb.add_worksheet("Summary")
    ws_sum.set_column(0, 0, 32)
    ws_sum.set_column(1, 1, 48)
    ws_sum.set_column(2, 2, 6)
    ws_sum.set_column(3, 3, 24)
    ws_sum.set_column(4, 4, 40)
    ws_sum.set_column(5, 5, 60)

    # Title
    ws_sum.set_row(0, 32)
    ws_sum.merge_range(0, 0, 0, 1, "  DQE RTE Quarterly Report", title_fmt)
    ws_sum.merge_range(0, 3, 0, 5, "  Rule Details", title_fmt)
    ws_sum.set_row(1, 4)

    # ── Section: Report Overview ──────────────────────────────────────────────
    ws_sum.set_row(2, 20)
    ws_sum.merge_range(2, 0, 2, 1, "  Report Overview", section_fmt)

    _failed_count = int((result_df["FAILURE_MODE"].str.strip() != "").sum()) if "FAILURE_MODE" in result_df.columns else 0
    overview_rows = [
        ("Events Created From Date", from_date.strftime("%d-%b-%Y")),
        ("Events Created To Date",   to_date.strftime("%d-%b-%Y")),
        ("Total Rows",     f"{len(result_df):,}"),
        ("Failed Rows",    f"{_failed_count:,}"),
        ("Non-Failed Rows", f"{len(result_df) - _failed_count:,}"),
        ("Rules in Scope", str(len(rules))),
    ]
    for i, (lbl, val) in enumerate(overview_rows):
        r = i + 3
        ws_sum.set_row(r, 18)
        ws_sum.write(r, 0, lbl, label_fmt)
        ws_sum.write(r, 1, val, value_fmt)

    # ── Section: Sheet Guide ──────────────────────────────────────────────────
    guide_start = len(overview_rows) + 4
    ws_sum.set_row(guide_start, 4)
    ws_sum.set_row(guide_start + 1, 20)
    ws_sum.merge_range(guide_start + 1, 0, guide_start + 1, 1, "  Sheet Guide", section_fmt)
    ws_sum.set_row(guide_start + 2, 16)
    ws_sum.write(guide_start + 2, 0, "Sheet",       sheet_hdr_fmt)
    ws_sum.write(guide_start + 2, 1, "Description", sheet_hdr_fmt)

    sheet_guide = [
        ("Summary",      "This sheet — report overview, sheet guide, and rule details."),
        ("Color Legend", "Explains all colour coding: row backgrounds, FAILURE_MODE text colours, header highlights, and gradient fills."),
        ("Data",         "All rows from the source dataset. Failed rows are colour-coded with FAILURE_MODE populated; non-failed rows are plain white with a blank FAILURE_MODE."),
    ]
    for i, (sheet_name, sheet_desc) in enumerate(sheet_guide):
        r = guide_start + 3 + i
        ws_sum.set_row(r, 32)
        ws_sum.write(r, 0, sheet_name, sheet_name_fmt)
        ws_sum.write(r, 1, sheet_desc, sheet_desc_fmt)

    footer_r = guide_start + 3 + len(sheet_guide) + 1
    ws_sum.merge_range(footer_r, 0, footer_r, 1,
                       f"  Generated by GRA-SmartHub  ·  {now_str}", meta_fmt)

    # ── Rule Details (right side columns 3-5) ────────────────────────────────
    ws_sum.set_row(2, 20)
    ws_sum.merge_range(2, 3, 2, 5, "  Rule ID · Title · Description", section_fmt)
    ws_sum.set_row(3, 16)
    ws_sum.write(3, 3, "Rule ID",      label_fmt)
    ws_sum.write(3, 4, "Title",        label_fmt)
    ws_sum.write(3, 5, "Description",  label_fmt)

    for i, rule_entry in enumerate(rules):
        r          = 4 + i
        rule_id    = rule_entry[0]
        rule_title = rule_entry[1]
        rule_desc  = rule_entry[2] if len(rule_entry) > 2 else ""
        chars_per_line = 60
        desc_lines     = max(1, -(-len(rule_desc) // chars_per_line))
        row_h          = min(409, max(32, desc_lines * 15 + 8))
        ws_sum.set_row(r, row_h)
        ws_sum.write(r, 3, rule_id,    value_fmt)
        ws_sum.write(r, 4, rule_title, value_fmt)
        ws_sum.write(r, 5, rule_desc,  rule_desc_wrap)

    # ── Helper: write a data sheet (new or resolved) ──────────────────────────
    def _write_data_sheet(ws, df: pd.DataFrame, sheet_label: str) -> None:
        if df.empty:
            ws.merge_range(0, 0, 0, 4, f"  No {sheet_label.lower()} rows in this run.", label_fmt)
            return

        final_cols    = list(df.columns)
        fm_col        = "FAILURE_MODE"
        fm_idx        = final_cols.index(fm_col) if fm_col in final_cols else -1
        stripped_cols = [c.strip() for c in final_cols]
        col_index     = {c: i for i, c in enumerate(stripped_cols)}

        color_col_indices: set = set()
        for col1, col2 in rcm.values():
            if col1 and col1 in col_index:
                color_col_indices.add(col_index[col1])
            if col2 and col2 in col_index:
                color_col_indices.add(col_index[col2])

        # Identify date columns by name (contain DATE but not DIFFERENCE)
        date_col_indices: set = {
            i for i, c in enumerate(final_cols)
            if 'DATE' in c.upper() and 'DIFFERENCE' not in c.upper()
        }
        _date_num_fmt = "dd-mmm-yyyy"
        _date_fmt_map = {
            "green":   _fmt(bg_color="#DCFCE7", left=2, left_color="#328714", num_format=_date_num_fmt),
            "yellow":  _fmt(bg_color="#FEF9C3", left=2, left_color="#CA8A04", num_format=_date_num_fmt),
            "red":     _fmt(bg_color="#FEE2E2", left=2, left_color="#EB1700", num_format=_date_num_fmt),
            "default": _fmt(bg_color="#FFFFFF", font_color="#312C2A",          num_format=_date_num_fmt),
        }

        # Pre-convert every date column to datetime64 so values in the loop are
        # guaranteed to be pd.Timestamp or pd.NaT — never raw strings or mixed types.
        df = df.copy()
        for _di in date_col_indices:
            _cn = final_cols[_di]
            _series = pd.to_datetime(df[_cn], errors="coerce", utc=False)
            if hasattr(_series.dtype, "tz") and _series.dtype.tz is not None:
                _series = _series.dt.tz_localize(None)
            df[_cn] = _series

        for c in range(len(final_cols)):
            ws.set_column(c, c, 100 if c == fm_idx else 22)

        for c, col in enumerate(final_cols):
            ws.write(0, c, col, hdr_color_fmt if c in color_col_indices else hdr_fmt)

        def _rule_font_fmt(rid, row_vals):
            pair = rcm.get(rid)
            if not pair or not pair[0] or not pair[1]:
                return fmt_rule_black
            ci1 = col_index.get(pair[0], -1)
            ci2 = col_index.get(pair[1], -1)
            if ci1 == -1 or ci2 == -1:
                return fmt_rule_black
            days = _day_diff(row_vals[ci1], row_vals[ci2])
            if days is None:    return fmt_rule_red
            if days <= 7:       return fmt_rule_green
            if days <= 15:      return fmt_rule_orange
            return fmt_rule_red

        CHUNK = 2_000
        total = len(df)
        for chunk_start in range(0, total, CHUNK):
            chunk = df.iloc[chunk_start: chunk_start + CHUNK]
            for ri, row_t in enumerate(chunk.itertuples(index=False), start=chunk_start + 1):
                vals = list(row_t)

                cell_bg: dict = {}
                fm_val   = vals[fm_idx] if fm_idx >= 0 else None
                rule_ids = ([r.strip() for r in str(fm_val).split(",") if r.strip()]
                            if fm_val and not _is_na(fm_val) else [])

                # ── Non-failed row: write plainly with no colour coding ────────
                if not rule_ids:
                    for ci, val in enumerate(vals):
                        if ci == fm_idx:
                            ws.write(ri, ci, "", rule_cell_base)
                        elif ci in date_col_indices:
                            if isinstance(val, pd.Timestamp) and not pd.isna(val):
                                ws.write_datetime(
                                    ri, ci,
                                    val.to_pydatetime().replace(tzinfo=None),
                                    _date_fmt_map["default"],
                                )
                            else:
                                ws.write(ri, ci, "", cell_fmt)
                        else:
                            ws.write(
                                ri, ci,
                                "" if _is_na(val) else _safe_cell(val),
                                cell_fmt,
                            )
                    continue  # skip colour-coding block below

                for _rid in rule_ids:
                    _p = rcm.get(_rid)
                    if _p and _p[0] and _p[1]:
                        _ci1 = col_index.get(_p[0], -1)
                        _ci2 = col_index.get(_p[1], -1)
                        if _ci1 != -1 and _ci2 != -1:
                            _days = _day_diff(vals[_ci1], vals[_ci2])
                            _bg   = ("#DCFCE7" if (_days is not None and _days <= 7)
                                     else "#FEF9C3" if (_days is not None and _days <= 15)
                                     else "#FEE2E2")
                            cell_bg.setdefault(_ci1, []).append(_bg)
                            cell_bg.setdefault(_ci2, []).append(_bg)

                cell_fmts: dict = {}
                for _ci, _bgs in cell_bg.items():
                    unique_bgs = list(dict.fromkeys(_bgs))
                    worst = max(unique_bgs, key=lambda b: _BG_PRI[b])
                    cell_fmts[_ci] = (fmt_green   if worst == "#DCFCE7"
                                      else fmt_yellow if worst == "#FEF9C3"
                                      else fmt_red_row)
                    if len(unique_bgs) > 1:
                        sorted_bgs = sorted(unique_bgs, key=lambda b: -_BG_PRI[b])
                        _active_gradient_map[0][(ri + 1, _ci + 1)] = sorted_bgs

                for ci, val in enumerate(vals):
                    if ci == fm_idx:
                        fr_text = "" if _is_na(val) else _safe_cell(val)
                        if not fr_text:
                            ws.write(ri, ci, "", rule_cell_base)
                        else:
                            rparts = [r.strip() for r in str(fr_text).split(",") if r.strip()]
                            if len(rparts) <= 1:
                                single = rparts[0] if rparts else fr_text
                                ff = _rule_font_fmt(single, vals)
                                fc, bold = (("#16A34A", False) if ff is fmt_rule_green
                                            else ("#FB923C", True) if ff is fmt_rule_orange
                                            else ("#DC2626", False) if ff is fmt_rule_red
                                            else ("#312C2A", False))
                                ws.write(ri, ci, single, _get_sr_fmt(fc, bold))
                            else:
                                rich_args = []
                                for idx2, rule in enumerate(rparts):
                                    if idx2 > 0:
                                        rich_args.extend([sep_run_fmt, " · "])
                                    rich_args.extend([_rule_font_fmt(rule, vals), rule])
                                ws.write_rich_string(ri, ci, *rich_args, rule_cell_base)
                    else:
                        bg_fmt = cell_fmts.get(ci)
                        if ci in date_col_indices:
                            # After pre-conversion, val is pd.Timestamp or pd.NaT
                            if isinstance(val, pd.Timestamp) and not pd.isna(val):
                                bg_key = ("green"   if bg_fmt is fmt_green
                                          else "yellow" if bg_fmt is fmt_yellow
                                          else "red"    if bg_fmt is fmt_red_row
                                          else "default")
                                ws.write_datetime(ri, ci,
                                                  val.to_pydatetime().replace(tzinfo=None),
                                                  _date_fmt_map[bg_key])
                            else:
                                ws.write(ri, ci, "",
                                         bg_fmt if bg_fmt is not None else cell_fmt)
                        else:
                            ws.write(ri, ci,
                                     "" if _is_na(val) else _safe_cell(val),
                                     bg_fmt if bg_fmt is not None else cell_fmt)

    # Per-sheet gradient map
    gradient_map: dict = {}
    _active_gradient_map: list = [gradient_map]

    # ── Data sheet (all rows, failed + non-failed) ────────────────────────────
    ws_data = wb.add_worksheet("Data")
    _write_data_sheet(ws_data, result_df, "DATA")

    wb.close()
    ws_leg = wb.add_worksheet("Color Legend")
    ws_leg.set_column(0, 0, 6)
    ws_leg.set_column(1, 1, 32)
    ws_leg.set_column(2, 2, 72)

    leg_title_fmt   = _fmt(bold=True, font_size=15, bg_color="#8c3bbb", font_color="#FFFFFF",
                           border=0, align="left", valign="vcenter")
    leg_section_fmt = _fmt(bold=True, font_size=11, bg_color="#312C2A", font_color="#FFFFFF",
                           border=0, align="left", valign="vcenter")
    leg_label_fmt_l = _fmt(bold=True, bg_color="#F5F4F2", font_color="#312C2A", valign="vcenter")
    leg_desc_fmt    = _fmt(bg_color="#FFFFFF", font_color="#312C2A", valign="vcenter", text_wrap=True)
    leg_note_fmt    = _fmt(italic=True, font_size=9, bg_color="#FAFAFA", font_color="#81766F", border=0)

    def _swatch(bg, border_color=None):
        kw = {"bg_color": bg, "border": 1}
        if border_color:
            kw.update({"left": 3, "left_color": border_color})
        return _fmt(**kw)

    def _leg_row(r, sw_fmt, label, description, row_h=22):
        ws_leg.set_row(r, row_h)
        ws_leg.write(r, 0, "", sw_fmt)
        ws_leg.write(r, 1, label,       leg_label_fmt_l)
        ws_leg.write(r, 2, description, leg_desc_fmt)

    ws_leg.set_row(0, 32)
    ws_leg.merge_range(0, 0, 0, 2, "  Color Legend — Quarterly DQ Report", leg_title_fmt)
    ws_leg.set_row(1, 4)

    # Section 1: Row background
    ws_leg.set_row(2, 20)
    ws_leg.merge_range(2, 0, 2, 2, "  Row Background Color (date-based timeliness)", leg_section_fmt)
    _leg_row(3, _swatch("#DCFCE7", "#328714"), "Green  (≤ 7 days)",
             "The difference between the two comparison date columns is 7 days or fewer.")
    _leg_row(4, _swatch("#FEF9C3", "#CA8A04"), "Yellow  (8 – 15 days)",
             "The date difference is between 8 and 15 days inclusive.")
    _leg_row(5, _swatch("#FEE2E2", "#EB1700"), "Red  (≥ 16 days or missing date)",
             "The date difference is 16 or more days, or one/both date values are null.")
    _leg_row(6, _swatch("#FFFFFF"), "White  (no date columns configured)",
             "The rule that flagged this row does not have color columns configured.")
    ws_leg.set_row(7, 4)

    # Section 2: Multi-rule gradient
    ws_leg.set_row(8, 20)
    ws_leg.merge_range(8, 0, 8, 2, "  Multi-Rule Rows — Gradient Cell Fill", leg_section_fmt)
    ws_leg.set_row(9, 36)
    ws_leg.write(9, 0, "", _swatch("#FEE2E2"))
    ws_leg.write(9, 1, "Gradient fill", leg_label_fmt_l)
    ws_leg.write(9, 2,
                 "When a single row failed multiple rules and those rules produce different "
                 "timeliness colors, the relevant date cells display a gradient blending each "
                 "rule's color. The gradient runs worst-severity first (red → yellow → green). "
                 "The row background uses the single worst color across all applicable rules.",
                 leg_desc_fmt)
    ws_leg.set_row(10, 4)

    # Section 3: FAILURE_MODE text colors
    ws_leg.set_row(11, 20)
    ws_leg.merge_range(11, 0, 11, 2, "  FAILURE_MODE Column — Text Color", leg_section_fmt)

    leg_rule_green_fmt  = wb.add_format({"font_name": "Calibri", "font_size": 10,
                                         "font_color": "#16A34A", "bg_color": "#FFFFFF", "border": 1})
    leg_rule_orange_fmt = wb.add_format({"font_name": "Calibri", "font_size": 10, "bold": True,
                                         "font_color": "#FB923C", "bg_color": "#FFFFFF", "border": 1})
    leg_rule_red_fmt    = wb.add_format({"font_name": "Calibri", "font_size": 10,
                                         "font_color": "#DC2626", "bg_color": "#FFFFFF", "border": 1})
    leg_rule_black_fmt  = wb.add_format({"font_name": "Calibri", "font_size": 10,
                                         "font_color": "#312C2A", "bg_color": "#FFFFFF", "border": 1})

    ws_leg.set_row(12, 22); ws_leg.write(12, 0, "", _swatch("#FFFFFF"))
    ws_leg.write(12, 1, "RULE_ID", leg_rule_green_fmt)
    ws_leg.write(12, 2, "Green text — the rule's comparison dates are within 7 days (timely).", leg_desc_fmt)

    ws_leg.set_row(13, 22); ws_leg.write(13, 0, "", _swatch("#FFFFFF"))
    ws_leg.write(13, 1, "RULE_ID", leg_rule_orange_fmt)
    ws_leg.write(13, 2, "Bold orange text — the rule's comparison dates are 8–15 days apart (warning).", leg_desc_fmt)

    ws_leg.set_row(14, 22); ws_leg.write(14, 0, "", _swatch("#FFFFFF"))
    ws_leg.write(14, 1, "RULE_ID", leg_rule_red_fmt)
    ws_leg.write(14, 2, "Red text — the rule's comparison dates are ≥ 16 days apart, or a date is missing (overdue / no data).", leg_desc_fmt)

    ws_leg.set_row(15, 22); ws_leg.write(15, 0, "", _swatch("#FFFFFF"))
    ws_leg.write(15, 1, "RULE_ID", leg_rule_black_fmt)
    ws_leg.write(15, 2, "Black text — the rule has no color columns configured; no timeliness score is computed.", leg_desc_fmt)
    ws_leg.set_row(16, 4)

    # Section 4: Column header highlight
    ws_leg.set_row(17, 20)
    ws_leg.merge_range(17, 0, 17, 2, "  Column Header Highlight", leg_section_fmt)
    _leg_row(18, _fmt(bold=True, bg_color="#D1FAE5", font_color="#312C2A", align="center",
                      left=2, left_color="#328714"),
             "Green-tinted header",
             "This column is used as a date comparison column by at least one rule. "
             "Its values drive the green / yellow / red timeliness coloring for that rule.")
    _leg_row(19, _fmt(bold=True, bg_color="#E8E6E3", font_color="#312C2A", align="center"),
             "Gray header",
             "Standard data column — not used as a color/date comparison column by any rule.")
    ws_leg.set_row(20, 4)

    # Section 5: How color columns work
    ws_leg.set_row(21, 20)
    ws_leg.merge_range(21, 0, 21, 2, "  How Color Columns Work", leg_section_fmt)
    ws_leg.set_row(22, 60)
    ws_leg.write(22, 0, "", _swatch("#FAFAFA"))
    ws_leg.write(22, 1, "Color columns", leg_label_fmt_l)
    ws_leg.write(22, 2,
                 "Each rule can optionally declare two date columns (e.g. EVENT_DATE and "
                 "PROCESS_DATE). The absolute difference in days between those two columns "
                 "for a given row determines the timeliness category: ≤ 7 days = green, "
                 "8–15 days = yellow, ≥ 16 days or null = red. Rules without declared "
                 "color columns produce no background or text coloring.",
                 leg_desc_fmt)
    ws_leg.set_row(23, 4)
    ws_leg.set_row(24, 14)
    ws_leg.merge_range(24, 0, 24, 2,
                       f"  Generated by GRA-SmartHub  ·  {now_str}", leg_note_fmt)

    buf.seek(0)

    # ── openpyxl post-processing: date formatting + gradient fills ────────────
    log.info("  Starting openpyxl post-processing (date formatting + gradients) ...")
    try:
        import openpyxl as _oxl
        from openpyxl.styles import GradientFill as _GF
        from openpyxl.styles.fills import Stop as _Stop
        from datetime import datetime as _dt_type

        raw_buf = io.BytesIO(buf.read())
        _wb2 = _oxl.load_workbook(raw_buf, rich_text=True)
        log.info(f"  Workbook loaded. Sheets: {_wb2.sheetnames}")
        _EXCEL_EPOCH = _dt_type(1899, 12, 30)
        _EXCEL_DATE_FMT = "DD-MMM-YYYY"

        sheet_key_map = {"Data": gradient_map}
        for sheet_name, gcm in sheet_key_map.items():
            if sheet_name not in _wb2.sheetnames:
                continue
            _ws2 = _wb2[sheet_name]

            # 1. Find date columns from header row
            _date_col_nums: set = set()
            for _hdr in next(_ws2.iter_rows(min_row=1, max_row=1), []):
                _hv = str(_hdr.value or "").upper()
                if "DATE" in _hv and "DIFFERENCE" not in _hv:
                    _date_col_nums.add(_hdr.column)
            log.info(f"  '{sheet_name}': {len(_date_col_nums)} date column(s) detected")

            # 2. Convert date cells to real Excel dates using serial numbers
            _converted = 0
            _skipped   = 0
            for _col_idx in _date_col_nums:
                for _row in _ws2.iter_rows(
                        min_row=2, min_col=_col_idx, max_col=_col_idx):
                    _dc = _row[0]
                    # Already a datetime
                    if isinstance(_dc.value, _dt_type):
                        _dc.number_format = _EXCEL_DATE_FMT
                        _converted += 1
                        continue
                    # Parse string to datetime
                    _sv = str(_dc.value).strip() if _dc.value is not None else ""
                    if not _sv or _sv.lower() in ("none", "nat", "null", ""):
                        _skipped += 1
                        continue
                    _parsed = None
                    for _dfmt in ("%d-%b-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                                  "%m/%d/%Y", "%d/%m/%Y"):
                        try:
                            _parsed = _dt_type.strptime(_sv, _dfmt)
                            break
                        except ValueError:
                            pass
                    if _parsed is None:
                        try:
                            import pandas as _pd
                            _ts = _pd.Timestamp(_sv)
                            if not _pd.isna(_ts):
                                _parsed = _ts.to_pydatetime().replace(tzinfo=None)
                        except Exception:
                            pass
                    if _parsed is not None:
                        # Write as Excel serial number (most direct approach)
                        _serial = (_parsed - _EXCEL_EPOCH).days
                        _dc.value = _serial
                        _dc.data_type = "n"
                        _dc.number_format = _EXCEL_DATE_FMT
                        _converted += 1
                    else:
                        _skipped += 1
            log.info(f"  '{sheet_name}': {_converted} date cell(s) converted, {_skipped} skipped")

            # 3. Convert numeric cells (string integers/floats -> actual numbers)
            _num_converted = 0
            _num_int_fmt   = "0"
            _num_float_fmt = "0.##"
            _hdr_row = list(next(_ws2.iter_rows(min_row=1, max_row=1), []))
            for _col_idx in range(1, len(_hdr_row) + 1):
                if _col_idx in _date_col_nums:
                    continue  # already handled as dates
                _hv = str(_hdr_row[_col_idx - 1].value or "").upper()
                if "FAILURE_MODE" in _hv:
                    continue  # keep as text
                for _row in _ws2.iter_rows(
                        min_row=2, min_col=_col_idx, max_col=_col_idx):
                    _dc = _row[0]
                    if not isinstance(_dc.value, str):
                        continue
                    _sv = _dc.value.strip()
                    if not _sv or _sv.lower() in ("none", "nat", "null"):
                        continue
                    try:
                        _dc.value = int(_sv)
                        _dc.number_format = _num_int_fmt
                        _num_converted += 1
                        continue
                    except ValueError:
                        pass
                    try:
                        _dc.value = float(_sv)
                        _dc.number_format = _num_float_fmt
                        _num_converted += 1
                    except ValueError:
                        pass
            log.info(f"  '{sheet_name}': {_num_converted} numeric cell(s) converted")

            # 4. Apply gradient fills
            gcm = gradient_map
            if gcm:
                log.info(f"  Applying {len(gcm):,} gradient fill(s) in '{sheet_name}' ...")
                for (xl_row, xl_col), bgs in gcm.items():
                    _cell = _ws2.cell(row=xl_row, column=xl_col)
                    n     = len(bgs)
                    _EPS  = 0.001
                    stops = []
                    for i, bg in enumerate(bgs):
                        argb  = "FF" + bg.lstrip("#").upper()
                        start = round(i / n + (_EPS if i > 0 else 0), 4)
                        end   = round((i + 1) / n - (_EPS if i < n - 1 else 0), 4)
                        stops.append(_Stop(color=argb, position=start))
                        stops.append(_Stop(color=argb, position=end))
                    _cell.fill = _GF(type="linear", degree=0, stop=stops)

        final_buf = io.BytesIO()
        _wb2.save(final_buf)
        final_buf.seek(0)
        file_bytes = final_buf.read()
        log.info("  Post-processing complete.")
    except Exception as exc:
        import traceback as _tb
        log.warning(f"  Post-processing FAILED: {exc}")
        log.warning(_tb.format_exc())
        buf.seek(0)
        file_bytes = buf.read()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(file_bytes)
    log.info(f"Quarterly report Excel saved → {output_path}  ({len(file_bytes) / 1024:.1f} KB)")



# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the quarterly DQ report."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).parent / "dq_config.json",
        help="Path to dq_config.json (default: same directory as this script)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent / "reports",
        help="Directory to save the report Excel file (default: ./reports/)",
    )
    parser.add_argument(
        "--skip-snowflake",
        action="store_true",
        help="Generate Excel only — skip all Snowflake writes (read operations still run)",
    )
    args = parser.parse_args()

    # ── Load config ──────────────────────────────────────────────────────────
    cfg     = load_config(args.config)
    bdc_cfg = cfg.get("business_defined_checks")
    if not bdc_cfg:
        log.error("'business_defined_checks' section not found in config.")
        sys.exit(1)

    database = bdc_cfg["database"]

    write_cfg = cfg.get("business_defined_checks_write", bdc_cfg)
    if "business_defined_checks_write" not in cfg:
        log.warning("No 'business_defined_checks_write' block found — using read credentials for writes.")

    # ── Timestamp ────────────────────────────────────────────────────────────
    now           = datetime.now(EASTERN)
    run_timestamp = now.strftime("%Y-%m-%d %H:%M:%S %Z")
    # File/table names are built after fetch_date_input() resolves the date range

    log.info("=" * 64)
    log.info("Quarterly DQ Report Generator")
    log.info(f"Run timestamp : {run_timestamp}")
    log.info("=" * 64)

    # ── Open connections ─────────────────────────────────────────────────────
    log.info("Opening Snowflake read connection ...")
    read_conn = open_snowflake_connection(bdc_cfg)
    log.info("  OK Read connection established")

    if args.skip_snowflake:
        write_conn = None
        def write_connection_factory(): return None
        log.info("  Skipping Snowflake write connection (--skip-snowflake)")
    else:
        log.info("Opening Snowflake write connection ...")
        write_conn = open_snowflake_connection(write_cfg)
        log.info("  OK Write connection established")
        def write_connection_factory():
            return open_snowflake_connection(write_cfg)

    try:
        # ── Resolve date range from DQ_DATE_INPUT ────────────────────────────
        from_date, to_date = fetch_date_input(read_conn, database)
        ts                   = now.strftime("%Y%m%d_%H%M%S")
        snapshot_name        = f"EMAIL_QUARTERLY_SNAPSHOT_{ts}"
        source_snapshot_name = f"EMAIL_QUARTERLY_SOURCE_SNAPSHOT_{ts}"
        report_name          = f"EMAIL_QUARTERLY_REPORT_{ts}"

        # File name: Quarterly_report_01_May_2025_10_00_00_AM_to_14_May_2025_02_30_22_PM.xlsx
        # Date portion from DQ_DATE_INPUT, time portion from the current run timestamp
        run_time_str    = now.strftime("%I_%M_%S_%p")          # e.g. 10_00_00_AM
        from_date_str   = from_date.strftime("%d_%b_%Y")       # e.g. 01_May_2025
        to_date_str     = to_date.strftime("%d_%b_%Y")         # e.g. 14_May_2025
        output_filename = f"Quarterly_report_{from_date_str}_to_{to_date_str}.xlsx"

        log.info(f"  Snapshot table        : {database}.DQ_CORE.{snapshot_name}")
        log.info(f"  Source snapshot table : {database}.DQ_CORE.{source_snapshot_name}")
        log.info(f"  Report table          : {database}.DQ_CORE.{report_name}")
        log.info(f"  Output file           : {output_filename}")

        # ── Step 1: rules ────────────────────────────────────────────────────
        log.info("-- Step 1: Fetching rules (TARGET_TABLE IS NOT NULL) --")
        rules, rule_color_map, target_table_map = fetch_active_rules(read_conn, bdc_cfg)
        if not rules:
            log.error("No active rules found -- aborting.")
            sys.exit(1)

        # ── Step 2: failed rows ──────────────────────────────────────────────
        log.info("-- Step 2: Fetching failed rows from rule tables per rule --")
        rule_dfs, rule_row_counts = fetch_rule_dataframes(read_conn, rules, database)

        # ── Step 3: full source dataset ──────────────────────────────────────
        log.info("-- Step 3: Fetching full source dataset from TARGET_TABLE source tables --")
        source_df = fetch_source_snapshot(read_conn, target_table_map, database)
        if source_df.empty:
            log.error("Source dataset is empty — cannot build result dataframe. Aborting.")
            sys.exit(1)

        # ── Step 4: tag all source rows with FAILURE_MODE ────────────────────
        log.info("-- Step 4: Tagging failed rows with FAILURE_MODE --")
        result_df, total_rows = build_result_dataframe(rule_dfs, source_df)
        if result_df.empty:
            log.error("Result dataframe is empty — aborting.")
            sys.exit(1)

        failed_count = int((result_df["FAILURE_MODE"].str.strip() != "").sum())
        log.info(
            f"  {len(result_df):,} total rows  "
            f"({failed_count:,} failed, {len(result_df) - failed_count:,} non-failed)"
        )

        # ── Step 5: Persist to Snowflake ────────────────────────────────────
        if args.skip_snowflake:
            log.info("-- Step 5: Skipped (--skip-snowflake) --")
        else:
            failed_df = (
                result_df[result_df["FAILURE_MODE"].str.strip() != ""]
                .reset_index(drop=True)
            )

            # 5a — Snapshot: failed rows only
            log.info("-- Step 5a: Persisting failed-rows snapshot --")
            write_source_snapshot_to_snowflake(
                write_conn               = write_conn,
                database                 = database,
                snapshot_name            = snapshot_name,
                source_df                = failed_df,
                write_connection_factory = write_connection_factory,
            )
            log.info(f"  ✅ Snapshot table written: {database}.DQ_CORE.{snapshot_name}")

            # 5b — Source snapshot: raw source rows, no FAILURE_MODE
            log.info("-- Step 5b: Persisting full source snapshot --")
            write_source_snapshot_to_snowflake(
                write_conn               = write_conn,
                database                 = database,
                snapshot_name            = source_snapshot_name,
                source_df                = source_df,
                write_connection_factory = write_connection_factory,
            )
            log.info(f"  ✅ Source snapshot table written: {database}.DQ_CORE.{source_snapshot_name}")

            # 5c — Report: all rows (same as source snapshot, persisted under report name)
            log.info("-- Step 5c: Persisting quarterly report table --")
            write_source_snapshot_to_snowflake(
                write_conn               = write_conn,
                database                 = database,
                snapshot_name            = report_name,
                source_df                = result_df,
                write_connection_factory = write_connection_factory,
            )
            log.info(f"  ✅ Report table written: {database}.DQ_CORE.{report_name}")

        # ── Step 6: Build Excel report ───────────────────────────────────────
        log.info("-- Step 6: Building quarterly Excel report --")
        output_path = args.output_dir / output_filename
        build_report_excel(
            result_df      = result_df,
            from_date      = from_date,
            to_date        = to_date,
            rules          = rules,
            rule_color_map = rule_color_map,
            output_path    = output_path,
        )

        # ── Step 7: Write registry row ───────────────────────────────────────
        if args.skip_snowflake:
            log.info("-- Step 7: Skipped (--skip-snowflake) --")
        else:
            log.info("-- Step 7: Writing run record to consolidated report registry --")
            registry_table = bdc_cfg.get("registry_table", "CONSOLIDATED_REPORT_REGISTRY")
            write_registry_row_to_snowflake(
                write_conn      = write_conn,
                database        = database,
                report_name     = report_name,
                run_timestamp   = run_timestamp,
                result_df       = result_df,
                source_df       = source_df,
                rules           = rules,
                rule_color_map  = rule_color_map,
                rule_row_counts = rule_row_counts,
                registry_table  = registry_table,
            )

        log.info("=" * 64)
        log.info("OK  Report complete!")
        log.info(f"    Excel report          : {output_path}")
        log.info(f"    Total rows            : {len(result_df):,}")
        log.info(f"    Failed rows           : {failed_count:,}")
        log.info(f"    Non-failed rows       : {len(result_df) - failed_count:,}")
        if not args.skip_snowflake:
            log.info(f"    Snapshot table        : {database}.DQ_CORE.{snapshot_name}  (failed rows only)")
            log.info(f"    Source snapshot table : {database}.DQ_CORE.{source_snapshot_name}  (raw source, no FAILURE_MODE)")
            log.info(f"    Report table          : {database}.DQ_CORE.{report_name}  (all rows)")
            log.info(f"    Registry table        : {database}.DQ_CORE.{registry_table}  (run record appended)")
        log.info("=" * 64)

    finally:
        read_conn.close()
        if write_conn is not None:
            write_conn.close()


if __name__ == "__main__":
    main()