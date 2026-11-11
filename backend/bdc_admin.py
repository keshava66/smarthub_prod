import asyncio
import logging
import math
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List
import pytz

EASTERN = pytz.timezone('America/New_York')
from typing import Optional
import re as _re
import time as _time

from fastapi import APIRouter, Body, HTTPException, Query, Header, Depends

from auth import require_auth, require_permission, bdc_datasets, dataset_sql, sql_literal, dataset_key
import db
from consolidated_report import _rule_table_name

# ─── Router ───────────────────────────────────────────────────────────────────
admin_router = APIRouter(prefix="/api/admin", tags=["Admin"])

# ─── Injected BDC dependencies (set once at startup by Backend.py) ────────────
_bdc_config_manager = None
_bdc_connection_manager = None


def set_bdc_dependencies(config_manager, connection_manager):
    global _bdc_config_manager, _bdc_connection_manager
    _bdc_config_manager = config_manager
    _bdc_connection_manager = connection_manager
    _cache.clear()


# Delta-mode review decisions share DQ_REVIEW_REGISTRY with quarterly ones,
# told apart by this prefix on RULE_ID (see review.py).
DELTA_PREFIX = "DELTA_"


def review_mode_sql(delta_mode: bool, column: str = "RULE_ID") -> str:
    """WHERE fragment picking one cadence's rows out of DQ_REVIEW_REGISTRY.
    STARTSWITH, not LIKE 'DELTA_%': in LIKE the underscore is a wildcard, so
    'DELTA_%' also matched ids like 'DELTAS...' and filed them under the wrong
    cadence. No '%' either, so it is safe inside parameterised statements."""
    test = f"STARTSWITH({column}, '{DELTA_PREFIX}')"
    return test if delta_mode else f"NOT {test}"


def _require_bdc():
    """Raise 503 if the BDC service is unavailable."""
    if not _bdc_connection_manager or not _bdc_config_manager:
        raise HTTPException(status_code=503, detail="BDC service is not available")


class _WriteConnectionManager:
    """
    Lightweight single-connection pool for write operations.
    Uses business_defined_checks_write config if present, otherwise falls back
    to the standard business_defined_checks read config.
    Refreshes automatically after 2 hours (same TTL as the read manager).
    """
    CONNECTION_TTL = 2 * 60 * 60  # 2 hours

    def __init__(self):
        self._conn = None
        self._created_at = None
        self._lock = threading.Lock()

    def _is_expired(self) -> bool:
        return self._created_at is None or (_time.time() - self._created_at) > self.CONNECTION_TTL

    def _build_conn(self, sf_config: dict):
        """Create a raw Snowflake connection from a config dict."""
        from cryptography.hazmat.primitives import serialization as _ser
        from pathlib import Path as _Path
        import snowflake.connector as _sf

        pk_path = sf_config["private_key_path"]
        pk_password = sf_config.get("private_key_password", "")

        if not _Path(pk_path).exists():
            raise FileNotFoundError(f"Write private key not found: {pk_path}")

        with open(pk_path, "rb") as f:
            key = _ser.load_pem_private_key(
                f.read(),
                password=pk_password.encode() if pk_password else None,
            )
        pk_bytes = key.private_bytes(
            encoding=_ser.Encoding.DER,
            format=_ser.PrivateFormat.PKCS8,
            encryption_algorithm=_ser.NoEncryption(),
        )

        return _sf.connect(
            user=sf_config["user"],
            account=sf_config["account"],
            private_key=pk_bytes,
            role=sf_config["role"],
            warehouse=sf_config["warehouse"],
            database=sf_config["database"],
            schema=sf_config.get("schema", "PUBLIC"),
        )

    def get_cursor(self):
        """
        Return a raw cursor for write operations.

        Lookup order:
          1. business_defined_checks_write config  → dedicated write account
          2. business_defined_checks config        → fallback to the read account (logs a warning)
        """
        if not _bdc_config_manager:
            raise HTTPException(status_code=503, detail="BDC config not available")

        main_cfg = _bdc_config_manager.config or {}
        if "business_defined_checks_write" in main_cfg:
            sf_config = main_cfg["business_defined_checks_write"]
        else:
            import logging as _log
            _log.getLogger("bdc_admin").warning(
                "No business_defined_checks_write config found — falling back to read "
                "connection for write ops. Add a 'business_defined_checks_write' block "
                "to dq_config.json for dedicated write credentials."
            )
            sf_config = _bdc_config_manager.get_snowflake_config()

        with self._lock:
            if self._conn is None or self._is_expired():
                try:
                    if self._conn is not None:
                        try:
                            self._conn.close()
                        except:
                            pass
                    self._conn = self._build_conn(sf_config)
                    self._created_at = _time.time()
                    import logging as _log2
                    _log2.getLogger("bdc_admin").info("✅ Write connection established")
                except Exception as e:
                    raise HTTPException(
                        status_code=503,
                        detail=f"Failed to open write connection: {type(e).__name__}: {e}"
                    )
            try:
                cur = self._conn.cursor()
                cur.execute("SELECT 1")
                cur.close()
            except Exception:
                try:
                    self._conn = self._build_conn(sf_config)
                    self._created_at = _time.time()
                except Exception as e:
                    raise HTTPException(
                        status_code=503,
                        detail=f"Write connection lost and could not reconnect: {e}"
                    )

            return self._conn.cursor()

    def close_all(self):
        if self._conn:
            try:
                self._conn.close()
            except:
                pass
        self._conn = None
        self._created_at = None


_write_mgr = _WriteConnectionManager()


def _raw_write_cursor():
    _require_bdc()
    return _write_mgr.get_cursor()


# ═══════════════════════════════════════════════════════════════════════════════
# Dataset access (who sees which DATASET_TITLE)
# ═══════════════════════════════════════════════════════════════════════════════
_RULE_DS_TTL = 60.0
_rule_ds: Dict[str, object] = {"map": None, "at": 0.0}
_rule_ds_lock = threading.Lock()


def rule_dataset_map(fresh: bool = False) -> Dict[str, str]:
    """RULE_ID -> DATASET_TITLE from DQ_RULE_INPUT, active row first. Cached a
    minute; fresh=True reads it now (rules can be added straight in Snowflake)."""
    with _rule_ds_lock:
        if not fresh and _rule_ds["map"] is not None and _time.monotonic() - _rule_ds["at"] < _RULE_DS_TTL:
            return _rule_ds["map"]
    _require_bdc()
    table = _bdc_config_manager.get_full_table_name(_bdc_config_manager.get_dq_config()["rule_input_table"])
    rows = _bdc_connection_manager.get_client().execute(f"""
        SELECT RULE_ID, DATASET_TITLE FROM {table}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY RULE_ID ORDER BY IS_ACTIVE DESC) = 1
    """).rows
    m = {r[0]: dataset_key(r[1]) for r in rows}
    with _rule_ds_lock:
        _rule_ds["map"], _rule_ds["at"] = m, _time.monotonic()
    return m


def list_datasets() -> List[str]:
    return sorted(set(rule_dataset_map(fresh=True).values()))


def allowed_rule_ids(auth: dict) -> Optional[set]:
    """None = every rule."""
    allowed = bdc_datasets(auth)
    if allowed is None:
        return None
    return {rid for rid, ds in rule_dataset_map().items() if ds in allowed}


def rule_allowed(auth: dict, rule_id: str) -> bool:
    allowed = bdc_datasets(auth)
    if allowed is None:
        return True
    def look(m):
        ds = m.get(rule_id)
        if ds is None and rule_id.startswith("DELTA_"):  # review registry keys delta rules this way
            ds = m.get(rule_id[6:])
        return ds
    ds = look(rule_dataset_map())
    if ds is None:  # added since the list was cached
        ds = look(rule_dataset_map(fresh=True))
    return ds in allowed


def check_dataset(auth: dict, title) -> None:
    """403 unless the caller may see this DATASET_TITLE."""
    allowed = bdc_datasets(auth)
    if allowed is not None and dataset_key(title) not in allowed:
        raise HTTPException(status_code=403, detail=f"You don't have access to the dataset '{dataset_key(title)}'")


def _dataset_of(row: dict):
    return next((v for k, v in (row or {}).items() if str(k).upper() == "DATASET_TITLE"), None)


def forget_rule_datasets() -> None:
    """After a rule-input write, so new or moved rules show up right away."""
    with _rule_ds_lock:
        _rule_ds["map"] = None


def require_bdc_rule_download(rule_id: str, auth: dict = Depends(require_permission("downloadBDCData"))) -> dict:
    """Per-rule Excel downloads: the download permission plus the rule's dataset."""
    if not rule_allowed(auth, rule_id):
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return auth


def require_bdc_rule(rule_id: str, auth: dict = Depends(require_permission("viewBusinessChecks"))) -> dict:
    """Per-rule endpoints: 404 for a rule outside the caller's datasets."""
    if not rule_allowed(auth, rule_id):
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")
    return auth



# ═══════════════════════════════════════════════════════════════════════════════
# DQ Table Full Viewer
# ═══════════════════════════════════════════════════════════════════════════════
_DQ_TABLE_KEY_MAP = {
    "rule-input":    "rule_input_table",
    "rule-registry": "rule_registry_table",
    # "date-input" is intentionally absent — resolved by _date_input_table_name()
    # which always uses "date_input_table" + default db/schema prefix (CRUD only).
    # The separate "date_range_table_fqn" table is handled by _date_range_table_name()
    # and is only used by the active-ranges banner endpoint.
}


@admin_router.get("/dq-tables/{table_type}")
def get_dq_table_full(
        table_type: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=200),
        auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    _require_bdc()

    sf = _bdc_connection_manager.get_client()
    dq_config = _bdc_config_manager.get_dq_config()

    # date-input uses its own resolver (date_input_table, the table the admin forms write)
    if table_type == "date-input":
        table_name = _date_input_table_name()
    else:
        table_key = _DQ_TABLE_KEY_MAP.get(table_type)
        if not table_key:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown table_type '{table_type}'. Valid values: {sorted(list(_DQ_TABLE_KEY_MAP) + ['date-input'])}"
            )
        if table_key not in dq_config:
            raise HTTPException(
                status_code=404,
                detail=f"'{table_key}' is not configured."
            )
        table_name = _bdc_config_manager.get_full_table_name(dq_config[table_key])
    offset = (page - 1) * page_size

    # Rule tables only show the caller's datasets
    where = ""
    if table_type == "rule-input":
        where = dataset_sql(auth, "DATASET_TITLE")
    elif table_type == "rule-registry":
        ids = allowed_rule_ids(auth)
        if ids is not None:
            where = f" AND RULE_ID IN ({', '.join(sql_literal(r) for r in sorted(ids))})" if ids else " AND 1 = 0"
    where = f"WHERE 1 = 1{where}" if where else ""

    count_result = sf.execute(f"SELECT COUNT(*) FROM {table_name} {where}")
    total_count = int(count_result.rows[0][0]) if count_result.rows else 0

    # Every type needs a total order: LIMIT/OFFSET over an unordered result can
    # repeat or skip rows between pages. Also keeps rows put after IS_ACTIVE updates.
    order_clause = {
        "date-input": "ORDER BY REPORT_SET, FROM_DATE, ID",
    }.get(table_type, "ORDER BY RULE_ID")

    result = sf.execute(f"SELECT * FROM {table_name} {where} {order_clause} LIMIT {page_size} OFFSET {offset}")

    return {
        "table_name": table_name,
        "columns": result.columns,
        "rows": [
            [str(cell) if cell is not None else None for cell in row]
            for row in result.rows
        ],
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, math.ceil(total_count / page_size)),
    }



@admin_router.get("/dq-tables/{table_type}/row")
def get_dq_table_row(
        table_type: str,
        key: str = Query(..., min_length=1, max_length=300, description="RULE_ID for rule-input, ID for date-input"),
        auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    """One row by its key, so an edit form can be opened from a link without
    paging through the table to find it."""
    _require_bdc()
    if table_type == "rule-input":
        dq_config = _bdc_config_manager.get_dq_config()
        if "rule_input_table" not in dq_config:
            raise HTTPException(status_code=404, detail="rule_input_table not configured.")
        table_name, key_col = _bdc_config_manager.get_full_table_name(dq_config["rule_input_table"]), "RULE_ID"
    elif table_type == "date-input":
        table_name, key_col = _date_input_table_name(), "ID"
    else:
        raise HTTPException(status_code=400, detail="table_type must be 'rule-input' or 'date-input'")

    sf = _bdc_connection_manager.get_client()
    result = sf.execute(f"SELECT * FROM {table_name} WHERE {key_col} = %(key)s LIMIT 1", {"key": key})
    if not result.rows:
        raise HTTPException(status_code=404, detail=f"No row with {key_col} '{key}'")
    if table_type == "rule-input":
        check_dataset(auth, _dataset_of(dict(zip(result.columns, result.rows[0]))))
    return {
        "table_name": table_name,
        "columns": result.columns,
        "row": [str(cell) if cell is not None else None for cell in result.rows[0]],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# DQ Rule Input Table — Row Edit
# ═══════════════════════════════════════════════════════════════════════════════

@admin_router.get("/dq-tables/rule-input/schema")
def get_rule_input_schema(
        auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    """
    Return column names + nullability for the rule input table so the
    frontend can render a properly typed edit form.
    """
    _require_bdc()

    sf = _bdc_connection_manager.get_client()
    dq_config = _bdc_config_manager.get_dq_config()

    if "rule_input_table" not in dq_config:
        raise HTTPException(status_code=404, detail="rule_input_table not configured.")

    table_name = _bdc_config_manager.get_full_table_name(dq_config["rule_input_table"])

    result = sf.execute(f"SELECT * FROM {table_name} LIMIT 1")

    return {
        "table_name": table_name,
        "columns": result.columns,
    }




@admin_router.get("/dq-tables/rule-input/datasets")
def get_rule_input_datasets(auth: dict = Depends(require_permission("viewBDCSuperuser"))):
    """DATASET_TITLEs a rule can be saved under by this caller: the ones in use,
    cut to their datasets. any_allowed says whether a new title would be accepted."""
    _require_bdc()
    allowed = bdc_datasets(auth)
    names = [d for d in list_datasets() if d != dataset_key(None) and (allowed is None or d in allowed)]
    return {"datasets": names, "any_allowed": allowed is None}


def _parse_version(rule_id: str):
    # Split RULE_ID into (base, version_number)
    # 'BDC_001'    -> ('BDC_001', 0)
    # 'BDC_001_v1' -> ('BDC_001', 1)
    m = _re.match(r'^(.+?)_v(\d+)$', rule_id, _re.IGNORECASE)
    if m:
        return m.group(1), int(m.group(2))
    return rule_id, 0


def _raw_cursor():
    return _bdc_connection_manager.get_client().conn.cursor()


def _versions_of(table_name: str, rule_id: str) -> List[tuple]:
    """(RULE_ID, IS_ACTIVE) for every version of rule_id's base. The prefix
    match is only a coarse filter ('BDC_001' also prefixes 'BDC_0010_v2', and
    '_' is a LIKE wildcard), so the base is compared exactly after parsing."""
    base, _ = _parse_version(rule_id)
    cur = _raw_cursor()
    try:
        cur.execute(
            f"SELECT RULE_ID, IS_ACTIVE FROM {table_name} WHERE STARTSWITH(UPPER(RULE_ID), %s)",
            (base.upper(),)
        )
        rows = cur.fetchall()
    finally:
        cur.close()
    return [(str(rid), act) for rid, act in rows if _parse_version(str(rid))[0].upper() == base.upper()]


def _next_version_id(table_name: str, rule_id: str) -> str:
    base, _ = _parse_version(rule_id)
    max_ver = max((_parse_version(rid)[1] for rid, _ in _versions_of(table_name, rule_id)), default=0)
    return f"{base}_v{max_ver + 1}"


def _safe_val(v):
    # Convert Snowflake-returned Python types to plain scalars safe for re-insertion
    if v is None:
        return None
    try:
        from decimal import Decimal
        if isinstance(v, Decimal):
            return float(v)
    except ImportError:
        pass
    if hasattr(v, 'isoformat'):
        return v.isoformat()
    return v


@admin_router.get("/dq-tables/rule-input/next-version")
def get_next_version_id(
        rule_id: str = Query(..., description="Current RULE_ID to compute the next version for"),
        auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    """Preview the next versioned RULE_ID without writing anything."""
    _require_bdc()

    dq_config = _bdc_config_manager.get_dq_config()
    if "rule_input_table" not in dq_config:
        raise HTTPException(status_code=404, detail="rule_input_table not configured.")

    table_name = _bdc_config_manager.get_full_table_name(dq_config["rule_input_table"])
    base, _ = _parse_version(rule_id)
    next_ver_id = _next_version_id(table_name, rule_id)

    return {
        "original_rule_id": rule_id,
        "base_rule_id": base,
        "next_version_id": next_ver_id,
    }


@admin_router.put("/dq-tables/rule-input/row")
def update_rule_input_row(

        payload: dict = Body(...),
        auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    _require_bdc()

    pk_col = str(payload.get("primary_key_col") or "").strip()
    pk_val = payload.get("primary_key_val")
    updates = payload.get("updates") or {}

    # The key column used to go into the SQL as sent; only RULE_ID is valid here.
    if pk_col.upper() != "RULE_ID":
        raise HTTPException(status_code=400, detail="primary_key_col must be RULE_ID")
    if not isinstance(pk_val, str) or not pk_val.strip():
        raise HTTPException(status_code=400, detail="primary_key_val is required")
    if not isinstance(updates, dict) or not updates:
        raise HTTPException(status_code=400, detail="updates dict is empty — nothing to save")

    dq_config = _bdc_config_manager.get_dq_config()
    if "rule_input_table" not in dq_config:
        raise HTTPException(status_code=404, detail="rule_input_table not configured.")

    table_name = _bdc_config_manager.get_full_table_name(dq_config["rule_input_table"])

    # Fetch original row via raw cursor
    cur = _raw_cursor()
    try:
        cur.execute(f"SELECT * FROM {table_name} WHERE RULE_ID = %s", (pk_val,))
        row = cur.fetchone()
        col_names = [d[0] for d in cur.description] if cur.description else []
    finally:
        cur.close()

    if not row:
        raise HTTPException(status_code=404, detail=f"Row with RULE_ID = '{pk_val}' not found.")

    pk_col = next(c for c in col_names if c.upper() == "RULE_ID")
    original_row = {col: _safe_val(val) for col, val in zip(col_names, row)}
    check_dataset(auth, _dataset_of(original_row))
    if any(str(k).upper() == "DATASET_TITLE" for k in updates):
        check_dataset(auth, _dataset_of(updates) or None)
    by_upper = {c.upper(): c for c in col_names}
    unknown = [k for k in updates if str(k).upper() not in by_upper]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown column(s): {', '.join(map(str, unknown))}")
    is_active_col = by_upper.get("IS_ACTIVE")
    created_ts_col = next((c for c in col_names if c.upper() in ("CREATED_TS", "CREATED_AT")), None)
    updated_ts_col = next((c for c in col_names if c.upper() in ("UPDATED_TS", "UPDATED_AT")), None)

    versions = _versions_of(table_name, pk_val)
    next_ver_id = f"{_parse_version(pk_val)[0]}_v{max((_parse_version(v)[1] for v, _ in versions), default=0) + 1}"
    # Editing an older version must not leave the newer one active next to the new row.
    retire = sorted({pk_val} | {v for v, act in versions if act is True or str(act).upper() == "TRUE"})
    now_ts = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S.%f")

    new_row = dict(original_row)
    for col, val in updates.items():
        real = by_upper[str(col).upper()]
        if real == pk_col:
            continue
        new_row[real] = None if val == "" else val
    new_row[pk_col] = next_ver_id
    if is_active_col:
        new_row[is_active_col] = True
    # Timestamps: carry original CREATED_TS, set UPDATED_TS to now
    if created_ts_col:
        new_row[created_ts_col] = original_row.get(created_ts_col) or now_ts
    if updated_ts_col:
        new_row[updated_ts_col] = now_ts

    insert_cols = list(col_names)
    placeholders = ", ".join(["%s"] * len(insert_cols))
    retire_in = ", ".join(["%s"] * len(retire))
    cur2 = _raw_write_cursor()
    try:
        # Insert the new version first, then retire the old ones. The other
        # order lost the rule entirely whenever the insert failed after the
        # deactivate had gone through.
        try:
            cur2.execute(
                f"INSERT INTO {table_name} ({', '.join(insert_cols)}) VALUES ({placeholders})",
                tuple(new_row.get(c) for c in insert_cols),
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Snowflake versioning failed: {type(e).__name__}: {e}")
        if is_active_col:
            try:
                stamp = f", {updated_ts_col} = %s" if updated_ts_col else ""
                cur2.execute(
                    f"UPDATE {table_name} SET {is_active_col} = FALSE{stamp} WHERE RULE_ID IN ({retire_in})",
                    ((now_ts,) if updated_ts_col else ()) + tuple(retire),
                )
            except Exception as e:
                # Put things back as they were rather than leave two active versions.
                try:
                    cur2.execute(f"DELETE FROM {table_name} WHERE RULE_ID = %s", (next_ver_id,))
                except Exception as e2:
                    logger.error(f"Could not roll back {next_ver_id} after a failed deactivate: {e2}")
                raise HTTPException(status_code=500, detail=f"Snowflake versioning failed: {type(e).__name__}: {e}")
    finally:
        cur2.close()

    forget_rule_datasets()
    invalidate_summary()
    return {
        "success": True,
        "original_rule_id": pk_val,
        "new_rule_id": next_ver_id,
        "message": f"'{pk_val}' deactivated. New row '{next_ver_id}' inserted with IS_ACTIVE = TRUE.",
        "updated_by": auth["user"]["email"],
    }


@admin_router.patch("/dq-tables/rule-input/toggle-active")
def toggle_rule_active(

        payload: dict = Body(...),
        auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    _require_bdc()

    rule_id = str(payload.get("rule_id") or "").strip()
    is_active = payload.get("is_active")

    if not rule_id:
        raise HTTPException(status_code=400, detail="rule_id is required")
    if not isinstance(is_active, bool):
        raise HTTPException(status_code=400, detail="is_active (true/false) is required")
    if not rule_allowed(auth, rule_id):
        raise HTTPException(status_code=404, detail=f"Rule '{rule_id}' not found")

    dq_config = _bdc_config_manager.get_dq_config()
    if "rule_input_table" not in dq_config:
        raise HTTPException(status_code=404, detail="rule_input_table not configured.")

    table_name = _bdc_config_manager.get_full_table_name(dq_config["rule_input_table"])
    now_ts = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S.%f")

    # Detect IS_ACTIVE and UPDATED_TS column names
    schema_cur = _raw_cursor()
    try:
        schema_cur.execute(f"SELECT * FROM {table_name} LIMIT 0")
        col_names = [d[0] for d in schema_cur.description] if schema_cur.description else []
    finally:
        schema_cur.close()

    is_active_col = next((c for c in col_names if c.upper() == "IS_ACTIVE"), None)
    updated_ts_col = next((c for c in col_names if c.upper() in ("UPDATED_TS", "UPDATED_AT")), None)

    if not is_active_col:
        raise HTTPException(status_code=404, detail="IS_ACTIVE column not found in the rule input table.")

    # One active version per rule, same as saving an edit: switching an older
    # version back on retires whichever version of that rule is active now.
    retire = []
    if is_active:
        retire = sorted(v for v, act in _versions_of(table_name, rule_id)
                        if v != rule_id and (act is True or str(act).upper() == "TRUE"))
    stamp = f", {updated_ts_col} = %s" if updated_ts_col else ""
    stamp_val = (now_ts,) if updated_ts_col else ()

    try:
        cur = _raw_write_cursor()
        try:
            cur.execute(
                f"UPDATE {table_name} SET {is_active_col} = %s{stamp} WHERE RULE_ID = %s",
                (bool(is_active),) + stamp_val + (rule_id,)
            )
            if retire:
                try:
                    cur.execute(
                        f"UPDATE {table_name} SET {is_active_col} = FALSE{stamp} "
                        f"WHERE RULE_ID IN ({', '.join(['%s'] * len(retire))})",
                        stamp_val + tuple(retire)
                    )
                except Exception:
                    # Put the target back rather than leave two active versions.
                    try:
                        cur.execute(f"UPDATE {table_name} SET {is_active_col} = FALSE WHERE RULE_ID = %s", (rule_id,))
                    except Exception as e2:
                        logger.error(f"Could not undo activating {rule_id} after a failed retire: {e2}")
                    raise
        finally:
            cur.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Toggle failed: {type(e).__name__}: {e}")

    forget_rule_datasets()
    invalidate_summary()
    return {
        "success": True,
        "rule_id": rule_id,
        "is_active": bool(is_active),
        "retired": retire,
        "updated_by": auth["user"]["email"],
    }


@admin_router.post("/dq-tables/rule-input/row")
def create_rule_input_row(

        payload: dict = Body(...),
        auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    _require_bdc()

    row_data = payload.get("row") or {}
    if not isinstance(row_data, dict) or not row_data:
        raise HTTPException(status_code=400, detail="row dict is required and cannot be empty")
    check_dataset(auth, _dataset_of(row_data))

    dq_config = _bdc_config_manager.get_dq_config()
    if "rule_input_table" not in dq_config:
        raise HTTPException(status_code=404, detail="rule_input_table not configured.")

    table_name = _bdc_config_manager.get_full_table_name(dq_config["rule_input_table"])

    # Validate RULE_ID is provided and not already taken
    pk_val = str(row_data.get("RULE_ID") or "").strip()
    if not pk_val:
        raise HTTPException(status_code=400, detail="RULE_ID is required")
    # RULE_ID ends up in results table names, URLs and the review registry key.
    if not _re.fullmatch(r"[A-Za-z0-9_]{1,190}", pk_val):
        raise HTTPException(status_code=400, detail="RULE_ID may only use letters, digits and underscores")
    if pk_val.upper().startswith(DELTA_PREFIX):
        raise HTTPException(status_code=400, detail=f"RULE_ID can't start with {DELTA_PREFIX}: that prefix marks "
                                                    "Semi-Monthly review decisions. Reword the start of the title.")

    taken = _versions_of(table_name, pk_val)
    if taken:
        raise HTTPException(
            status_code=409,
            detail=f"A rule with this ID already exists ({', '.join(sorted(v for v, _ in taken))}). "
                   "Edit that rule to make a new version, or choose a different title."
        )

    # Fetch column list from the table to know all columns and their order
    cur_schema = _raw_cursor()
    try:
        cur_schema.execute(f"SELECT * FROM {table_name} LIMIT 0")
        col_names = [d[0] for d in cur_schema.description] if cur_schema.description else []
    finally:
        cur_schema.close()

    now_ts = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S.%f")

    # Build insert values for every column we know about
    # Caller-supplied values take priority; server fills timestamps and IS_ACTIVE.
    insert_cols = []
    insert_vals = []

    for col in col_names:
        cu = col.upper()
        if cu in ("CREATED_TS", "CREATED_AT", "UPDATED_TS", "UPDATED_AT"):
            insert_cols.append(col)
            insert_vals.append(now_ts)
        elif cu == "IS_ACTIVE":
            insert_cols.append(col)
            insert_vals.append(True)
        elif col in row_data:
            val = row_data[col]
            insert_cols.append(col)
            insert_vals.append(None if val == "" else val)
        # columns not supplied by caller are omitted (will be NULL or DB default)

    # Caller-supplied keys never become column names; no table columns, no insert.
    if not col_names:
        raise HTTPException(status_code=500, detail=f"Could not read the columns of {table_name}")

    placeholders = ", ".join(["%s"] * len(insert_cols))
    col_list = ", ".join(insert_cols)

    try:
        cur2 = _raw_write_cursor()
        try:
            cur2.execute(
                f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})",
                tuple(insert_vals)
            )
        finally:
            cur2.close()
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Snowflake insert failed: {type(e).__name__}: {e}"
        )

    forget_rule_datasets()
    invalidate_summary()
    return {
        "success": True,
        "rule_id": pk_val,
        "message": f"New rule '{pk_val}' created in {table_name} with IS_ACTIVE = TRUE.",
        "created_by": auth["user"]["email"],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# DQ Date Input Table — Row CRUD
# Identified by the ID (UUID) column — unique per row.
# CREATED_DATE / UPDATED_DATE are always server-managed.
# ═══════════════════════════════════════════════════════════════════════════════

def _date_input_table_name() -> str:
    """
    Return the fully-qualified name of the admin date-input table.
    Used exclusively by the CRUD endpoints (create / update / toggle-active).
    Always resolves via 'date_input_table' (short name) + the default db/schema prefix.
    Never touches 'date_range_table_fqn'.
    """
    dq_config = _bdc_config_manager.get_dq_config()
    if "date_input_table" not in dq_config:
        raise HTTPException(
            status_code=404,
            detail="'date_input_table' is not configured in dq_config.",
        )
    return _bdc_config_manager.get_full_table_name(dq_config["date_input_table"])


def _date_range_table_name() -> str:
    """
    Return the fully-qualified name of the separate date-range table used
    exclusively by the active-ranges banner endpoint.
    Always resolves via 'date_range_table_fqn' — a fully-qualified name stored
    verbatim in dq_config (e.g. "PRD_DB.SCHEMA.DQ_DATE_INPUT__L1__DQ_CORE").
    Never touches 'date_input_table'.
    """
    dq_config = _bdc_config_manager.get_dq_config()
    fqn = (dq_config.get("date_range_table_fqn") or "").strip()
    if not fqn:
        raise HTTPException(
            status_code=404,
            detail=(
                "'date_range_table_fqn' is not configured in dq_config. "
                "Set it to the fully-qualified name of the date-range table "
                "(e.g. 'MY_DB.MY_SCHEMA.DQ_DATE_INPUT__L1__DQ_CORE')."
            ),
        )
    return fqn


@admin_router.post("/dq-tables/date-input/row")
def create_date_input_row(
    payload: dict = Body(...),
    auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    _require_bdc()
    row = payload.get("row") or {}
    if not isinstance(row, dict) or not row:
        raise HTTPException(status_code=400, detail="row dict is required.")
    row = {str(k).upper(): v for k, v in row.items()}

    report_set = str(row.get("REPORT_SET") or "").strip()
    if not report_set:
        raise HTTPException(status_code=400, detail="REPORT_SET is required.")
    from_date, to_date = _check_range(row.get("FROM_DATE"), row.get("TO_DATE"), require_to="TO_DATE" in row)
    row.update(REPORT_SET=report_set, FROM_DATE=from_date)
    if to_date:
        row["TO_DATE"] = to_date
    # The ID is made here; a client-made one needed crypto.randomUUID, which
    # browsers only offer on https.
    row["ID"] = str(row.get("ID") or "").strip() or str(uuid.uuid4())
    active = _as_bool(row.get("IS_ACTIVE"), default=True)

    table_name = _date_input_table_name()
    now_ts = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S.%f")
    col_names, is_active_col, updated_ts_col = _date_columns(table_name)

    insert_cols, insert_vals = [], []
    for col in col_names:
        cu = col.upper()
        if cu in ("CREATED_DATE", "UPDATED_DATE", "CREATED_TS", "UPDATED_TS", "CREATED_AT", "UPDATED_AT"):
            insert_cols.append(col); insert_vals.append(now_ts)
        elif cu == "IS_ACTIVE":
            insert_cols.append(col); insert_vals.append(active)
        elif cu in row:
            val = row[cu]
            insert_cols.append(col); insert_vals.append(None if val == "" else val)

    placeholders = ", ".join(["%s"] * len(insert_cols))
    col_list     = ", ".join(insert_cols)
    try:
        cur2 = _raw_write_cursor()
        try:
            cur2.execute(f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})", tuple(insert_vals))
            # One active range per report set, same as the toggle.
            if active and is_active_col:
                _retire_other_ranges(cur2, table_name, report_set, row["ID"], now_ts, is_active_col, updated_ts_col)
        finally:
            cur2.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Insert failed: {type(e).__name__}: {e}")

    invalidate_summary()
    return {"success": True, "id": row["ID"], "report_set": report_set, "from_date": from_date,
            "created_by": auth["user"]["email"]}


def _as_bool(v, default: bool = False) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "y", "yes")


def _check_range(from_raw, to_raw, require_to: bool = True) -> tuple:
    """('YYYY-MM-DD', 'YYYY-MM-DD' or None), or 400. Dates must parse and run forwards."""
    def day(v, name):
        s = str(v or "").strip()[:10]
        if not s:
            return None
        try:
            return date.fromisoformat(s)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{name} must be a date (YYYY-MM-DD).")
    f, t = day(from_raw, "FROM_DATE"), day(to_raw, "TO_DATE")
    if not f:
        raise HTTPException(status_code=400, detail="FROM_DATE is required.")
    if require_to and not t:
        raise HTTPException(status_code=400, detail="TO_DATE is required.")
    if t and t < f:
        raise HTTPException(status_code=400, detail="TO_DATE can't be before FROM_DATE.")
    return f.isoformat(), t.isoformat() if t else None


def _date_columns(table_name: str) -> tuple:
    """(all columns, IS_ACTIVE column or None, updated-stamp column or None)."""
    cur_schema = _raw_cursor()
    try:
        cur_schema.execute(f"SELECT * FROM {table_name} LIMIT 0")
        col_names = [d[0] for d in cur_schema.description] if cur_schema.description else []
    finally:
        cur_schema.close()
    if not col_names:
        raise HTTPException(status_code=500, detail=f"Could not read the columns of {table_name}")
    is_active_col = next((c for c in col_names if c.upper() == "IS_ACTIVE"), None)
    updated_ts_col = next((c for c in col_names if c.upper() in ("UPDATED_DATE", "UPDATED_TS", "UPDATED_AT")), None)
    return col_names, is_active_col, updated_ts_col


def _retire_other_ranges(cur, table_name, report_set, keep_id, now_ts, is_active_col, updated_ts_col):
    stamp = f", {updated_ts_col} = %s" if updated_ts_col else ""
    cur.execute(
        f"UPDATE {table_name} SET {is_active_col} = FALSE{stamp} "
        f"WHERE REPORT_SET = %s AND ID != %s AND {is_active_col} = TRUE",
        ((now_ts,) if updated_ts_col else ()) + (report_set, keep_id),
    )


@admin_router.put("/dq-tables/date-input/row")
def update_date_input_row(
    payload: dict = Body(...),
    auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    _require_bdc()
    row_id  = str(payload.get("id") or "").strip()
    updates = payload.get("updates") or {}

    if not row_id:
        raise HTTPException(status_code=400, detail="id is required.")
    if not isinstance(updates, dict) or not updates:
        raise HTTPException(status_code=400, detail="updates dict is empty.")

    table_name = _date_input_table_name()
    now_ts = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S.%f")
    col_names, is_active_col, updated_ts_col = _date_columns(table_name)
    by_upper = {c.upper(): c for c in col_names}

    # The row as it is now, so the range check and the one-active-per-set
    # rule see the values after this update, not just the fields that changed.
    cur = _raw_cursor()
    try:
        cur.execute(f"SELECT * FROM {table_name} WHERE ID = %s", (row_id,))
        current = cur.fetchone()
        cols = [d[0].upper() for d in cur.description] if cur.description else []
    finally:
        cur.close()
    if not current:
        raise HTTPException(status_code=404, detail=f"No row with ID '{row_id}'")
    after = dict(zip(cols, current))

    LOCKED = {"CREATED_DATE", "UPDATED_DATE", "CREATED_TS", "UPDATED_TS", "CREATED_AT", "UPDATED_AT", "ID"}
    set_parts, set_vals = [], []
    for col, val in updates.items():
        cu = str(col).upper()
        if cu in LOCKED:
            continue
        # Column names come from the table, never from the request.
        real = by_upper.get(cu)
        if not real:
            raise HTTPException(status_code=400, detail=f"Unknown column: {col}")
        if cu == "IS_ACTIVE":
            val = _as_bool(val)
        set_parts.append(f"{real} = %s")
        set_vals.append(None if val == "" else val)
        after[cu] = val

    if not set_parts:
        return {"success": True, "message": "Nothing to update."}
    _check_range(after.get("FROM_DATE"), after.get("TO_DATE"), require_to=False)
    if "REPORT_SET" in after and not str(after.get("REPORT_SET") or "").strip():
        raise HTTPException(status_code=400, detail="REPORT_SET is required.")

    if updated_ts_col:
        set_parts.append(f"{updated_ts_col} = %s")
        set_vals.append(now_ts)

    set_clause = ", ".join(set_parts)
    set_vals.append(row_id)

    try:
        cur = _raw_write_cursor()
        try:
            cur.execute(f"UPDATE {table_name} SET {set_clause} WHERE ID = %s", tuple(set_vals))
            if is_active_col and _as_bool(after.get("IS_ACTIVE")):
                _retire_other_ranges(cur, table_name, after.get("REPORT_SET"), row_id, now_ts, is_active_col, updated_ts_col)
        finally:
            cur.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Update failed: {type(e).__name__}: {e}")

    invalidate_summary()
    return {"success": True, "updated_by": auth["user"]["email"]}


@admin_router.patch("/dq-tables/date-input/toggle-active")
def toggle_date_input_active(
    payload: dict = Body(...),
    auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    _require_bdc()
    row_id    = str(payload.get("id") or "").strip()
    is_active = payload.get("is_active")

    if not row_id:
        raise HTTPException(status_code=400, detail="id is required.")
    if not isinstance(is_active, bool):
        raise HTTPException(status_code=400, detail="is_active (true/false) is required.")

    table_name = _date_input_table_name()
    now_ts = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S.%f")

    cur_schema = _raw_cursor()
    try:
        cur_schema.execute(f"SELECT * FROM {table_name} LIMIT 0")
        col_names = [d[0] for d in cur_schema.description] if cur_schema.description else []
    finally:
        cur_schema.close()

    is_active_col  = next((c for c in col_names if c.upper() == "IS_ACTIVE"), None)
    updated_ts_col = next(
        (c for c in col_names if c.upper() in ("UPDATED_DATE", "UPDATED_TS", "UPDATED_AT")), None
    )

    if not is_active_col:
        raise HTTPException(status_code=404, detail="IS_ACTIVE column not found.")

    try:
        cur = _raw_write_cursor()
        try:
            # If activating a row, first deactivate all other rows in the same REPORT_SET
            if bool(is_active):
                # Fetch the REPORT_SET value for this row
                report_set_cur = _raw_cursor()
                try:
                    report_set_cur.execute(f"SELECT REPORT_SET FROM {table_name} WHERE ID = %s", (row_id,))
                    rs_row = report_set_cur.fetchone()
                finally:
                    report_set_cur.close()

                if rs_row:
                    report_set_val = rs_row[0]
                    # Deactivate all other rows in the same REPORT_SET
                    if updated_ts_col:
                        cur.execute(
                            f"UPDATE {table_name} SET {is_active_col} = FALSE, {updated_ts_col} = %s "
                            f"WHERE REPORT_SET = %s AND ID != %s",
                            (now_ts, report_set_val, row_id),
                        )
                    else:
                        cur.execute(
                            f"UPDATE {table_name} SET {is_active_col} = FALSE "
                            f"WHERE REPORT_SET = %s AND ID != %s",
                            (report_set_val, row_id),
                        )

            # Now apply the requested toggle to the target row
            if updated_ts_col:
                cur.execute(
                    f"UPDATE {table_name} SET {is_active_col} = %s, {updated_ts_col} = %s WHERE ID = %s",
                    (bool(is_active), now_ts, row_id),
                )
            else:
                cur.execute(
                    f"UPDATE {table_name} SET {is_active_col} = %s WHERE ID = %s",
                    (bool(is_active), row_id),
                )
        finally:
            cur.close()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Toggle failed: {type(e).__name__}: {e}")

    invalidate_summary()
    return {"success": True, "is_active": bool(is_active), "updated_by": auth["user"]["email"]}


# ═══════════════════════════════════════════════════════════════════════════════
# Multi-source Table Browser
# Unified endpoints that fetch table lists from Snowflake, Denodo, or PostgreSQL
# based on the caller-supplied connection_type parameter.
# ═══════════════════════════════════════════════════════════════════════════════


# ── PostgreSQL helper ─────────────────────────────────────────────────────────

def _get_pg_config() -> dict:
    """Read PostgreSQL connection details from the 'postgresql' key in dq_config.json."""
    if not _bdc_config_manager or not _bdc_config_manager.config:
        raise HTTPException(status_code=503, detail="BDC config not available")
    pg = _bdc_config_manager.config.get("postgresql")
    if not pg:
        raise HTTPException(
            status_code=503,
            detail="No 'postgresql' section found in dq_config.json."
        )
    return pg


def _pg_cursor():
    """Open a psycopg2 cursor (connection not cached — PG queries are infrequent)."""
    try:
        import psycopg2
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="psycopg2 is not installed. Run: pip install psycopg2-binary"
        )
    cfg = _get_pg_config()
    try:
        conn = psycopg2.connect(
            host=cfg.get("host"),
            port=cfg.get("port", 5432),
            dbname=cfg.get("database"),
            user=cfg.get("user"),
            password=cfg.get("password"),
            connect_timeout=10,
        )
        conn.autocommit = True
        return conn, conn.cursor()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"PostgreSQL connection failed: {type(e).__name__}: {e}")


# ── Denodo helper ─────────────────────────────────────────────────────────────

def _get_denodo_creds():
    """Return (base_url, username, password) from the injected config manager."""
    if not _bdc_config_manager or not _bdc_config_manager.config:
        raise HTTPException(status_code=503, detail="BDC config not available")
    sa = _bdc_config_manager.config.get("denodo_service_account", {})
    base_url = sa.get("base_url")
    user = sa.get("username")
    password = sa.get("password")
    if not base_url or not user:
        raise HTTPException(
            status_code=503,
            detail="Denodo credentials not found in dq_config.json. "
                   "Ensure 'denodo_service_account' has base_url, username, password."
        )
    return base_url, user, password


def _sf_ident(name: str) -> str:
    """A Snowflake object name as a quoted identifier, so it can't carry SQL."""
    name = str(name or "").strip()
    if not name or len(name) > 255:
        raise HTTPException(status_code=400, detail="A database name is required")
    # Plain names stay unquoted so they keep resolving case-insensitively as before.
    if _re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
        return name
    return '"' + name.replace('"', '""') + '"'


def _denodo_verify() -> bool:
    """TLS checks on Denodo calls; off only when the config says so."""
    sa = (_bdc_config_manager.config or {}).get("denodo_service_account", {}) if _bdc_config_manager else {}
    return sa.get("verify_ssl", True) is not False


# ── Endpoint: databases / top-level groupings ─────────────────────────────────

@admin_router.get("/tables/databases")
def list_table_databases(
        connection_type: str = Query("Snowflake", description="Snowflake | Denodo | PostgreSQL"),
        auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    """Return the top-level groupings for the chosen connection."""
    ct = connection_type.strip().lower()

    if ct == "snowflake":
        _require_bdc()
        cur = _raw_cursor()
        try:
            cur.execute("SHOW DATABASES")
            rows = cur.fetchall()
            databases = sorted({row[1] for row in rows if row[1]})
        except Exception:
            databases = [_bdc_config_manager.get_snowflake_config().get("database", "")]
        finally:
            cur.close()
        return {"connection_type": "Snowflake", "databases": databases}

    elif ct == "denodo":
        base_url, user, password = _get_denodo_creds()
        import requests as _req
        from requests.auth import HTTPBasicAuth as _BA
        try:
            resp = _req.get(
                f"{base_url}/public/api/databases",
                auth=_BA(user, password),
                headers={"Accept": "application/json"},
                timeout=15,
                verify=_denodo_verify(),
            )
            if resp.status_code == 200:
                data = resp.json()
                # The response can be a list of {name:...} or similar shapes
                if isinstance(data, list):
                    databases = sorted({
                        d.get("name") or d.get("databaseName") or str(d)
                        for d in data if isinstance(d, dict)
                    })
                else:
                    databases = []
            else:
                databases = []
        except Exception:
            databases = []
        return {"connection_type": "Denodo", "databases": databases}

    elif ct == "postgresql":
        conn, cur = _pg_cursor()
        try:
            cur.execute("SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname")
            databases = [row[0] for row in cur.fetchall()]
        finally:
            cur.close();
            conn.close()
        return {"connection_type": "PostgreSQL", "databases": databases}

    raise HTTPException(status_code=400,
                        detail=f"Unknown connection_type '{connection_type}'. Use Snowflake, Denodo, or PostgreSQL.")


# ── Endpoint: schemas ─────────────────────────────────────────────────────────

@admin_router.get("/tables/schemas")
def list_table_schemas(

        connection_type: str = Query("Snowflake"),
        database: str = Query(...),
        auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    """Return schemas inside a database for the chosen connection."""
    ct = connection_type.strip().lower()

    if ct == "snowflake":
        _require_bdc()
        cur = _raw_cursor()
        try:
            cur.execute(f"SHOW SCHEMAS IN DATABASE {_sf_ident(database)}")
            rows = cur.fetchall()
            schemas = sorted({row[1] for row in rows if row[1] and row[1] != "INFORMATION_SCHEMA"})
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Snowflake error: {e}")
        finally:
            cur.close()
        return {"database": database, "schemas": schemas}

    elif ct == "denodo":
        # In Denodo the "database" IS the schema concept; return views grouped by database
        base_url, user, password = _get_denodo_creds()
        import requests as _req
        from requests.auth import HTTPBasicAuth as _BA
        try:
            resp = _req.get(
                f"{base_url}/public/api/databases/{database}/views",
                auth=_BA(user, password),
                headers={"Accept": "application/json"},
                timeout=15,
                verify=_denodo_verify(),
            )
            # If the endpoint doesn't exist, fall back to a dummy schema list
            if resp.status_code == 200:
                schemas = ["views"]
            else:
                schemas = ["views"]
        except Exception:
            schemas = ["views"]
        return {"database": database, "schemas": schemas}

    elif ct == "postgresql":
        conn, cur = _pg_cursor()
        try:
            cur.execute(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('pg_catalog','information_schema','pg_toast') "
                "ORDER BY schema_name"
            )
            schemas = [row[0] for row in cur.fetchall()]
        finally:
            cur.close();
            conn.close()
        return {"database": database, "schemas": schemas}

    raise HTTPException(status_code=400, detail=f"Unknown connection_type '{connection_type}'.")


# ── Endpoint: table names ─────────────────────────────────────────────────────

@admin_router.get("/tables/table-names")
def list_table_names(

        connection_type: str = Query("Snowflake"),
        database: str = Query(...),
        schema: str = Query(...),
        search: str = Query(""),
        auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    """Return table/view names in a specific database.schema for the chosen connection."""
    ct = connection_type.strip().lower()
    search = search.strip().lower()

    if ct == "snowflake":
        _require_bdc()
        # Names and search text are bound or quoted; they used to be pasted in.
        sf_filter = "AND CONTAINS(LOWER(TABLE_NAME), %s)" if search else ""
        sql = f"""
            SELECT TABLE_NAME, TABLE_TYPE
            FROM {_sf_ident(database)}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = %s
              AND TABLE_TYPE IN ('BASE TABLE', 'VIEW')
              {sf_filter}
            ORDER BY TABLE_NAME
            LIMIT 2000
        """
        cur = _raw_cursor()
        try:
            cur.execute(sql, (schema.upper(), search) if search else (schema.upper(),))
            rows = cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Snowflake error: {type(e).__name__}: {e}")
        finally:
            cur.close()
        tables = [
            {"database": database, "schema": schema, "table_name": row[0], "type": row[1],
             "full_name": f"{database}.{schema}.{row[0]}"}
            for row in rows
        ]
        return {"tables": tables}

    elif ct == "denodo":
        base_url, user, password = _get_denodo_creds()
        import requests as _req
        from requests.auth import HTTPBasicAuth as _BA
        # Use the metadata search API — filter by database name
        payload = {
            "text": search or "",
            "elementType": "Views",
            "databaseIds": [],
            "searchType": "CONTAINS" if search else "EXACT_MATCH",
            "offset": 0,
            "limit": 2000,
        }
        try:
            resp = _req.post(
                f"{base_url}/public/api/search/metadata?serverId=1",
                auth=_BA(user, password),
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json=payload,
                timeout=30,
                verify=_denodo_verify(),
            )
            tables = []
            if resp.status_code == 200:
                data = resp.json()
                elements = data.get("elements", data) if isinstance(data, dict) else data
                for el in (elements if isinstance(elements, list) else []):
                    db_info = el.get("database", {})
                    db_name = db_info.get("databaseName", "") if isinstance(db_info, dict) else ""
                    # Filter to the selected database
                    if database.lower() not in (db_name.lower(), ""):
                        continue
                    name = el.get("name", "")
                    if search and search not in name.lower():
                        continue
                    tables.append({
                        "database": db_name or database,
                        "schema": schema,
                        "table_name": name,
                        "type": "VIEW",
                        "full_name": f"{db_name or database}/{name}",
                    })
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Denodo error: {type(e).__name__}: {e}")
        return {"tables": sorted(tables, key=lambda t: t["table_name"])}

    elif ct == "postgresql":
        conn, cur = _pg_cursor()
        try:
            pg_filter = "AND POSITION(%s IN LOWER(table_name)) > 0" if search else ""
            cur.execute(f"""
                SELECT table_name, table_type
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type IN ('BASE TABLE', 'VIEW')
                  {pg_filter}
                ORDER BY table_name
                LIMIT 2000
            """, (schema, search) if search else (schema,))
            rows = cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"PostgreSQL error: {type(e).__name__}: {e}")
        finally:
            cur.close();
            conn.close()
        tables = [
            {"database": database, "schema": schema, "table_name": row[0], "type": row[1],
             "full_name": f"{database}.{schema}.{row[0]}"}
            for row in rows
        ]
        return {"tables": tables}

    raise HTTPException(status_code=400, detail=f"Unknown connection_type '{connection_type}'.")


# ── Endpoint: columns for a specific table ────────────────────────────────────

@admin_router.get("/tables/columns")
def list_table_columns(
        connection_type: str = Query("Snowflake"),
        database: str = Query(...),
        schema: str = Query(...),
        table: str = Query(...),
        auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    """Return column names (+ data types) for a specific database.schema.table."""
    ct = connection_type.strip().lower()

    if ct == "snowflake":
        _require_bdc()
        cur = _raw_cursor()
        try:
            cur.execute(f"""
                SELECT COLUMN_NAME, DATA_TYPE
                FROM {_sf_ident(database)}.INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s
                  AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
            """, (schema.upper(), table.upper()))
            rows = cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Snowflake error: {type(e).__name__}: {e}")
        finally:
            cur.close()
        columns = [{"name": row[0], "type": row[1]} for row in rows]
        return {"database": database, "schema": schema, "table": table, "columns": columns}

    elif ct == "postgresql":
        conn, cur = _pg_cursor()
        try:
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
            """, (schema, table))
            rows = cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"PostgreSQL error: {type(e).__name__}: {e}")
        finally:
            cur.close();
            conn.close()
        columns = [{"name": row[0], "type": row[1]} for row in rows]
        return {"database": database, "schema": schema, "table": table, "columns": columns}

    elif ct == "denodo":
        # Denodo table browsing is marked "Coming Soon" on the frontend — no column lookup yet.
        return {"database": database, "schema": schema, "table": table, "columns": []}

    raise HTTPException(status_code=400, detail=f"Unknown connection_type '{connection_type}'.")

# ─── Active Date Ranges — used by the BDC top bar to show dataset context ─────

@admin_router.get("/date-input/active-ranges")
def get_active_date_ranges(
        auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    """
    Return the active date range for each REPORT_SET in DQ_DATE_INPUT.

    For each report_set we:
      1. Count how many rows have IS_ACTIVE = TRUE.
         - 0  → status: "none"    (no active row configured)
         - 1  → status: "ok"      (normal case)
         - 2+ → status: "multiple" (data integrity issue — multiple actives)
      2. When multiple are active, pick the most recently updated row via
         QUALIFY so the frontend always has a single date range to display,
         while also surfacing the warning.

    Response shape:
      {
        "ranges": [
          {
            "report_set":       "RTE_REPORT_BIWEEKLY",
            "from_date":        "2026-05-01",   # null when status == "none"
            "to_date":          "2026-05-31",   # null when status == "none"
            "status":           "ok" | "none" | "multiple",
            "active_count":     1               # actual count of IS_ACTIVE rows
          },
          ...
        ]
      }
    """
    _require_bdc()
    try:
        sf = _bdc_connection_manager.get_client()
        table_name = _date_range_table_name()  # separate table — never DQ_DATE_INPUT

        # ── Step 1: count active rows per report_set (all rows, not just active) ──
        count_res = sf.execute(f"""
            SELECT
                REPORT_SET,
                SUM(CASE WHEN IS_ACTIVE = TRUE THEN 1 ELSE 0 END) AS active_count
            FROM {table_name}
            GROUP BY REPORT_SET
            ORDER BY REPORT_SET
        """)
        active_counts = {row[0]: int(row[1]) for row in count_res.rows}

        # ── Step 2: for each report_set that has ≥1 active row, get the best one ──
        # QUALIFY with ROW_NUMBER picks the most recently updated row when there
        # are multiple actives, so we always return exactly one date range per set.
        best_res = sf.execute(f"""
            SELECT
                REPORT_SET,
                TO_CHAR(FROM_DATE, 'YYYY-MM-DD') AS FROM_DATE,
                TO_CHAR(TO_DATE,   'YYYY-MM-DD') AS TO_DATE
            FROM {table_name}
            WHERE IS_ACTIVE = TRUE
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY REPORT_SET
                ORDER BY COALESCE(UPDATED_DATE, CREATED_DATE) DESC
            ) = 1
            ORDER BY REPORT_SET
        """)
        best_rows = {row[0]: {"from_date": row[1], "to_date": row[2]} for row in best_res.rows}

        # ── Step 3: merge counts + best row into the final response ──
        ranges = []
        for report_set, count in active_counts.items():
            best = best_rows.get(report_set)
            if count == 0:
                status = "none"
            elif count == 1:
                status = "ok"
            else:
                status = "multiple"

            ranges.append({
                "report_set":   report_set,
                "from_date":    best["from_date"] if best else None,
                "to_date":      best["to_date"]   if best else None,
                "status":       status,
                "active_count": count,
            })

        return {"ranges": ranges}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch active date ranges: {type(e).__name__}: {e}"
        )


# ─── Standard view: /api/bdc/summary and the validation run log ──────────────
# The summary is everything the hero, KPIs and rules table need in one round
# trip, cached per cadence so a room opening the page costs one set of queries.
# Runs started from SmartHub are recorded in Postgres (bdc_runs).

logger = logging.getLogger(__name__)

TREND_POINTS = 12
SUMMARY_TTL = 30.0
RUN_STALE_HOURS = 6       # ponytail: no heartbeat for these runs; a run older than this with no end is "lost"
RUN_KEEP_DAYS = 180

# Columns the latest view may carry for "rows evaluated"; first match wins.
ROWS_EVALUATED_COLUMNS = ("ROWS_EVALUATED", "EVALUATED_ROWS", "TOTAL_ROWS", "TOTAL_COUNT", "TOTAL_RECORDS", "RECORD_COUNT")

bdc_router = APIRouter(prefix="/api/bdc", tags=["BDC"])

_cache: Dict[bool, tuple] = {}
_cache_locks = {True: threading.Lock(), False: threading.Lock()}


def invalidate_summary():
    _cache.clear()


def next_run_from_range(dataset_range: dict) -> Optional[dict]:
    # next run = day after the active DQ_DATE_INPUT window closes
    to_date = (dataset_range or {}).get("to_date")
    if not to_date:
        return None
    return {"at": (date.fromisoformat(to_date) + timedelta(days=1)).isoformat()}


# ── summary ───────────────────────────────────────────────────────────────────

def _iso(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(EASTERN)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _day(value) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10] if value else None


def _num(value) -> Optional[float]:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def assemble_summary(rule_cols: List[str], rule_rows: Iterable[tuple], trend_rows: Iterable[tuple],
                     table_counts: Dict[str, Optional[int]], delta_mode: bool,
                     rule_domains: Optional[Dict[str, str]] = None) -> dict:
    """Turn raw query output into the payload. Pure, so it can be tested without Snowflake."""
    rule_domains = rule_domains or {}
    idx = {c.upper(): i for i, c in enumerate(rule_cols)}
    rows_col = next((c for c in ROWS_EVALUATED_COLUMNS if c in idx), None)

    history: Dict[str, list] = {}
    for rule_id, run_date, pct in trend_rows:
        if pct is None:
            continue
        history.setdefault(rule_id, []).append((run_date, float(pct)))

    rules, failed_by_rule = [], {}
    for r in rule_rows:
        get = lambda name: r[idx[name]] if name in idx else None
        rule_id = get("RULE_ID")
        title = get("RULE_TITLE") or ""
        table = _rule_table_name(rule_id, title)
        failed = table_counts.get(f"DELTA_{table}" if delta_mode else table)
        failed_by_rule[rule_id] = failed
        if not get("__ACCEPTED"):
            continue
        points = sorted(history.get(rule_id, []), key=lambda p: p[0])[-TREND_POINTS:]
        rows_evaluated = get(rows_col) if rows_col else None
        pass_pct = _num(get("PASS_PERCENTAGE"))
        rules.append({
            "rule_id": rule_id,
            "rule_title": title,
            "rule_description": get("RULE_DESCRIPTION") or "",
            "severity": (get("SEVERITY") or "").upper(),
            "target_table": get("TARGET_TABLE") or "",
            "dataset_title": get("DATASET_TITLE") or "",
            "domain": rule_domains.get(rule_id, ""),
            # No pass rate means the run produced no result (it errored). The UI shows
            # "No result" for these and they stay out of the averages below.
            "pass_percentage": pass_pct if pass_pct is not None else 0.0,
            "no_result": pass_pct is None,
            "status": get("STATUS") or "",
            "color_columns": get("__COLOR_COLUMNS") or "",
            "failed_rows": failed,
            "rows_evaluated": int(rows_evaluated) if _num(rows_evaluated) is not None else None,
            "last_run": _iso(points[-1][0]) if points else None,
            "trend": [{"date": _iso(d), "pass_percentage": p} for d, p in points],
        })
    rules.sort(key=lambda x: x["rule_id"])

    scored = [r for r in rules if not r["no_result"]]
    passes = [r["pass_percentage"] for r in scored]
    prev = [r["trend"][-2]["pass_percentage"] if len(r["trend"]) > 1 else r["pass_percentage"] for r in scored]
    has_history = any(len(r["trend"]) > 1 for r in scored)
    known = [r["failed_rows"] for r in rules if r["failed_rows"] is not None]

    by_day: Dict[str, list] = {}
    for r in rules:
        for p in r["trend"]:
            by_day.setdefault(p["date"][:10], []).append(p["pass_percentage"])
    series = [{"date": d, "avg_pass": round(sum(v) / len(v), 2)} for d, v in sorted(by_day.items())][-TREND_POINTS:]

    last_runs = [r["last_run"] for r in rules if r["last_run"]]
    return {
        "delta_mode": delta_mode,
        "rules": rules,
        "failed_by_rule": failed_by_rule,
        "series": series,
        "last_run": max(last_runs) if last_runs else None,
        "kpi": {
            "tables": len({r["target_table"] for r in rules if r["target_table"]}),
            "rules": len(rules),
            "datasets": len({r["dataset_title"] for r in rules if r["dataset_title"]}),
            "avg_pass": round(sum(passes) / len(passes), 2) if passes else 0.0,
            "prev_avg_pass": round(sum(prev) / len(prev), 2) if has_history else None,
            "at_100": sum(1 for p in passes if p >= 100),
            "critical": sum(1 for p in passes if p < 80),
            "failing_rows": sum(known),
            "failing_rows_known": len(known),
        },
    }


def _views(delta_mode: bool) -> dict:
    cfg = _bdc_config_manager.get_dq_config()
    def pick(key):
        name = cfg[key]
        if delta_mode and not name.endswith("_DELTA"):
            name += "_DELTA"
        return _bdc_config_manager.get_full_table_name(name)
    return {
        "latest": pick("rule_latest_view"),
        "trends": pick("rule_trends_view"),
        "input": _bdc_config_manager.get_full_table_name(cfg["rule_input_table"]),
        "reviews": _bdc_config_manager.core_table("DQ_REVIEW_REGISTRY"),
    }


def _query_rules(sf, views: dict, delta_mode: bool):
    review_filter = review_mode_sql(delta_mode)
    review_id = "'DELTA_' || v.RULE_ID" if delta_mode else "v.RULE_ID"
    try:
        return sf.execute(f"""
            WITH latest_reviews AS (
                SELECT RULE_ID, STATUS
                FROM {views['reviews']}
                WHERE {review_filter}
                QUALIFY ROW_NUMBER() OVER (PARTITION BY RULE_ID ORDER BY REVIEWED_AT DESC) = 1
            )
            SELECT v.*, i.COLOR_COLUMNS AS "__COLOR_COLUMNS",
                   (lr.RULE_ID IS NOT NULL) AS "__ACCEPTED"
            FROM {views['latest']} v
            LEFT JOIN {views['input']} i ON v.RULE_ID = i.RULE_ID AND i.IS_ACTIVE = TRUE
            LEFT JOIN latest_reviews lr ON lr.RULE_ID = {review_id} AND lr.STATUS = 'ACCEPTED'
        """)
    except Exception as e:
        # Same fallback as /api/rules: no review registry yet means nothing is gated.
        logger.warning(f"/api/bdc/summary review join failed ({e}); treating every rule as accepted")
        return sf.execute(f"""
            SELECT v.*, i.COLOR_COLUMNS AS "__COLOR_COLUMNS", TRUE AS "__ACCEPTED"
            FROM {views['latest']} v
            LEFT JOIN {views['input']} i ON v.RULE_ID = i.RULE_ID AND i.IS_ACTIVE = TRUE
        """)


def _query_trends(sf, views: dict):
    return sf.execute(f"""
        SELECT RULE_ID, RUN_DATE, AVG_PASS_PERCENTAGE
        FROM {views['trends']}
        QUALIFY ROW_NUMBER() OVER (PARTITION BY RULE_ID ORDER BY RUN_DATE DESC) <= {TREND_POINTS}
    """).rows


def _query_domains(sf, views: dict) -> List[tuple]:
    """(RULE_ID, DOMAIN, DATASET_TITLE) for every active DQ_RULE_INPUT row with a
    domain. Rows with no RULE_ID still count: they register a domain that has no
    rules yet. DATASET_TITLE is kept so build_summary can apply dataset access."""
    return sf.execute(f"""
        SELECT RULE_ID, TRIM(DOMAIN), DATASET_TITLE
        FROM {views['input']}
        WHERE IS_ACTIVE = TRUE AND NULLIF(TRIM(DOMAIN), '') IS NOT NULL
    """).rows


def _query_counts(sf) -> Dict[str, Optional[int]]:
    # Table metadata, not COUNT(*): one cheap query instead of one scan per rule.
    database = _bdc_config_manager.get_snowflake_config().get("database")
    res = sf.execute(f"""
        SELECT TABLE_NAME, ROW_COUNT
        FROM {database}.INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = %(schema)s
    """, {"schema": _bdc_config_manager.get_core_schema()})
    return {name: (int(n) if n is not None else None) for name, n in res.rows}


def _query_range(delta_mode: bool) -> dict:
    key = "RTE_REPORT_BIWEEKLY" if delta_mode else "RTE_REPORT_QUARTERLY"
    try:
        ranges = get_active_date_ranges(auth=None)["ranges"]
    except HTTPException as e:
        return {"error": str(e.detail)}
    entry = next((r for r in ranges if r["report_set"] == key), None)
    return entry or {"report_set": key, "status": "none", "from_date": None, "to_date": None, "active_count": 0}


def _query_summary(delta_mode: bool) -> dict:
    """Raw query output, shared by everyone; build_summary cuts it per caller."""
    sf = _bdc_connection_manager.get_client()
    views = _views(delta_mode)
    with ThreadPoolExecutor(max_workers=5) as pool:
        rules_f = pool.submit(_query_rules, sf, views, delta_mode)
        domains_f = pool.submit(_query_domains, sf, views)
        trends_f = pool.submit(_query_trends, sf, views)
        counts_f = pool.submit(_query_counts, sf)
        range_f = pool.submit(_query_range, delta_mode)
        rules = rules_f.result()
        try:
            trends = trends_f.result()
        except Exception as e:
            logger.warning(f"/api/bdc/summary trends query failed: {e}")
            trends = []
        try:
            counts = counts_f.result()
        except Exception as e:
            logger.warning(f"/api/bdc/summary row counts failed: {e}")
            counts = {}
        dataset_range = range_f.result()
        try:
            domains = domains_f.result()
        except Exception as e:
            # Older environments may not have the DOMAIN column; the dial just shows no domain ring.
            logger.warning(f"/api/bdc/summary domains query failed: {e}")
            domains = None

    return {"columns": rules.columns, "rows": rules.rows, "trends": trends, "counts": counts,
            "range": dataset_range, "domains": domains, "next_run": next_run_from_range(dataset_range),
            "generated_at": datetime.now(EASTERN).isoformat()}


def build_summary(raw: dict, delta_mode: bool, allowed: Optional[set]) -> dict:
    rows = raw["rows"]
    if allowed is not None:
        col = next((i for i, c in enumerate(raw["columns"]) if c.upper() == "DATASET_TITLE"), None)
        rows = [r for r in rows if col is not None and dataset_key(r[col]) in allowed]
    known = raw.get("domains") is not None
    domain_rows = raw.get("domains") or []
    if allowed is not None:
        # Same rule as the rules themselves: a domain row is visible only if its
        # dataset is (blank dataset = NO_DATASET grant), so names never leak.
        domain_rows = [r for r in domain_rows if dataset_key(r[2]) in allowed]
    rule_domains = {r[0]: r[1] for r in domain_rows if r[0]}
    payload = assemble_summary(raw["columns"], rows, raw["trends"], raw["counts"], delta_mode, rule_domains)
    # Every active domain, including ones with no rules yet, so the dial shows coverage gaps.
    # None (not []) when the domains could not be read, so the UI hides the ring instead of showing it empty.
    payload["domains"] = sorted({r[1] for r in domain_rows}, key=str.lower) if known else None
    payload["range"] = raw["range"]
    payload["next_run"] = raw["next_run"]
    payload["generated_at"] = raw["generated_at"]
    payload["scope"] = {"all": allowed is None, "datasets": sorted(allowed) if allowed is not None else None}
    return payload


@bdc_router.get("/summary")
def get_summary(
    delta_mode: bool = Query(False, description="Semi-Monthly (_DELTA) data instead of quarterly"),
    refresh: bool = Query(False, description="Skip the short-lived cache"),
    auth: dict = Depends(require_permission("viewBusinessChecks")),
):
    """Everything the standard view's first paint needs, in one response."""
    if not _bdc_connection_manager or not _bdc_config_manager or not _bdc_connection_manager.available:
        raise HTTPException(status_code=503, detail="Business Defined Checks service not available")
    with _cache_locks[delta_mode]:
        hit = _cache.get(delta_mode)
        if hit and not refresh and time.monotonic() - hit[0] < SUMMARY_TTL:
            raw = hit[1]
        else:
            try:
                raw = _query_summary(delta_mode)
            except HTTPException:
                raise
            except Exception as e:
                logger.error(f"/api/bdc/summary failed: {e}")
                raise HTTPException(status_code=500, detail=f"Could not load the summary: {e}")
            _cache[delta_mode] = (time.monotonic(), raw)
    return build_summary(raw, delta_mode, bdc_datasets(auth))


# ── run log ───────────────────────────────────────────────────────────────────

def _who(auth: dict) -> tuple:
    user = (auth or {}).get("user") or {}
    return (user.get("name") or user.get("email") or "", user.get("email") or "")


# Runs this process started and has not seen end. After a restart a row can
# say "running" forever; this is how such a leftover from this host is told
# apart from a live run, so it doesn't block new runs.
_LIVE_RUNS: set = set()


def _record_start(kind: str, mode: str, auth: dict) -> Optional[str]:
    if not db.is_configured():
        return None
    run_id = uuid.uuid4().hex
    name, email = _who(auth)
    try:
        with db.transaction() as conn:
            conn.execute(f"DELETE FROM bdc_runs WHERE started_at < now() - interval '{RUN_KEEP_DAYS} days'")
            conn.execute(
                "INSERT INTO bdc_runs (id, kind, mode, started_by, started_by_email, host) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, kind, mode or "", name, email, socket.gethostname()),
            )
        _LIVE_RUNS.add(run_id)
        return run_id
    except Exception as e:
        logger.warning(f"Could not record BDC {kind} start: {e}")
        return None


def _record_end(run_id: Optional[str], returncode: Optional[int], status: str):
    if not run_id:
        return
    _LIVE_RUNS.discard(run_id)
    try:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE bdc_runs SET finished_at = now(), returncode = ?, status = ? WHERE id = ? AND finished_at IS NULL",
                (returncode, status, run_id),
            )
    except Exception as e:
        logger.warning(f"Could not record BDC run end: {e}")


async def record_start(kind: str, mode: str, auth: dict) -> Optional[str]:
    return await asyncio.to_thread(_record_start, kind, mode, auth)


async def record_end(run_id: Optional[str], returncode: Optional[int]):
    # The DQE engine exits 1 when some rules errored: the others still wrote
    # results, so the cached summary is stale either way.
    status = {0: "success", 1: "partial"}.get(returncode, "failed")
    await asyncio.to_thread(_record_end, run_id, returncode, status)
    if returncode in (0, 1):
        invalidate_summary()


WHOLE_RUN_MODES = ("EXECUTE_FULL", "EXECUTE_ALLRULES", "EXECUTE_ALLRULES_DELTA")
RUN_MODE_RE = _re.compile(r"EXECUTE_[A-Za-z0-9_.\-]{1,200}")


def _covers_everything(kind: str, mode: str) -> bool:
    return kind == "compilation" or mode in WHOLE_RUN_MODES


def _conflicting_run(kind: str, mode: str) -> Optional[dict]:
    """A run still going that this one would trip over: anything at all next to
    a compilation or whole-table run, or the same single rule twice."""
    if not db.is_configured():
        return None
    try:
        with db.transaction(read_only=True) as conn:
            rows = conn.execute(f"""
                SELECT id, kind, mode, started_by, started_at, host FROM bdc_runs
                WHERE finished_at IS NULL AND started_at > now() - interval '{RUN_STALE_HOURS} hours'
                ORDER BY started_at
            """).fetchall()
    except Exception as e:
        logger.warning(f"Could not check for running BDC runs: {e}")
        return None
    here = socket.gethostname()
    for r in map(dict, rows):
        if r.get("host") == here and r.get("id") not in _LIVE_RUNS:
            continue  # left over from before a restart of this server
        if _covers_everything(kind, mode) or _covers_everything(r["kind"], r["mode"]) or r["mode"] == mode:
            return r
    return None


async def ensure_no_conflicting_run(kind: str, mode: str) -> None:
    """409 when another SmartHub-started run would overlap this one.
    ponytail: check-then-insert, so two clicks in the same instant can both pass;
    an advisory lock in Postgres closes that if it ever matters."""
    r = await asyncio.to_thread(_conflicting_run, kind, mode)
    if r:
        what = "compilation" if r["kind"] == "compilation" else f"validation ({r['mode']})"
        at = _iso(r["started_at"]) or ""
        raise HTTPException(status_code=409, detail=f"A {what} started by {r['started_by'] or 'someone'} at {at[11:16]} ET "
                                                    "is still running. Try again when it has finished.")


def record_detached(run_id: Optional[str]):
    """The browser went away before the script finished; we no longer know how it ended."""
    _record_end(run_id, None, "detached")


@bdc_router.get("/runs")
def list_runs(
    limit: int = Query(50, ge=1, le=500),
    auth: dict = Depends(require_permission("viewBDCSuperuser")),
):
    """Validation and compilation runs started from SmartHub, newest first."""
    if not db.is_configured():
        raise HTTPException(status_code=503, detail="Run history needs the SmartHub database, which is not configured")
    try:
        with db.transaction(read_only=True) as conn:
            rows = conn.execute(f"""
                SELECT id, kind, mode, started_by, started_by_email, started_at, finished_at, returncode,
                       CASE WHEN finished_at IS NULL AND started_at < now() - interval '{RUN_STALE_HOURS} hours'
                            THEN 'lost' ELSE status END AS status,
                       EXTRACT(EPOCH FROM (COALESCE(finished_at, now()) - started_at))::int AS seconds
                FROM bdc_runs ORDER BY started_at DESC LIMIT ?
            """, (limit,)).fetchall()
    except db.DatabaseUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    runs = [dict(r) for r in rows]
    ids = allowed_rule_ids(auth)
    if ids is not None:
        whole = ("EXECUTE_FULL", "EXECUTE_ALLRULES", "EXECUTE_ALLRULES_DELTA")
        runs = [r for r in runs if r["kind"] != "validation" or r["mode"] in whole
                or not r["mode"].startswith("EXECUTE_") or r["mode"][8:] in ids]
    return {"runs": runs}