from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from cryptography.hazmat.primitives import serialization

from audit import write_audit_event
from auth import require_auth, require_permission

logger = logging.getLogger(__name__)

gra_data_domains_router = APIRouter(prefix="/api/gra-data-domains", tags=["GRA Data Domains"])

_config: Optional[Dict[str, Any]] = None

TABLE = "UNSTRUCTURED_PACKAGES"


def set_gra_data_domains_dependencies(config: Optional[Dict[str, Any]]):
    """Called once from app.py with the 'structured_packages' section of
    dq_config.json. Falls back to the 'unstructured' section since this table
    lives on the same Snowflake account/warehouse as the Unstructured explorer."""
    global _config
    _config = config
    if not _config:
        logger.warning("⚠️ structured_packages config not found - GRA Data Domains endpoints will be unavailable")


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


class _GraDataDomainsConnectionManager:
    CONNECTION_TTL = 2 * 60 * 60  # 2 hours

    def __init__(self):
        self._client = None
        self._created_at = 0.0
        self._lock = threading.Lock()

    def _connect(self):
        from app import SnowflakeClient
        import snowflake.connector

        if not _config:
            raise HTTPException(status_code=503, detail="GRA Data Domains not configured - missing 'structured_packages' section in dq_config.json")

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
                try:
                    self._client.execute("SELECT 1")
                    return self._client
                except Exception:
                    logger.info("GRA Data Domains connection stale/expired, reconnecting")

            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass

            self._client = self._connect()
            self._created_at = time.time()
            return self._client


_conn_manager = _GraDataDomainsConnectionManager()


def _run(sql: str, params: Optional[Dict[str, Any]] = None):
    client = _conn_manager.get_client()
    try:
        return client.execute(sql, params or {})
    except FileNotFoundError:
        raise
    except HTTPException:
        raise
    except Exception:
        logger.info("GRA Data Domains query failed on shared connection, retrying once")
        client = _conn_manager._connect()
        with _conn_manager._lock:
            _conn_manager._client = client
            _conn_manager._created_at = time.time()
        return client.execute(sql, params or {})


def _to_json_safe(val):
    if hasattr(val, "isoformat"):
        return val.isoformat()
    if isinstance(val, float):
        if val != val or val == float("inf") or val == float("-inf"):
            return None
    return val


@gra_data_domains_router.get("/domains")
def get_domains():
    """All distinct domains with a record count each - drives the welcome
    screen's list of domains."""
    try:
        result = _run(f'SELECT "DOMAIN", COUNT(*) FROM {TABLE} GROUP BY "DOMAIN" ORDER BY "DOMAIN"')
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GRA Data Domains /domains failed: {e}")
        raise HTTPException(status_code=500, detail=f"Query failed: {e}")

    return {
        "domains": [
            {"domain": r[0], "count": r[1]} for r in result.rows if r[0] is not None
        ]
    }


@gra_data_domains_router.get("/data")
def get_domain_data(domain: str = Query(...)):
    """Full rows for one domain - shown in the modal when a domain is clicked.
    SELECT * so any column added to the table later is included automatically."""
    try:
        result = _run(f'SELECT * FROM {TABLE} WHERE "DOMAIN" = %(domain)s', {"domain": domain})
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GRA Data Domains /data failed: {e}")
        raise HTTPException(status_code=500, detail=f"Query failed: {e}")

    return {
        "domain": domain,
        "columns": result.columns,
        "rows": [[_to_json_safe(v) for v in row] for row in result.rows],
    }


# ============================================================================
# EDITING
# ----------------------------------------------------------------------------
# The table has no primary key, so a row is identified the only way it safely
# can be: by every value it had when the browser read it. That doubles as an
# optimistic lock — if someone changed the row in between, nothing matches and
# the save is rejected instead of silently overwriting their work.
#
# DOMAIN stays read-only. It's what the whole page groups by, and renaming it
# from a cell would move the row to a different (or brand new) domain, which
# is a different operation than fixing a steward's name.
# ============================================================================

LOCKED_COLUMNS = {"DOMAIN"}

_columns_cache: Optional[list] = None


def _table_columns(refresh: bool = False) -> list:
    """Real column names, used to whitelist anything the browser sends.
    Column names can't be bound as parameters, so they must be checked."""
    global _columns_cache
    if _columns_cache is None or refresh:
        _columns_cache = list(_run(f"SELECT * FROM {TABLE} LIMIT 0").columns)
    return _columns_cache


def _match_clause(original: Dict[str, Any], columns: list):
    """WHERE that pins the row to the exact values the browser last saw.
    Values we can't compare reliably as binds (timestamps came back as ISO
    strings) are skipped — the remaining columns still identify the row, and
    the count check below refuses to run if they don't."""
    parts, params = [], {}
    for i, (col, val) in enumerate(original.items()):
        if col not in columns:
            raise HTTPException(status_code=400, detail=f"Unknown column: {col}")
        if val is None:
            parts.append(f'"{col}" IS NULL')
        elif isinstance(val, (str, int, float, bool)):
            key = f"w{i}"
            parts.append(f'"{col}" = %({key})s')
            params[key] = val
    if not parts:
        raise HTTPException(status_code=400, detail="Nothing to match the row on")
    return " AND ".join(parts), params


class RowUpdate(BaseModel):
    domain: str
    original: Dict[str, Any]
    changes: Dict[str, Any]


@gra_data_domains_router.put(
    "/row",
    dependencies=[Depends(require_permission("editGraDataDomains"))],
)
def update_row(payload: RowUpdate, request: Request, auth: dict = Depends(require_auth)):
    """Update one row of one domain. Rejects the save unless the row still
    looks exactly like it did when it was read, and unless exactly one row
    matches — a duplicate row would otherwise take the edit twice."""
    changes = {k: v for k, v in payload.changes.items() if k not in LOCKED_COLUMNS}
    if not changes:
        raise HTTPException(status_code=400, detail="No editable changes supplied")

    columns = _table_columns()
    # A column added upstream after this process started won't be in the
    # cache. Re-read once before rejecting, so adding a column to the table
    # doesn't need an API restart to become editable.
    if any(col not in columns for col in changes):
        columns = _table_columns(refresh=True)
    for col in changes:
        if col not in columns:
            raise HTTPException(status_code=400, detail=f"Unknown column: {col}")

    where_sql, where_params = _match_clause(payload.original, columns)

    try:
        found = _run(f"SELECT COUNT(*) FROM {TABLE} WHERE {where_sql}", where_params)
        count = found.rows[0][0] if found.rows else 0
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GRA Data Domains /row lookup failed: {e}")
        raise HTTPException(status_code=500, detail=f"Lookup failed: {e}")

    if count == 0:
        raise HTTPException(
            status_code=409,
            detail="This row changed since you opened it. Reload the domain and try again.",
        )
    if count > 1:
        raise HTTPException(
            status_code=409,
            detail=f"{count} identical rows match — this one can't be edited safely.",
        )

    set_parts, set_params = [], {}
    for i, (col, val) in enumerate(changes.items()):
        key = f"s{i}"
        set_parts.append(f'"{col}" = %({key})s')
        set_params[key] = None if val == "" else val

    sql = f'UPDATE {TABLE} SET {", ".join(set_parts)} WHERE {where_sql}'
    try:
        _run(sql, {**set_params, **where_params})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"GRA Data Domains /row update failed: {e}")
        raise HTTPException(status_code=500, detail=f"Update failed: {e}")

    summary = "; ".join(
        f"{col}: {payload.original.get(col) or '(empty)'} -> {val or '(empty)'}"
        for col, val in changes.items()
    )
    write_audit_event(
        action="structured_package_row_updated",
        performed_by=auth.get("email", ""),
        target=payload.domain,
        details=summary,
        ip_address=request.client.host if request.client else "",
    )

    return {"updated": True, "domain": payload.domain, "changes": changes}