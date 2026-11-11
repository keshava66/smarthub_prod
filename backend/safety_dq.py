"""Safety tab in DQE. Reads its data from an Excel file instead of Snowflake.

Edit the Excel file and the page picks it up on the next refresh (it also
checks every 30s). File is re-read only when its modified time changes.

File location: SAFETY_DQ_FILE env var, else safety_dq_data.xlsx next to this file.

Sheet "Rules"   - one row per rule. A row with Domain but no Rule ID is a domain
                  with no approved rules yet (shows as NA).
    Rule ID, Rule Title, Dimension, Component, Domain, Severity, Description,
    Target Table, Connection, Pass %, Status
    Pass % blank -> rule counts as not yet scored. Status blank -> worked out from Pass %.

Sheet "Run History" - one row per rule per run. ("History" is reserved in Excel.)
    Rule ID, Run Date, Pass %
"""
from __future__ import annotations

import logging
import os
import re
import threading
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

safety_dq_router = APIRouter(prefix="/dq/safety", tags=["Safety DQ"])

DATA_FILE = Path(os.getenv("SAFETY_DQ_FILE") or Path(__file__).with_name("safety_dq_data.xlsx"))

# header in the sheet -> key the page uses
RULE_COLS = {
    "rule_id": "rule_id",
    "rule_title": "rule_title",
    "dimension": "dimension",
    "component": "component_name",
    "domain": "domain",
    "severity": "severity",
    "description": "rule_description",
    "target_table": "target_table",
    "connection": "connection_type",
    "pass": "pass_percentage",
    "status": "status",
}
HISTORY_COLS = {"rule_id": "rule_id", "run_date": "date", "pass": "pass_percentage"}

_cache = {"mtime": None, "data": None}
_lock = threading.Lock()


def _key(header) -> str:
    # "Pass %" -> "pass", "Rule ID" -> "rule_id"
    k = re.sub(r"[^a-z0-9]+", "_", str(header or "").strip().lower()).strip("_")
    return "pass" if k.startswith("pass") else k


def _text(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _pct(cell):
    v = cell.value
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = v.strip().rstrip("%").strip()
        if not v:
            return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    # cell formatted as a percentage holds 0.88, not 88
    if "%" in str(getattr(cell, "number_format", "") or "") and n <= 1:
        n *= 100
    return round(n, 2)


def _date(v):
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = _text(v)
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:11].strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s


def _status(p):
    if p is None:
        return "ERROR"
    return "PASS" if p >= 95 else "WARNING" if p >= 80 else "FAIL"


def _read_sheet(wb, name, cols, required, alt=()):
    names = {n.lower() for n in (name, *alt)}
    match = next((ws for ws in wb.worksheets if ws.title.strip().lower() in names), None)
    if match is None:
        raise HTTPException(422, f'Sheet "{name}" not found in {DATA_FILE.name}.')
    rows = match.iter_rows()
    header = next(rows, None)
    if not header:
        return []
    idx = {}
    for i, c in enumerate(header):
        k = _key(c.value)
        if k in cols and k not in idx:
            idx[k] = i
    missing = [k for k in required if k not in idx]
    if missing:
        raise HTTPException(422, f'Sheet "{name}" is missing column(s): {", ".join(missing)}.')
    out = []
    for r in rows:
        if all(c.value in (None, "") for c in r):
            continue
        out.append({cols[k]: (r[i] if i < len(r) else None) for k, i in idx.items()})
    return out


def _load():
    import openpyxl

    wb = openpyxl.load_workbook(DATA_FILE, read_only=True, data_only=True)
    try:
        raw_rules = _read_sheet(wb, "Rules", RULE_COLS, ["rule_id", "dimension", "domain"])
        raw_hist = _read_sheet(wb, "Run History", HISTORY_COLS, ["rule_id", "run_date", "pass"], alt=("History",))
    finally:
        wb.close()

    history: dict[str, list] = {}
    for h in raw_hist:
        rid = _text(h["rule_id"].value)
        d = _date(h["date"].value)
        p = _pct(h["pass_percentage"])
        if rid and d and p is not None:
            history.setdefault(rid, []).append({"date": d, "pass_percentage": p})
    for pts in history.values():
        pts.sort(key=lambda x: x["date"])

    rules, empty_domains, seen = [], [], set()
    for r in raw_rules:
        rid = _text(r["rule_id"].value)
        domain = _text(r["domain"].value)
        if not rid:
            if domain and domain not in empty_domains:
                empty_domains.append(domain)
            continue
        if rid in seen:
            continue  # first row wins
        seen.add(rid)
        row = {k: _text(c.value) for k, c in r.items() if k != "pass_percentage"}
        p = _pct(r["pass_percentage"]) if "pass_percentage" in r else None
        row["pass_percentage"] = p
        row["status"] = (row.get("status") or _status(p)).upper()
        row["severity"] = (row.get("severity") or "").upper() or None
        row["rule_title"] = row.get("rule_title") or rid
        rules.append(row)

    used = {r["domain"] for r in rules}
    return {
        "rules": rules,
        "emptyDomains": [d for d in empty_domains if d not in used],
        "history": {rid: pts for rid, pts in history.items() if rid in seen},
    }


@safety_dq_router.get("/data")
def safety_data():
    if not DATA_FILE.exists():
        raise HTTPException(404, f"Safety data file not found: {DATA_FILE.name}")
    mtime = DATA_FILE.stat().st_mtime
    with _lock:
        if _cache["mtime"] != mtime:
            try:
                _cache["data"] = _load()
            except HTTPException:
                raise
            except PermissionError:
                # Excel on Windows locks the file while saving, keep the last good copy
                if _cache["data"] is None:
                    raise HTTPException(503, "Safety data file is busy, try again in a moment.")
                return {**_cache["data"], "updated": _cache["mtime"]}
            except Exception as e:
                logger.exception("Safety DQ file read failed")
                raise HTTPException(500, f"Could not read {DATA_FILE.name}: {e}")
            _cache["mtime"] = mtime
        return {**_cache["data"], "updated": mtime}