"""IND/CTA Data Quality page (DQE, 4th tab).

Reads the IND/CTA DQ history view in Snowflake. The view is SCD2: every issue
(SOURCE_PK + DQ_IDENTIFIER) can have several versions, the live one has
DB_IS_CURRENT = TRUE. When an issue stops showing up its last row gets closed,
so "no current row" means the issue was fixed.

Config (optional) lives in the "ind_cta_dq" section of dq_config.json:
    table              fully qualified view name
    cache_seconds      how long summary results stay cached
    role / conn_id     which Data Studio login and role to use (default login,
                       first role that can read the view)
    login: "bdc"       use the Business Checks login instead
    account/user/...   a separate Snowflake login just for this page
"""
from __future__ import annotations

import csv
import io
import logging
import re
import textwrap
import threading
import time
from pathlib import Path
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from audit import write_audit_event
from auth import require_permission

logger = logging.getLogger(__name__)

ind_cta_dq_router = APIRouter(prefix="/dq/ind-cta", tags=["IND/CTA Data Quality"])

DEFAULT_TABLE = '"PRD_RND_INTEGRATED_DATA_DW"."L3__RDH_MDM_PUBLISH"."VW_MDM_DQ_HIST__L3__RDH_MDM_PUBLISH"'

_cfg: Dict[str, Any] = {}
_bdc_manager = None
_table = DEFAULT_TABLE
_cache_ttl = 300

_cache: Dict[str, Tuple[float, Any]] = {}
_cache_lock = threading.Lock()

# columns the issues grid can sort on
SORTABLE = {
    "source_pk": "SOURCE_PK", "rule": "DQ_IDENTIFIER", "group": "DQ_GROUP", "name": "DQ_NAME",
    "first_seen": "FIRST_SEEN", "updated": "DL_LAST_UPDATED_DATE", "resolved_at": "RESOLVED_AT",
    "name_score": "NAME_SIMILARITY_SCORE", "semantic_score": "SEMANTIC_SIMILARITY_SCORE",
    "edit_distance": "EDIT_DISTANCE", "overlap": "WORD_OVERLAP_RATIO",
    "confidence": "DQ_CONFIDENCE_REASON", "versions": "VERSIONS",
}

ISSUE_COLS = [
    "SOURCE_PK", "DQ_IDENTIFIER", "DQ_GROUP", "DQ_NAME", "DQ_DESCRIPTION", "DQ_DATA_DETAIL",
    "NAME_SIMILARITY_SCORE", "EDIT_DISTANCE", "SEMANTIC_SIMILARITY_SCORE", "WORD_OVERLAP_RATIO",
    "DQ_CONFIDENCE_REASON", "DL_ID", "DL_CREATED_DATE", "DL_LAST_UPDATED_DATE",
    "DB_ROW_START_DATE", "DB_ROW_END_DATE", "DB_IS_CURRENT", "STATUS", "FIRST_SEEN", "RESOLVED_AT", "VERSIONS",
]

AGE_SQL = "DATEDIFF(day, FIRST_SEEN, CURRENT_TIMESTAMP())"
# bucket -> (more than, at most) days since FIRST_SEEN
AGE_BUCKETS = {"0-7": (None, 7), "8-30": (7, 30), "31-90": (30, 90), "90+": (90, None)}
AGE_PATTERN = r"^(0-7|8-30|31-90|90\+)$"

_IDENT = re.compile(r'^("[^"]+"|[A-Za-z_][A-Za-z0-9_$]*)$')


def _check_table(name: str) -> str:
    parts = [p.strip() for p in name.split(".")]
    if len(parts) != 3 or not all(_IDENT.match(p) for p in parts):
        raise ValueError(f"ind_cta_dq.table must be DATABASE.SCHEMA.VIEW, got {name!r}")
    return ".".join(parts)


def set_ind_cta_dq_dependencies(config: Optional[Dict[str, Any]], bdc_connection_manager=None):
    global _cfg, _bdc_manager, _table, _cache_ttl
    _cfg = config or {}
    _bdc_manager = bdc_connection_manager
    _table = _check_table(_cfg.get("table") or DEFAULT_TABLE)
    _cache_ttl = int(_cfg.get("cache_seconds", 300))
    logger.info(f"IND/CTA DQ reads {_table}")


# ── connection ───────────────────────────────────────────────────────────────
#
# Which Snowflake login reads the view, first match wins:
#   1. ind_cta_dq has its own account/user/key          -> that login
#   2. ind_cta_dq.login == "bdc"                         -> the Business Checks login
#   3. otherwise                                     -> the Data Studio login
#      (ind_cta_dq.conn_id picks a numbered one, ind_cta_dq.role pins the role,
#       no role = try each role the login has until one can read the view)

def _login_options() -> List[Dict[str, Any]]:
    if _cfg.get("account"):
        return [{
            "label": f"ind_cta_dq {_cfg.get('user')}", "account": _cfg["account"], "user": _cfg["user"],
            "password": _cfg.get("password"), "private_key_path": _cfg.get("private_key_path"),
            "private_key_password": _cfg.get("private_key_password") or "",
            "role": _cfg.get("role"), "warehouse": _cfg.get("warehouse"),
        }]
    import snowflake_query_runner as sqr
    if not sqr._configured():
        return []
    conn_id = _cfg.get("conn_id")
    prof = sqr.conn_profile(conn_id, None)
    cid = prof[len(sqr.CONN_PREFIX):].partition("|")[0] if prof.startswith(sqr.CONN_PREFIX) else "default"
    base = sqr._numbered_config(cid, None)   # this one carries the login's roles list
    roles = [_cfg["role"]] if _cfg.get("role") else []
    if not roles:
        for r in [base.get("role"), *(base.get("roles") or [])]:
            if r and r.upper() not in {x.upper() for x in roles}:
                roles.append(r)
    out = []
    for r in roles or [None]:
        c = sqr._profile_config(sqr.conn_profile(conn_id, r)) if r else base
        out.append({**c, "label": f"Data Studio {c.get('user')}/{c.get('role') or 'default'}",
                    "warehouse": _cfg.get("warehouse") or c.get("warehouse")})
    return out


def _open(login: Dict[str, Any]):
    import snowflake.connector
    from app import SnowflakeClient

    kw = {
        "account": login["account"], "user": login["user"], "application": "SmartHub_IND_CTA_DQ",
        "login_timeout": 30, "session_parameters": {"STATEMENT_TIMEOUT_IN_SECONDS": 180},
    }
    if login.get("private_key_path"):
        import snowflake_query_runner as sqr
        if not Path(login["private_key_path"]).exists():
            raise HTTPException(503, f"IND/CTA DQ private key not found at {login['private_key_path']}")
        kw["private_key"] = sqr._load_private_key(login["private_key_path"], login.get("private_key_password") or "")
    elif login.get("password"):
        kw["password"] = login["password"]
    else:
        raise HTTPException(503, f"{login['label']} has no password or private key")
    for k in ("role", "warehouse"):
        if login.get(k):
            kw[k] = login[k]
    return SnowflakeClient(snowflake.connector.connect(**kw))


class _Connection:
    TTL = 2 * 60 * 60

    def __init__(self):
        self._client = None
        self._made = 0.0
        self._login = None   # the login/role that worked last time
        self._lock = threading.Lock()

    def reset(self):
        with self._lock:
            self._drop()
            self._login = None

    def _drop(self):
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None

    def get(self):
        with self._lock:
            if self._client and time.time() - self._made < self.TTL:
                try:
                    self._client.execute("SELECT 1")
                    return self._client
                except Exception:
                    logger.info("IND/CTA DQ connection dropped, reconnecting")
            self._drop()
            options = [self._login] if self._login else _login_options()
            if not options:
                raise HTTPException(503, "IND/CTA DQ has no Snowflake login. Add a snowflake_query_runner or ind_cta_dq section to dq_config.json")
            tried = []
            for login in options:
                try:
                    client = _open(login)
                except HTTPException:
                    raise
                except Exception as e:
                    tried.append(f"{login['label']}: can't connect ({str(e)[:120]})")
                    continue
                if len(options) > 1 or not self._login:
                    try:
                        client.execute(f"SELECT 1 FROM {_table} LIMIT 1")
                    except Exception:
                        tried.append(f"{login['label']}: no access")
                        try:
                            client.close()
                        except Exception:
                            pass
                        continue
                self._client, self._made, self._login = client, time.time(), login
                logger.info(f"IND/CTA DQ reading {_table} as {login['label']}")
                return client
            raise HTTPException(503, f"No Snowflake role here can read the IND/CTA view. Tried {'; '.join(tried)}. "
                                     "Set ind_cta_dq.role in dq_config.json to a role that has SELECT on it.")

    def who(self):
        return self._login["label"] if self._login else None


_conn = _Connection()


def _client():
    if _cfg.get("login") == "bdc" and not _cfg.get("account"):
        if _bdc_manager is None:
            raise HTTPException(503, "IND/CTA DQ is set to the BDC login but BDC isn't configured")
        return _bdc_manager.get_client()
    return _conn.get()


def _literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return f"'{v.isoformat()}'"
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def _show(sql: str, params: Optional[Dict[str, Any]] = None) -> str:
    """The query exactly as Snowflake gets it, values filled in, so it can be
    pasted into a worksheet and give the same rows."""
    text = re.sub(r"%\((\w+)\)s", lambda m: _literal((params or {}).get(m.group(1))), sql)
    base = _base()
    if text.startswith(base):
        text = base + textwrap.dedent(text[len(base):]).strip()
    else:
        text = textwrap.dedent(text).strip()
    return text + ";"


def _run_as() -> Dict[str, Optional[str]]:
    login = _conn._login or {}
    if _cfg.get("login") == "bdc" and not _cfg.get("account") and _bdc_manager is not None:
        try:
            sf = _bdc_manager.config_manager.get_snowflake_config()
            return {"role": sf.get("role"), "warehouse": sf.get("warehouse")}
        except Exception:
            return {"role": None, "warehouse": None}
    return {"role": login.get("role"), "warehouse": login.get("warehouse")}


def _run(sql: str, params: Optional[Dict[str, Any]] = None, log: Optional[Dict[str, Any]] = None,
         name: str = "", title: str = "", note: str = "") -> List[Dict[str, Any]]:
    if log is not None:
        log[name] = {"title": title, "note": note, "sql": _show(sql, params)}
    try:
        res = _client().execute(sql, params or {})
    except HTTPException:
        raise
    except Exception as e:
        msg = str(e)
        logger.error(f"IND/CTA DQ query failed: {msg}")
        if "does not exist or not authorized" in msg:
            who = _conn.who() or "The Snowflake login"
            _conn.reset()
            raise HTTPException(503, f"{who} can't read the IND/CTA view. Set ind_cta_dq.role in dq_config.json to a role that can.")
        raise HTTPException(500, f"Snowflake query failed: {msg[:300]}")
    cols = [c.upper() for c in res.columns]
    return [dict(zip(cols, r)) for r in res.rows]


def _cached(key: str, fn, refresh: bool, ttl: Optional[int] = None):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and not refresh and now - hit[0] < (_cache_ttl if ttl is None else ttl):
            return hit[1]
    val = fn()
    with _cache_lock:
        if len(_cache) > 500:   # facet keys include free text, so drop stale ones now and then
            oldest = time.time() - max(_cache_ttl, 60)
            for k in [k for k, (t, _) in _cache.items() if t < oldest]:
                _cache.pop(k, None)
        _cache[key] = (time.time(), val)
    return val


# ── SQL ──────────────────────────────────────────────────────────────────────

def _base() -> str:
    # one row per issue: its latest version, plus when it was first seen and
    # how many versions it has had
    return textwrap.dedent(f"""
    WITH v AS (
      SELECT t.*,
             MIN(t.DL_CREATED_DATE) OVER (PARTITION BY t.SOURCE_PK, t.DQ_IDENTIFIER) AS FIRST_SEEN,
             COUNT(*) OVER (PARTITION BY t.SOURCE_PK, t.DQ_IDENTIFIER) AS VERSIONS,
             MAX(IFF(t.DB_IS_CURRENT, 1, 0)) OVER (PARTITION BY t.SOURCE_PK, t.DQ_IDENTIFIER) AS HAS_CURRENT
      FROM {_table} t
      QUALIFY ROW_NUMBER() OVER (
        PARTITION BY t.SOURCE_PK, t.DQ_IDENTIFIER
        ORDER BY t.DB_IS_CURRENT DESC, t.DB_ROW_START_DATE DESC, t.DL_LAST_UPDATED_DATE DESC) = 1
    ),
    issues AS (
      SELECT v.*,
             IFF(HAS_CURRENT = 1, 'OPEN', 'RESOLVED') AS STATUS,
             IFF(HAS_CURRENT = 1, NULL, TO_TIMESTAMP_NTZ(DB_ROW_END_DATE)) AS RESOLVED_AT
      FROM v
    )
    """).lstrip()


def _cols(cols: List[str]) -> str:
    return ",\n             ".join(cols)


def _num(x):
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _iso(x):
    return x.isoformat() if hasattr(x, "isoformat") else x


def _clean(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in row.items():
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif v is not None and k in ("NAME_SIMILARITY_SCORE", "SEMANTIC_SIMILARITY_SCORE", "WORD_OVERLAP_RATIO"):
            v = float(v)
        elif v is not None and k in ("EDIT_DISTANCE", "VERSIONS"):
            v = int(v)
        out[k] = v
    return out


def _summary(weeks: int) -> Dict[str, Any]:
    t0 = time.time()
    base = _base()
    sql: Dict[str, Any] = {}

    kpi = _run(base + """
      SELECT
        COUNT_IF(STATUS = 'OPEN')                                    AS OPEN_ISSUES,
        COUNT_IF(STATUS = 'RESOLVED')                                AS RESOLVED_ISSUES,
        COUNT(DISTINCT IFF(STATUS = 'OPEN', SOURCE_PK, NULL))        AS AFFECTED_RECORDS,
        COUNT(DISTINCT IFF(STATUS = 'OPEN', DQ_IDENTIFIER, NULL))    AS ACTIVE_RULES,
        COUNT(DISTINCT DQ_IDENTIFIER)                                AS TOTAL_RULES,
        COUNT_IF(STATUS = 'OPEN' AND FIRST_SEEN >= DATEADD(day, -7, CURRENT_TIMESTAMP()))  AS NEW_7D,
        COUNT_IF(STATUS = 'OPEN' AND FIRST_SEEN >= DATEADD(day, -14, CURRENT_TIMESTAMP())
                 AND FIRST_SEEN < DATEADD(day, -7, CURRENT_TIMESTAMP()))                  AS NEW_PREV_7D,
        COUNT_IF(RESOLVED_AT >= DATEADD(day, -30, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ))     AS RESOLVED_30D,
        COUNT_IF(RESOLVED_AT >= DATEADD(day, -60, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)
                 AND RESOLVED_AT < DATEADD(day, -30, CURRENT_TIMESTAMP()::TIMESTAMP_NTZ))  AS RESOLVED_PREV_30D,
        AVG(IFF(STATUS = 'OPEN', DATEDIFF(day, FIRST_SEEN, CURRENT_TIMESTAMP()), NULL))   AS AVG_AGE_DAYS,
        MEDIAN(IFF(STATUS = 'RESOLVED', DATEDIFF(day, FIRST_SEEN, RESOLVED_AT), NULL))    AS MEDIAN_DAYS_TO_FIX,
        MAX(DL_LAST_UPDATED_DATE)                                    AS LAST_LOAD,
        MIN(FIRST_SEEN)                                              AS FIRST_LOAD
      FROM issues
    """, log=sql, name="kpis", title="Headline numbers",
        note="Open, new, fixed, records affected, rules firing and age. The fix rate ring is RESOLVED_ISSUES / (OPEN_ISSUES + RESOLVED_ISSUES).")[0]

    # one pass gives rules, groups and confidence
    rows = _run(base + """
      SELECT DQ_IDENTIFIER, DQ_GROUP, DQ_NAME, ANY_VALUE(DQ_DESCRIPTION) AS DQ_DESCRIPTION,
             COALESCE(DQ_CONFIDENCE_REASON, '') AS CONF,
             COUNT_IF(STATUS = 'OPEN')     AS OPEN_N,
             COUNT_IF(STATUS = 'RESOLVED') AS RESOLVED_N,
             COUNT_IF(STATUS = 'OPEN' AND FIRST_SEEN >= DATEADD(day, -7, CURRENT_TIMESTAMP())) AS NEW_7D,
             AVG(IFF(STATUS = 'OPEN', NAME_SIMILARITY_SCORE, NULL))     AS AVG_NAME,
             AVG(IFF(STATUS = 'OPEN', SEMANTIC_SIMILARITY_SCORE, NULL)) AS AVG_SEM,
             MAX(DL_LAST_UPDATED_DATE) AS LAST_SEEN
      FROM issues
      GROUP BY DQ_IDENTIFIER, DQ_GROUP, DQ_NAME, CONF
      ORDER BY OPEN_N DESC
    """, log=sql, name="rules", title="Rules, groups and confidence",
        note="One row per rule and confidence band. The page adds these up per rule (Rule hotspots), per DQ_GROUP (Open by group) and per band (Name match confidence).")

    rules: Dict[str, Dict[str, Any]] = {}
    groups: Dict[str, Dict[str, int]] = {}
    conf: Dict[str, int] = {}
    for r in rows:
        rid = r["DQ_IDENTIFIER"]
        rule = rules.setdefault(rid, {
            "id": rid, "group": r["DQ_GROUP"], "name": r["DQ_NAME"], "description": r["DQ_DESCRIPTION"],
            "open": 0, "resolved": 0, "new_7d": 0, "confidence": {},
            "_name": [], "_sem": [], "last_seen": None,
        })
        o, res, n7 = int(r["OPEN_N"] or 0), int(r["RESOLVED_N"] or 0), int(r["NEW_7D"] or 0)
        rule["open"] += o
        rule["resolved"] += res
        rule["new_7d"] += n7
        if r["CONF"] and o:
            rule["confidence"][r["CONF"]] = rule["confidence"].get(r["CONF"], 0) + o
            conf[r["CONF"]] = conf.get(r["CONF"], 0) + o
        if _num(r["AVG_NAME"]) is not None:
            rule["_name"].append((_num(r["AVG_NAME"]), o))
        if _num(r["AVG_SEM"]) is not None:
            rule["_sem"].append((_num(r["AVG_SEM"]), o))
        seen = _iso(r["LAST_SEEN"])
        if seen and (not rule["last_seen"] or seen > rule["last_seen"]):
            rule["last_seen"] = seen
        g = groups.setdefault(r["DQ_GROUP"] or "OTHER", {"open": 0, "resolved": 0, "rules": set()})
        g["open"] += o
        g["resolved"] += res
        g["rules"].add(rid)

    def wavg(pairs):
        tot = sum(w for _, w in pairs)
        return round(sum(v * w for v, w in pairs) / tot, 4) if tot else None

    rule_list = []
    for rule in rules.values():
        rule["avg_name_score"] = wavg(rule.pop("_name"))
        rule["avg_semantic_score"] = wavg(rule.pop("_sem"))
        total = rule["open"] + rule["resolved"]
        rule["fix_rate"] = round(rule["resolved"] / total, 4) if total else None
        rule_list.append(rule)
    rule_list.sort(key=lambda x: (-x["open"], x["id"]))

    group_list = sorted(
        ({"group": k, "open": v["open"], "resolved": v["resolved"], "rules": len(v["rules"])} for k, v in groups.items()),
        key=lambda x: -x["open"])

    trend_rows = _run(base + """
      SELECT 'NEW' AS KIND, DATE_TRUNC('week', FIRST_SEEN)::DATE AS WK, DQ_GROUP, COUNT(*) AS N
      FROM issues WHERE FIRST_SEEN >= DATEADD(week, -%(weeks)s, CURRENT_DATE())
      GROUP BY 1, 2, 3
      UNION ALL
      SELECT 'RESOLVED', DATE_TRUNC('week', RESOLVED_AT)::DATE, DQ_GROUP, COUNT(*)
      FROM issues WHERE RESOLVED_AT >= DATEADD(week, -%(weeks)s, CURRENT_DATE())
      GROUP BY 1, 2, 3
      ORDER BY 2, 1
    """, {"weeks": weeks}, log=sql, name="trend", title="Issue flow",
        note="New issues per week by first seen date, fixed issues per week by the date their last row was closed. The backlog line starts from today's open count and walks back week by week.")

    trend = _build_trend(trend_rows, int(kpi["OPEN_ISSUES"] or 0))

    # sample for the similarity scatter (only rules that carry scores)
    scatter = _run(base + """
      SELECT SOURCE_PK, DQ_IDENTIFIER, DQ_CONFIDENCE_REASON,
             NAME_SIMILARITY_SCORE, SEMANTIC_SIMILARITY_SCORE, EDIT_DISTANCE, WORD_OVERLAP_RATIO,
             LEFT(DQ_DATA_DETAIL, 400) AS DQ_DATA_DETAIL
      FROM issues
      WHERE STATUS = 'OPEN' AND NAME_SIMILARITY_SCORE IS NOT NULL AND SEMANTIC_SIMILARITY_SCORE IS NOT NULL
      ORDER BY RANDOM()
      LIMIT 1200
    """, log=sql, name="scatter", title="Name match map",
        note="A random sample of 1,200 open issues that have both scores, so each run gives a slightly different set of dots.")

    aging = _run(base + f"""
      , aged AS (
        SELECT STATUS, FIRST_SEEN, {AGE_SQL} AS AGE, MIN(FIRST_SEEN) OVER () AS FIRST_LOAD
        FROM issues
      )
      SELECT
        COUNT_IF(AGE <= 7)              AS AGE_0_7,
        COUNT_IF(AGE > 7 AND AGE <= 30)  AS AGE_8_30,
        COUNT_IF(AGE > 30 AND AGE <= 90) AS AGE_31_90,
        COUNT_IF(AGE > 90)              AS AGE_90_PLUS,
        COUNT_IF(FIRST_SEEN < DATEADD(day, 1, FIRST_LOAD)) AS FIRST_LOAD_OPEN
      FROM aged
      WHERE STATUS = 'OPEN'
    """, log=sql, name="aging", title="How old are open issues",
        note="Open issues bucketed by days since FIRST_SEEN. FIRST_LOAD_OPEN counts the ones that came in within a day of the very first load.")[0]

    for r in _run(base + """
      SELECT DQ_GROUP, COUNT(DISTINCT SOURCE_PK) AS RECORDS
      FROM issues
      WHERE STATUS = 'OPEN'
      GROUP BY DQ_GROUP
    """, log=sql, name="group_records", title="Records per group",
        note="Distinct Source PKs with at least one open issue, per DQ_GROUP. A record can sit in both groups."):
        g = next((x for x in group_list if x["group"] == (r["DQ_GROUP"] or "OTHER")), None)
        if g is not None:
            g["affected_records"] = int(r["RECORDS"] or 0)
    for g in group_list:
        g.setdefault("affected_records", 0)

    # weeks are counted back from this week's Monday so the last slot is the current week
    spark_rows = _run(base + """
      SELECT DQ_IDENTIFIER,
             DATEDIFF(day, DATE_TRUNC('week', FIRST_SEEN)::DATE, DATE_TRUNC('week', CURRENT_DATE())) / 7 AS WEEKS_AGO,
             COUNT(*) AS N
      FROM issues
      WHERE FIRST_SEEN >= DATEADD(week, -11, DATE_TRUNC('week', CURRENT_DATE()))
      GROUP BY 1, 2
    """, log=sql, name="spark", title="New issues per rule, last 12 weeks",
        note="One row per rule and week. The Rules tab draws these as the 12-week trend line.")
    for rule in rule_list:
        rule["spark"] = [0] * 12
    by_id = {r["id"]: r for r in rule_list}
    for r in spark_rows:
        ago = int(r["WEEKS_AGO"] or 0)
        rule = by_id.get(r["DQ_IDENTIFIER"])
        if rule is not None and 0 <= ago < 12:
            rule["spark"][11 - ago] += int(r["N"] or 0)

    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "kpis": {
            "open_issues": int(kpi["OPEN_ISSUES"] or 0),
            "resolved_issues": int(kpi["RESOLVED_ISSUES"] or 0),
            "affected_records": int(kpi["AFFECTED_RECORDS"] or 0),
            "active_rules": int(kpi["ACTIVE_RULES"] or 0),
            "total_rules": int(kpi["TOTAL_RULES"] or 0),
            "new_7d": int(kpi["NEW_7D"] or 0),
            "new_prev_7d": int(kpi["NEW_PREV_7D"] or 0),
            "resolved_30d": int(kpi["RESOLVED_30D"] or 0),
            "resolved_prev_30d": int(kpi["RESOLVED_PREV_30D"] or 0),
            "first_load_open": int(aging["FIRST_LOAD_OPEN"] or 0),
            "avg_age_days": _num(kpi["AVG_AGE_DAYS"]),
            "median_days_to_fix": _num(kpi["MEDIAN_DAYS_TO_FIX"]),
            "last_load": _iso(kpi["LAST_LOAD"]),
            "first_load": _iso(kpi["FIRST_LOAD"]),
        },
        "groups": group_list,
        "rules": rule_list,
        "confidence": [{"label": k, "count": v} for k, v in sorted(conf.items(), key=lambda x: -x[1])],
        "aging": [
            {"bucket": "0-7", "count": int(aging["AGE_0_7"] or 0)},
            {"bucket": "8-30", "count": int(aging["AGE_8_30"] or 0)},
            {"bucket": "31-90", "count": int(aging["AGE_31_90"] or 0)},
            {"bucket": "90+", "count": int(aging["AGE_90_PLUS"] or 0)},
        ],
        "trend": trend,
        "scatter": [_clean(r) for r in scatter],
        "sql": sql,
        "run_as": _run_as(),
    }


def _build_trend(rows: List[Dict[str, Any]], open_now: int) -> List[Dict[str, Any]]:
    weeks: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        wk = _iso(r["WK"])
        if not wk:
            continue
        w = weeks.setdefault(wk, {"week": wk, "new": 0, "resolved": 0, "by_group": {}})
        n = int(r["N"] or 0)
        key = "new" if r["KIND"] == "NEW" else "resolved"
        w[key] += n
        g = w["by_group"].setdefault(r["DQ_GROUP"] or "OTHER", {"new": 0, "resolved": 0})
        g[key] += n
    out = sorted(weeks.values(), key=lambda x: x["week"])
    # walk back from today's backlog to get the backlog at the end of each week
    backlog = open_now
    for w in reversed(out):
        w["backlog"] = backlog
        backlog = backlog - w["new"] + w["resolved"]
    return out


def _age_sql(bucket: str) -> str:
    lo, hi = AGE_BUCKETS[bucket]
    parts = []
    if lo is not None:
        parts.append(f"{AGE_SQL} > {lo}")
    if hi is not None:
        parts.append(f"{AGE_SQL} <= {hi}")
    return " AND ".join(parts)


def _conditions(status: str, group: Optional[str], rule: Optional[str], confidence: Optional[str],
                q: Optional[str], days: Optional[int], age: Optional[str] = None
                ) -> Tuple[Dict[str, str], Dict[str, Any]]:
    """One SQL condition per filter, keyed by filter name, plus the bind values.
    Kept apart so /facets can drop one filter at a time."""
    cond, p = {}, {}
    if status in ("open", "resolved"):
        cond["status"] = "STATUS = %(status)s"
        p["status"] = status.upper()
    if group:
        cond["group"] = "DQ_GROUP = %(group)s"
        p["group"] = group
    if rule:
        ids = [x.strip() for x in rule.split(",") if x.strip()][:50]
        keys = []
        for i, x in enumerate(ids):
            keys.append(f"%(rule{i})s")
            p[f"rule{i}"] = x
        if keys:
            cond["rule"] = f"DQ_IDENTIFIER IN ({', '.join(keys)})"
    if confidence:
        if confidence == "NONE":
            cond["confidence"] = "DQ_CONFIDENCE_REASON IS NULL"
        else:
            cond["confidence"] = "DQ_CONFIDENCE_REASON = %(conf)s"
            p["conf"] = confidence
    if age in AGE_BUCKETS:
        cond["age"] = _age_sql(age)
    if q:
        cond["q"] = ("(SOURCE_PK ILIKE %(q)s OR DQ_DATA_DETAIL ILIKE %(q)s OR DQ_NAME ILIKE %(q)s "
                     "OR DQ_DESCRIPTION ILIKE %(q)s OR DQ_IDENTIFIER ILIKE %(q)s)")
        p["q"] = f"%{q.strip()[:200]}%"
    if days:
        cond["days"] = "FIRST_SEEN >= DATEADD(day, -%(days)s, CURRENT_TIMESTAMP())"
        p["days"] = int(days)
    return cond, p


def _filters(status: str, group: Optional[str], rule: Optional[str], confidence: Optional[str],
             q: Optional[str], days: Optional[int], age: Optional[str] = None) -> Tuple[str, Dict[str, Any]]:
    cond, p = _conditions(status, group, rule, confidence, q, days, age)
    where = list(cond.values())
    return ("WHERE " + "\n        AND ".join(where)) if where else "", p


FACETS = {   # facet -> (filter it ignores, column it counts by)
    "status": ("status", "STATUS"),
    "group": ("group", "DQ_GROUP"),
    "confidence": ("confidence", "CONF"),
    "rule": ("rule", "DQ_IDENTIFIER"),
    "age": ("age", "AGE_BUCKET"),
}


def _facets(status, group, rule, confidence, q, days, age) -> Dict[str, Any]:
    t0 = time.time()
    cond, p = _conditions(status, group, rule, confidence, q, days, age)
    # search and first-seen are not facets, so they always apply
    shared = [cond.pop(k) for k in ("q", "days") if k in cond]
    where = ("WHERE " + "\n        AND ".join(shared)) if shared else ""

    counts = []
    for name, (skip, _) in FACETS.items():
        rest = [c for k, c in cond.items() if k != skip]
        expr = " AND ".join(f"({c})" for c in rest) if rest else "TRUE"
        counts.append(f"COUNT_IF({expr}) AS N_{name.upper()}")
    flags = [f"GROUPING({col}) AS G_{name.upper()}" for name, (_, col) in FACETS.items()]
    sets = ", ".join(f"({col})" for _, col in FACETS.values())
    cols = ", ".join(col for _, col in FACETS.values())

    log: Dict[str, Any] = {}
    rows = _run(_base() + f"""
      , f AS (
        SELECT STATUS, DQ_GROUP, DQ_IDENTIFIER, DQ_CONFIDENCE_REASON, FIRST_SEEN,
               COALESCE(DQ_CONFIDENCE_REASON, 'NONE') AS CONF,
               CASE WHEN {AGE_SQL} <= 7 THEN '0-7'
                    WHEN {AGE_SQL} <= 30 THEN '8-30'
                    WHEN {AGE_SQL} <= 90 THEN '31-90'
                    ELSE '90+' END AS AGE_BUCKET
        FROM issues
        {where}
      )
      SELECT {cols},
             {_cols(flags)},
             {_cols(counts)}
      FROM f
      GROUP BY GROUPING SETS ({sets})
    """, p, log=log, name="facets", title="Filter counts",
        note="One pass that counts each filter option. Every section's counts use all the other filters, but not its own.")

    out: Dict[str, Dict[str, int]] = {name: {} for name in FACETS}
    for r in rows:
        for name, (_, col) in FACETS.items():
            if int(r[f"G_{name.upper()}"] or 0) == 0:
                key = r[col] if r[col] is not None else "OTHER"
                out[name][str(key)] = out[name].get(str(key), 0) + int(r[f"N_{name.upper()}"] or 0)
                break
    return {**out, "elapsed_ms": int((time.time() - t0) * 1000), "sql": log["facets"], "run_as": _run_as()}


# ── endpoints ────────────────────────────────────────────────────────────────

_viewer = require_permission("viewIndCtaQuality")


def _hide_sql(out: Dict[str, Any], auth: dict) -> Dict[str, Any]:
    # SQL and the Snowflake role only go to people with View SQL
    if (auth.get("permissions") or {}).get("viewIndCtaSql") is True:
        return out
    return {k: v for k, v in out.items() if k not in ("sql", "run_as")}


@ind_cta_dq_router.get("/summary")
def ind_cta_summary(weeks: int = Query(26, ge=4, le=104), refresh: bool = False, auth: dict = Depends(_viewer)):
    return _hide_sql(_cached(f"summary:{weeks}", lambda: _summary(weeks), refresh), auth)


@ind_cta_dq_router.get("/issues")
def ind_cta_issues(
    status: str = Query("open", pattern="^(open|resolved|all)$"),
    group: Optional[str] = None,
    rule: Optional[str] = None,
    confidence: Optional[str] = None,
    q: Optional[str] = None,
    days: Optional[int] = Query(None, ge=1, le=3650),
    age: Optional[str] = Query(None, pattern=AGE_PATTERN),
    sort: str = "first_seen",
    dir: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=10, le=500),
    auth: dict = Depends(_viewer),
):
    col = SORTABLE.get(sort, "FIRST_SEEN")
    where, p = _filters(status, group, rule, confidence, q, days, age)
    p.update({"lim": page_size, "off": (page - 1) * page_size})
    t0 = time.time()
    log: Dict[str, Any] = {}
    rows = _run(_base() + f"""
      SELECT {_cols(ISSUE_COLS)},
             COUNT(*) OVER () AS TOTAL_ROWS
      FROM issues
      {where}
      ORDER BY {col} {dir.upper()} NULLS LAST, SOURCE_PK
      LIMIT %(lim)s OFFSET %(off)s
    """, p, log=log, name="issues", title="Issue explorer",
        note="This page of results with your filters and sort. TOTAL_ROWS is the full match count. Drop the LIMIT / OFFSET line to get every row.")
    total = int(rows[0]["TOTAL_ROWS"]) if rows else 0
    if not rows and page > 1:
        # page ran off the end, count separately so the pager can recover
        total = int(_run(_base() + f"SELECT COUNT(*) AS N FROM issues {where}", p)[0]["N"])
    for r in rows:
        r.pop("TOTAL_ROWS", None)
    return _hide_sql({
        "rows": [_clean(r) for r in rows], "total": total, "page": page, "page_size": page_size,
        "elapsed_ms": int((time.time() - t0) * 1000),
        "sql": log["issues"], "run_as": _run_as(),
    }, auth)


@ind_cta_dq_router.get("/facets")
def ind_cta_facets(
    status: str = Query("open", pattern="^(open|resolved|all)$"),
    group: Optional[str] = None,
    rule: Optional[str] = None,
    confidence: Optional[str] = None,
    q: Optional[str] = None,
    days: Optional[int] = Query(None, ge=1, le=3650),
    age: Optional[str] = Query(None, pattern=AGE_PATTERN),
    auth: dict = Depends(_viewer),
):
    key = "facets:" + "|".join(str(x or "") for x in (status, group, rule, confidence, q, days, age))
    return _hide_sql(_cached(key, lambda: _facets(status, group, rule, confidence, q, days, age), False, ttl=60), auth)


@ind_cta_dq_router.get("/history")
def ind_cta_history(source_pk: str, rule: str, auth: dict = Depends(_viewer)):
    log: Dict[str, Any] = {}
    cols = [c for c in ISSUE_COLS if c not in ("STATUS", "FIRST_SEEN", "RESOLVED_AT", "VERSIONS")]
    rows = _run(f"""
      SELECT {_cols(cols + ["DL_CREATED_BY", "DL_UPDATED_BY", "DB_UUID"])}
      FROM {_table}
      WHERE SOURCE_PK = %(pk)s AND DQ_IDENTIFIER = %(rule)s
      ORDER BY DB_ROW_START_DATE DESC
      LIMIT 200
    """, {"pk": source_pk, "rule": rule}, log=log, name="history", title="Issue history",
        note="Every version of this issue straight from the view, newest first. DB_IS_CURRENT = TRUE is the live row.")
    return _hide_sql({"versions": [_clean(r) for r in rows], "sql": log["history"], "run_as": _run_as()}, auth)


@ind_cta_dq_router.get("/export")
def ind_cta_export(
    request: Request,
    status: str = Query("open", pattern="^(open|resolved|all)$"),
    group: Optional[str] = None,
    rule: Optional[str] = None,
    confidence: Optional[str] = None,
    q: Optional[str] = None,
    days: Optional[int] = Query(None, ge=1, le=3650),
    age: Optional[str] = Query(None, pattern=AGE_PATTERN),
    auth: dict = Depends(_viewer),
):
    where, p = _filters(status, group, rule, confidence, q, days, age)
    rows = _run(_base() + f"""
      SELECT {', '.join(ISSUE_COLS)} FROM issues {where}
      ORDER BY DQ_IDENTIFIER, FIRST_SEEN DESC
      LIMIT 200000
    """, p)

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(ISSUE_COLS)
    for r in rows:
        w.writerow([_iso(r.get(c)) if r.get(c) is not None else "" for c in ISSUE_COLS])
    buf.seek(0)

    try:
        write_audit_event(
            action="ind_cta_dq_export",
            performed_by=(auth.get("user") or {}).get("email", "") or auth.get("email", ""),
            target=_table,
            details=f"{len(rows)} rows; status={status} group={group or ''} rule={rule or ''} age={age or ''} q={q or ''}",
            ip_address=request.client.host if request.client else "",
        )
    except Exception as e:
        logger.warning(f"IND/CTA DQ export audit failed: {e}")

    stamp = time.strftime("%Y%m%d_%H%M")
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="ind_cta_dq_{status}_{stamp}.csv"'},
    )


@ind_cta_dq_router.get("/status")
def ind_cta_status():
    return {"table": _table, "login": _conn.who(), "cache_seconds": _cache_ttl}