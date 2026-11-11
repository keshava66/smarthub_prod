"""Virtual Regulator - External scope. FDA press releases pulled from the RSS feed
into DEV_RAW.UNSTRUCTURED_EXPLORER.FDA_PRESS_RELEASE_RSS.

The loader dumps the raw RSS item as-is, so LINK / PUBDATE / DC:CREATOR / GUID
come in as arrays of {"value": ...}. We just use the flat columns it also writes
(ITEM_URL, PUBLISHED_TS) and dig the creator out of the array.

Table is tiny (a feed keeps ~20 items per load), so we send everything back
and let the page do search / sort.

Other External tables go in the dropdown too. They're plain tables, so /table/{id}
just does SELECT * and the page shows them in a generic grid. Add more in config:
  "vr_external_sources": [{"id": "x", "label": "X", "table": "DB.SCHEMA.TABLE"}]

EXTERNAL_FDA_MAPPING says which health authority / region / feed each table
comes from. We match its TABLE_NAME to the last part of each source table and
send that along, the page builds its facet filters from it.
"""
from __future__ import annotations

import logging
import re
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from auth import require_permission
from unstructured import (
    _cache_get,
    _cache_key,
    _cache_set,
    _render_sql_for_display,
    _run,
    _to_json_safe,
)

logger = logging.getLogger(__name__)

DEFAULT_TABLE = "DEV_RAW.UNSTRUCTURED_EXPLORER.FDA_PRESS_RELEASE_RSS"
MAX_ROWS = 5000

PRESS_ID = "press_releases"
DEFAULT_MAPPING_TABLE = "DEV_RAW.FDA_RSS.EXTERNAL_FDA_MAPPING"

# extra tables for the dropdown. config can add to / override these by id.
DEFAULT_SOURCES = [
    # {"id": "warning_letters", "label": "FDA Warning Letters", "table": "DEV_RAW.UNSTRUCTURED_EXPLORER.FDA_WARNING_LETTERS"},
]

vr_external_router = APIRouter(prefix="/api/vr-external", tags=["Virtual Regulator External"])
_state = {"table": DEFAULT_TABLE, "sources": [], "mapping": DEFAULT_MAPPING_TABLE}

_NAME_OK = re.compile(r"[A-Za-z0-9_$.]+")
_ID_OK = re.compile(r"[A-Za-z0-9_-]+")


def set_vr_external_dependencies(config: Optional[Dict[str, Any]]):
    cfg = config or {}
    table = cfg.get("vr_external_table") or DEFAULT_TABLE
    if not _NAME_OK.fullmatch(table):
        logger.warning(f"vr_external_table '{table}' looks wrong, using {DEFAULT_TABLE}")
        table = DEFAULT_TABLE
    _state["table"] = table

    mapping = cfg.get("vr_external_mapping_table") or DEFAULT_MAPPING_TABLE
    if not _NAME_OK.fullmatch(mapping):
        logger.warning(f"vr_external_mapping_table '{mapping}' looks wrong, using {DEFAULT_MAPPING_TABLE}")
        mapping = DEFAULT_MAPPING_TABLE
    _state["mapping"] = mapping

    merged = {s["id"]: s for s in DEFAULT_SOURCES}
    for s in cfg.get("vr_external_sources") or []:
        if isinstance(s, dict) and s.get("id"):
            merged[s["id"]] = {**merged.get(s["id"], {}), **s}

    good = []
    for s in merged.values():
        sid, tbl = str(s.get("id", "")), str(s.get("table", ""))
        if sid == PRESS_ID or not _ID_OK.fullmatch(sid) or not _NAME_OK.fullmatch(tbl):
            logger.warning(f"skipping vr_external source {s!r}, bad id or table name")
            continue
        good.append({"id": sid, "label": s.get("label") or sid, "table": tbl, "order_by": s.get("order_by")})
    _state["sources"] = good


def _source(sid: str) -> Dict[str, Any]:
    for s in _state["sources"]:
        if s["id"] == sid:
            return s
    raise HTTPException(status_code=404, detail=f"Unknown source '{sid}'")


def _to_iso(pub: Optional[str]) -> Optional[str]:
    # "Wed, 23 Sep 2026 15:50:17 EDT" -> ISO. parsedate knows EDT/EST.
    if not pub:
        return None
    try:
        return parsedate_to_datetime(pub).isoformat()
    except (TypeError, ValueError):
        return None


def _mapping(bypass: bool = False) -> Dict[str, Dict[str, Any]]:
    """TABLE_NAME (upper) -> row from EXTERNAL_FDA_MAPPING. Empty if it can't be read,
    the dropdown still works without it, just no facets."""
    t = _state["mapping"]
    key = _cache_key("vr-external-mapping", {"table": t})
    if not bypass:
        hit = _cache_get(key)
        if hit is not None:
            return hit

    sql = (
        'SELECT "REGION_INSIGHT", "HA_SITE_NAME", "TABLE_NAME", "WEBSITE", "RSS_NON_RSS", '
        '"CURRENT_LOADED_IN_DH", "ROW_COUNT", "CDC_ENABLED" '
        f'FROM {t} WHERE "TABLE_NAME" IS NOT NULL'
    )
    try:
        res = _run(sql, {})
    except Exception as e:
        logger.warning(f"couldn't read mapping table {t}: {e}")
        return {}

    out = {}
    for r in res.rows:
        r = [_to_json_safe(v) for v in r]
        name = str(r[2]).strip().upper()
        out[name] = {
            "region": r[0],
            "site": r[1],
            "website": r[3],
            "feed": r[4],
            "loaded": r[5],
            "row_count": r[6],
            "cdc": r[7],
        }
    _cache_set(key, out)
    return out


_FDA_RE = re.compile(r"\bFDA\b|food and drug", re.I)


def _press_meta(maps: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Press release table isn't in EXTERNAL_FDA_MAPPING, so borrow the FDA row's
    authority / region so it groups under the same health authority."""
    own = maps.get(_state["table"].split(".")[-1].upper())
    if own:
        return own
    fda = next((m for m in maps.values() if _FDA_RE.search(str(m.get("site") or ""))), None)
    return {
        "site": fda["site"] if fda else "FDA",
        "region": fda["region"] if fda else None,
        "website": "https://www.fda.gov/news-events/fda-newsroom/press-announcements",
        "feed": "RSS",
        "loaded": None,
        "row_count": None,
        "cdc": None,
    }


@vr_external_router.get("/sources")
def get_sources(
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    maps = _mapping(bypass_cache)
    # press releases always first, it has its own dashboard
    out = [{"id": PRESS_ID, "label": "FDA Press Releases", "table": _state["table"], "kind": "press",
            "meta": _press_meta(maps)}]
    for s in _state["sources"]:
        out.append({
            "id": s["id"],
            "label": s["label"],
            "table": s["table"],
            "kind": "table",
            "meta": maps.get(s["table"].split(".")[-1].upper()),
        })
    return {"sources": out, "mapping_table": _state["mapping"], "mapped": bool(maps)}


@vr_external_router.get("/table/{source_id}")
def get_table(
    source_id: str,
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    src = _source(source_id)
    t = src["table"]
    key = _cache_key("vr-external-table", {"table": t})
    if not bypass_cache:
        hit = _cache_get(key)
        if hit is not None:
            return {**hit, "cached": True}

    order = src.get("order_by")
    order_sql = f' ORDER BY "{order}" DESC' if order and _NAME_OK.fullmatch(order) else ""
    # pull one extra row so we know if it got cut off
    sql = f"SELECT * FROM {t}{order_sql} LIMIT {MAX_ROWS + 1}"
    try:
        res = _run(sql, {})
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"VR external /table/{source_id} failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    cols = [str(c) for c in res.columns]
    rows = [[_to_json_safe(v) for v in r] for r in res.rows]
    truncated = len(rows) > MAX_ROWS
    payload = {
        "id": source_id,
        "label": src["label"],
        "table": t,
        "columns": cols,
        "rows": rows[:MAX_ROWS],
        "truncated": truncated,
        "sql": _render_sql_for_display(sql.replace(f"LIMIT {MAX_ROWS + 1}", f"LIMIT {MAX_ROWS}"), {}),
    }
    _cache_set(key, payload)
    return {**payload, "cached": False}


@vr_external_router.get("/items")
def get_items(
    bypass_cache: bool = Query(False),
    auth: dict = Depends(require_permission("viewUnstructured")),
):
    t = _state["table"]
    key = _cache_key("vr-external-items", {"table": t})
    if not bypass_cache:
        hit = _cache_get(key)
        if hit is not None:
            return {**hit, "cached": True}

    # same item can land again on a later load, keep the newest copy
    sql = (
        f'SELECT "ITEM_KEY", "TITLE", "DESCRIPTION", '
        f'COALESCE("ITEM_URL", "LINK"[0]:"value"::STRING) AS ITEM_URL, '
        f'COALESCE("PUBLISHED_TS", "PUBDATE"[0]:"value"::STRING) AS PUBLISHED_TS, '
        f'"DC:CREATOR"[0]:"value"::STRING AS CREATOR, '
        f'"SOURCE_NAME", "LOAD_TS" '
        f"FROM {t} "
        f'QUALIFY ROW_NUMBER() OVER (PARTITION BY "ITEM_KEY" ORDER BY "LOAD_TS" DESC) = 1 '
        f'ORDER BY "LOAD_TS" DESC LIMIT {MAX_ROWS}'
    )
    try:
        res = _run(sql, {})
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"VR external /items failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")

    items = []
    for r in res.rows:
        row = [_to_json_safe(v) for v in r]
        items.append({
            "ITEM_KEY": row[0],
            "TITLE": (row[1] or "").strip(),
            "DESCRIPTION": (row[2] or "").strip(),
            "ITEM_URL": row[3],
            "PUBLISHED_TS": row[4],
            "PUBLISHED_ISO": _to_iso(row[4]),
            "CREATOR": row[5],
            "SOURCE_NAME": row[6],
            "LOAD_TS": row[7],
        })
    # newest press release first
    items.sort(key=lambda x: x["PUBLISHED_ISO"] or "", reverse=True)

    payload = {"table": t, "items": items, "sql": _render_sql_for_display(sql, {})}
    _cache_set(key, payload)
    return {**payload, "cached": False}