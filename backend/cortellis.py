"""Virtual Regulator -> External -> Cortellis.

Cortellis tables live in DEV_RAW.POC_CORE as RDH_*_PILL4R_DATA. Everything Cortellis
lives in this file (and CortellisExplorer.jsx on the page), so changes here don't
touch the other explorers. Same Snowflake connection + cache as unstructured.py.

Each table has a spec in TABLES saying which columns matter:
  title    - main text column for a record
  facets   - low-cardinality columns, become the charts + filters
  split    - facets that hold comma lists ("Oncology,CNS"), counted per item
  date     - column for the by-year chart (+ optional Snowflake date format)
  year     - use this numeric year column instead of a date
  grid     - default columns for the Records grid (All columns shows everything)
  search   - columns the search box looks in (all of them is too slow on 500k rows)
  text     - long text shown first in the record drawer
  url      - link column
  recent   - date column for "latest" / last 12 months when `date` isn't one (defaults to date)
  cross    - two facets for the heatmap (rows, columns)
  leaders  - high-cardinality column for the "top N" list (sponsor, company...), (col, label, split?)
  measures - numeric columns for the distribution cards, (col, label, unit)

Tables are big (RP_TRIALS ~570k rows) so filtering, search, sort and paging
all happen in Snowflake.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from auth import require_permission
from unstructured import _cache_get, _cache_key, _cache_set, _render_sql_for_display, _run, _to_json_safe

logger = logging.getLogger(__name__)

DB, SCHEMA = "DEV_RAW", "POC_CORE"
MAX_PAGE = 200
CELL_CUT = 600        # grid cells get cut, the drawer loads the full row
ROW_BATCH = 20        # grid rows per streamed line
TOP_VALUES = 12
YEAR_MIN, YEAR_MAX = 1950, 2040

_cortellis = lambda table, label, blurb: {  # noqa: E731  RWE + approval packages share a shape
    "label": label, "table": table, "blurb": blurb,
    "title": "title", "date": ("publishedOn", None), "url": None, "text": "abstract", "key": "id",
    "facets": ["docTypes", "source", "country", "versions", "ReasonForUpdate", "therapeuticArea"],
    "split": ["versions", "therapeuticArea"],
    "grid": ["number", "title", "docTypes", "source", "country", "products", "therapeuticArea",
             "indication", "publishedOn", "lastUpdatedDate"],
    "search": ["title", "products", "keywords", "indication", "number"],
    "cross": ("docTypes", "country"),
    "leaders": ("products", "Top products", True),
}

TABLES: Dict[str, Dict[str, Any]] = {
    "rp_trials": {
        "label": "RP Trials", "table": "RDH_RP_TRIALS_PILL4R_DATA",
        "cross": ("trialPhase", "trialStatus"),
        "leaders": ("trialSponsors", "Top sponsors", True),
        "measures": [("trailInfo_trialTargetAccrual", "Target accrual", "patients"),
                     ("trailInfo_trialTiming_totalTrialDuration", "Total trial duration", "months")],
        "blurb": "Clinical trials from TrialTrove",
        "title": "trialTitle", "date": ("trailInfo_trialStartDate", None), "url": "recordUrl",
        "text": "trailInfo_trialObjective", "key": "id",
        "facets": ["trialPhase", "trialStatus", "name", "trailInfo_trialRecordType",
                   "trailInfo_trialPatientCriteria_gender", "trailInfo_trialPatientCriteria_ageGroup"],
        "split": ["trailInfo_trialPatientCriteria_ageGroup"],
        "labels": {"name": "Therapeutic area", "trailInfo_trialRecordType": "Record type",
                   "trailInfo_trialPatientCriteria_gender": "Gender",
                   "trailInfo_trialPatientCriteria_ageGroup": "Age group"},
        "grid": ["trialId", "trialTitle", "trialPhase", "trialStatus", "name", "diseases",
                 "trialSponsors", "trialCountries", "startDate", "primaryCompletion", "recordUrl"],
        "search": ["trialTitle", "diseases", "trialSponsors", "trialId", "mechanismsOfAction"],
    },
    "rp_products": {
        "label": "RP Products", "table": "RDH_RP_PRODUCTS_PILL4R_DATA",
        "cross": ("origin", "developmentStatus"),
        "leaders": ("originatorName", "Top originators", False),
        "measures": [("molecularWeight", "Molecular weight", "g/mol"), ("firstLaunchYear", "First launch year", "year")],
        "blurb": "Drug pipeline from Pharmaprojects",
        "title": "drugPrimaryName", "year": "firstLaunchYear", "recent": ("latestChangeDate", None), "url": "recordUrl",
        "text": "overview", "key": "id",
        "facets": ["globalStatus", "developmentStatus", "origin", "originatorCountry", "orphanDrug", "nce"],
        "labels": {"nce": "New chemical entity", "firstLaunchYear": "First launch"},
        "grid": ["drugId", "drugPrimaryName", "globalStatus", "developmentStatus", "originatorName",
                 "indication", "mechanismsOfAction", "drugTherapeuticClasses", "firstLaunchYear",
                 "latestChangeDate", "recordUrl"],
        "search": ["drugPrimaryName", "drugNameSynonyms", "originatorName", "indication", "mechanismsOfAction"],
    },
    "cortellis_rwe": _cortellis("RDH_CORTELLIS_RWE_PILL4R_DATA", "Cortellis Real World Evidence",
                                "Approval packages flagged as real world evidence"),
    "rp_reg_milestone": {
        "label": "RP Regulatory Milestone", "table": "RDH_RP_REG_MILESTONE_PILL4R_DATA",
        "cross": ("eventPhase", "therapeuticArea"),
        "leaders": ("companyLead", "Top companies", False),
        "measures": [("likelihoodOfApproval", "Likelihood of approval", "%")],
        "blurb": "Regulatory events from BioMedtracker",
        "title": "brandName", "date": ("eventDate", None), "url": "recordUrl",
        "text": "eventAnalysis", "key": "id",
        "facets": ["eventType", "eventPhase", "drugPhase", "drugClassification", "therapeuticArea", "companyLeadType"],
        "grid": ["eventDate", "brandName", "genericName", "eventType", "eventPhase", "indication",
                 "companyLead", "therapeuticArea", "likelihoodOfApproval", "recordUrl"],
        "search": ["brandName", "genericName", "companyLead", "indication", "eventType"],
    },
    "rp_pmrc": {
        "label": "RP PMRC", "table": "RDH_RP_PRED_PMRC_PILL4R_DATA",
        "cross": ("status", "CDER_OR_CBER"),
        "leaders": ("APPLICANT", "Top applicants", False),
        "blurb": "Postmarketing requirements and commitments",
        "title": "PRODUCT", "date": ("NDA_BLA_APPROVAL_DATE", "MM/DD/YY"), "url": None,
        "text": "CMT_DESC", "key": "id",
        "facets": ["status", "CDER_OR_CBER", "SUBPART_FLAG", "CMT_DOC_TYPE"],
        "labels": {"CDER_OR_CBER": "Center", "SUBPART_FLAG": "Subpart", "CMT_DOC_TYPE": "Doc type",
                   "NDA_BLA_APPROVAL_DATE": "Approval date"},
        "grid": ["NDA_NUMBER", "PRODUCT", "APPLICANT", "status", "CMT_NUMBER", "CMT_DESC",
                 "NDA_BLA_APPROVAL_DATE", "FINAL_REPORT_DUE_DATE", "CDER_OR_CBER"],
        "search": ["PRODUCT", "APPLICANT", "CMT_DESC", "NDA_NUMBER"],
    },
    "reg_pred_meeting": {
        "label": "Reg Pred Meeting", "table": "RDH_REG_PRED_MEETING_PILL4R_DATA",
        "cross": ("CommitteeName", "VoteApproval"),
        "leaders": ("CompanyName", "Top companies", False),
        "blurb": "FDA advisory committee meetings and votes",
        "title": "ProductName", "date": ("ADCOMMDate", None), "url": None,
        "text": "FDALetterStatement", "key": "id",
        "facets": ["CommitteeName", "FDAActionName", "VoteApproval", "ApplicationType", "ReviewType1", "therapeuticArea"],
        "split": ["therapeuticArea"],
        "labels": {"CommitteeName": "Committee", "FDAActionName": "FDA action", "ReviewType1": "Review type",
                   "ADCOMMDate": "AdComm date"},
        "grid": ["ADCOMMDate", "ProductName", "ActiveIngredients", "CompanyName", "CommitteeName",
                 "ApplicationType", "VoteApproval", "VoteEfficacy", "VoteSafety", "FDAActionName", "FDADecisionDate"],
        "search": ["ProductName", "ActiveIngredients", "CompanyName", "Indication", "CommitteeName"],
    },
    "rp_safety_warnings": {
        "label": "RP Products Safety Warnings", "table": "RDH_RP_PRODUCTS_SAFETY_WARNINGS_PILL4R_DATA",
        "cross": ("origin", "globalStatus"),
        "leaders": ("originatorName", "Top originators", False),
        "blurb": "Regulatory safety warnings on launched drugs",
        "title": "drugPrimaryName", "date": ("eventDate", None), "url": "recordUrl",
        "text": "eventDetails", "key": "id",
        "facets": ["globalStatus", "origin", "originatorCountry", "developmentStatus", "deliveryMediums"],
        "grid": ["eventDate", "drugPrimaryName", "eventDetails", "originatorName", "indication",
                 "globalStatus", "mechanismsOfAction", "recordUrl"],
        "search": ["drugPrimaryName", "eventDetails", "originatorName", "indication"],
    },
    "hanews": {
        "label": "HA News", "table": "RDH_HANEWS_PILL4R_DATA",
        "cross": ("region", "document_type"),
        "leaders": ("country", "Top countries", False),
        "measures": [("confidenceLevel", "ML confidence", "%")],
        "blurb": "Health authority news, guidance and legislation",
        "title": "title", "date": ("publishedOn", None), "url": "externalUrl",
        "text": "summary", "key": "id",
        "facets": ["agency", "region", "document_type", "categories", "source", "docTypes"],
        "split": ["categories"],
        "labels": {"document_type": "Document type", "docTypes": "Doc types"},
        "grid": ["publishedOn", "title", "agency", "country", "region", "document_type", "categories",
                 "summary", "externalUrl"],
        "search": ["title", "summary", "agency", "keywords", "country"],
    },
    "cortellis_approval": _cortellis("RDH_CORTELLIS_APPROVAL_PACKAGE_PILL4R_DATA", "Cortellis Approval Package",
                                     "FDA, EMA and PMDA approval packages"),
}

cortellis_router = APIRouter(prefix="/api/cortellis", tags=["Cortellis"])
PERM = require_permission("viewUnstructured")


# ------------------------------------------------------------------ helpers

def _fq(spec) -> str:
    return f"{DB}.{SCHEMA}.{spec['table']}"


def _q(col: str) -> str:
    return '"' + col.replace('"', '""') + '"'


def _spec(tid: str):
    if tid not in TABLES:
        raise HTTPException(status_code=404, detail=f"Unknown Cortellis table '{tid}'")
    return TABLES[tid]


def _run_or_fail(sql, params, what):
    try:
        return _run(sql, params)
    except HTTPException:
        raise
    except FileNotFoundError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(f"Cortellis {what} failed: {e}")
        raise HTTPException(status_code=500, detail="Couldn't load this data. Please try again.")


def _merge(payload, part):
    # "rows+" style keys append to a list, the rest replace. page does the same
    for k, v in part.items():
        if k.endswith("+"):
            payload.setdefault(k[:-1], []).extend(v)
        else:
            payload[k] = v


def _stream(name, args, bypass, parts):
    """NDJSON, one line per finished piece so the page draws as data lands
    instead of waiting on the slowest query. cache hit = one line."""
    key = _cache_key(name, args)
    hit = None if bypass else _cache_get(key)

    def gen():
        if hit is not None:
            yield json.dumps({**hit, "cached": True}, default=str) + "\n"
            return
        payload = {}
        try:
            for part in parts():
                _merge(payload, part)
                yield json.dumps(part, default=str) + "\n"
        except HTTPException as e:
            yield json.dumps({"error": e.detail}) + "\n"
            return
        except Exception as e:
            logger.error(f"Cortellis {name} stream failed: {e}")
            yield json.dumps({"error": "Couldn't load this data. Please try again."}) + "\n"
            return
        _cache_set(key, payload)  # only whole payloads get cached

    # X-Accel-Buffering: stop nginx holding the lines back
    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _columns(spec, bypass: bool) -> List[str]:
    key = _cache_key("cortellis-cols", {"t": spec["table"]})
    hit = None if bypass else _cache_get(key)
    if hit is not None:
        return hit
    res = _run_or_fail(
        "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = %(s)s AND TABLE_NAME = %(t)s ORDER BY ORDINAL_POSITION",
        {"s": SCHEMA, "t": spec["table"]}, "columns")
    cols = [r[0] for r in res.rows]
    _cache_set(key, cols)
    return cols


def _year_sql(spec):
    if spec.get("year"):
        expr = f"TRY_TO_NUMBER({_q(spec['year'])})"
    elif spec.get("date"):
        col, fmt = spec["date"]
        expr = (f"YEAR(TRY_TO_DATE({_q(col)}, '{fmt}'))" if fmt
                else f"YEAR(TRY_TO_TIMESTAMP_NTZ(REPLACE({_q(col)}, 'Z', '')))")
    else:
        return None
    return expr


def _date_sql(spec):
    col, fmt = spec.get("recent") or spec.get("date") or (None, None)
    if not col:
        return None
    return (f"TRY_TO_DATE({_q(col)}, '{fmt}')" if fmt
            else f"TO_DATE(TRY_TO_TIMESTAMP_NTZ(REPLACE({_q(col)}, 'Z', '')))")


def _stack_facet(spec, cols):
    # first plain facet colours the year chart
    return next((c for c in spec["facets"] if c in cols and c not in spec.get("split", [])), None)


def _like_escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _split_sql(col):
    # "a, b,c" -> ",a,b,c," so one value can be matched with LIKE '%,v,%'
    return f"(',' || REGEXP_REPLACE({_q(col)}, '\\\\s*,\\\\s*', ',') || ',')"


def _where(spec, filters: Dict[str, List[str]], q: str, years, params: Dict[str, Any], skip=None) -> str:
    """Filters are {facet: [values]} (OR inside a facet, AND across facets).
    skip leaves one facet out so its own chart still shows the other options."""
    parts = []
    split = set(spec.get("split", []))
    for fi, (col, vals) in enumerate(filters.items()):
        if col == skip or not vals:
            continue
        ors = []
        for vi, v in enumerate(vals):
            p = f"f{fi}_{vi}"
            if col in split:
                params[p] = f"%,{_like_escape(v)},%"
                ors.append(f"{_split_sql(col)} LIKE %({p})s ESCAPE '\\\\'")
            else:
                params[p] = v
                ors.append(f"TRIM({_q(col)}::STRING) = %({p})s")
        parts.append("(" + " OR ".join(ors) + ")")
    if q:
        params["q"] = f"%{q}%"
        parts.append("(" + " OR ".join(f"{_q(c)}::STRING ILIKE %(q)s" for c in spec["search"]) + ")")
    ysql = _year_sql(spec)
    if years and ysql:
        params["y0"], params["y1"] = years
        parts.append(f"{ysql} BETWEEN %(y0)s AND %(y1)s")
    return (" WHERE " + " AND ".join(parts)) if parts else ""


def _parse_filters(spec, raw: str) -> Dict[str, List[str]]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="filters must be JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="filters must be an object")
    out = {}
    for col, vals in data.items():
        if col not in spec["facets"]:
            # old / hand-edited link, just skip it rather than break the page
            logger.info(f"Cortellis {spec['label']}: ignoring unknown filter '{col}'")
            continue
        if isinstance(vals, list) and vals:
            out[col] = [str(v)[:300] for v in vals[:50]]
    return out


def _parse_years(raw: str):
    if not raw:
        return None
    try:
        a, b = (int(x) for x in raw.split("-"))
    except ValueError:
        return None  # bad value from a link, show all years
    if not (1000 <= a <= 9999 and 1000 <= b <= 9999):
        return None
    return (min(a, b), max(a, b))


def _label(spec, col):
    return spec.get("labels", {}).get(col)


# ------------------------------------------------------------------ routes

@cortellis_router.get("/sources")
def get_sources(bypass_cache: bool = Query(False), auth: dict = Depends(PERM)):
    def parts():
        # cards straight away from config, counts follow
        yield {"sources": [{"id": tid, "label": s["label"], "blurb": s.get("blurb"), "table": _fq(s)}
                           for tid, s in TABLES.items()]}
        # ROW_COUNT from metadata, no COUNT(*) over 500k rows
        counts = {}
        try:
            names = ", ".join(f"'{s['table']}'" for s in TABLES.values())
            res = _run(f"SELECT TABLE_NAME, ROW_COUNT, LAST_ALTERED FROM {DB}.INFORMATION_SCHEMA.TABLES "
                       f"WHERE TABLE_SCHEMA = '{SCHEMA}' AND TABLE_NAME IN ({names})", {})
            counts = {r[0]: (_to_json_safe(r[1]), _to_json_safe(r[2])) for r in res.rows}
        except Exception as e:
            logger.warning(f"Cortellis row counts failed: {e}")
        out = []
        for tid, s in TABLES.items():
            rows, altered = counts.get(s["table"], (None, None))
            out.append({"id": tid, "label": s["label"], "blurb": s.get("blurb"), "table": _fq(s),
                        "rows": rows, "updated": altered})
        yield {"sources": out}
    return _stream("cortellis-sources", {}, bypass_cache, parts)


@cortellis_router.get("/meta/{tid}")
def get_meta(tid: str, bypass_cache: bool = Query(False), auth: dict = Depends(PERM)):
    """What the page needs to lay out a table: facets, labels, grid columns."""
    spec = _spec(tid)
    cols = _columns(spec, bypass_cache)
    labels = {c: _label(spec, c) for c in cols if _label(spec, c)}
    return {
        "id": tid, "label": spec["label"], "blurb": spec.get("blurb"), "table": _fq(spec),
        "facets": [f for f in spec["facets"] if f in cols],
        "grid": [c for c in spec["grid"] if c in cols],
        "columns": cols, "labels": labels,
        "title": spec["title"], "text": spec.get("text"), "url": spec.get("url"), "key": spec.get("key"),
        "search": spec["search"],
        "has_years": bool(_year_sql(spec)),
        "year_label": _label(spec, spec.get("year") or (spec.get("date") or [None])[0]) or "Year",
    }


@cortellis_router.get("/overview/{tid}")
def get_overview(
    tid: str,
    filters: str = Query(""),
    q: str = Query("", max_length=200),
    years: str = Query(""),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(PERM),
):
    spec = _spec(tid)
    f = _parse_filters(spec, filters)
    yrs = _parse_years(years)
    args = {"tid": tid, "f": f, "q": q.strip(), "y": yrs}

    def parts():
        t = _fq(spec)
        cols = set(_columns(spec, bypass_cache))
        params: Dict[str, Any] = {}
        total = _run_or_fail(f"SELECT COUNT(*) FROM {t}{_where(spec, f, args['q'], yrs, params)}",
                             params, "overview count").rows[0][0]
        yield {"id": tid, "total": total}

        # every facet in one round trip. each one skips its own filter so the
        # chart still shows what else you could tick.
        selects, params = [], {}
        split = set(spec.get("split", []))
        for i, col in enumerate(c for c in spec["facets"] if c in cols):
            w = _where(spec, f, args["q"], yrs, params, skip=col)
            if col in split:
                src = f"(SELECT TRIM(s.value) v FROM {t} x, LATERAL SPLIT_TO_TABLE(x.{_q(col)}, ',') s{w})"
            else:
                src = f"(SELECT TRIM({_q(col)}::STRING) v FROM {t}{w})"
            selects.append(
                f"SELECT * FROM (SELECT {i} i, v, COUNT(*) n FROM {src} "
                f"WHERE NULLIF(TRIM(v), '') IS NOT NULL GROUP BY v ORDER BY n DESC LIMIT {TOP_VALUES + 1})")
        facets = {}
        if selects:
            names = [c for c in spec["facets"] if c in cols]
            for i, v, n in _run_or_fail(" UNION ALL ".join(selects), params, "overview facets").rows:
                facets.setdefault(names[i], []).append({"value": v, "count": n})
        out_facets = []
        for col in [c for c in spec["facets"] if c in cols]:
            vals = sorted(facets.get(col, []), key=lambda x: -x["count"])
            out_facets.append({"column": col, "label": _label(spec, col), "values": vals[:TOP_VALUES],
                               "more": len(vals) > TOP_VALUES})
        yield {"facets": out_facets}

        # by year, split by the top 5 values of the first plain facet
        trend, stack = [], None
        ysql = _year_sql(spec)
        scol = _stack_facet(spec, cols)
        if ysql:
            params = {}
            w = _where(spec, f, args["q"], None, params)  # whole range, so you can see where you are
            w = (w + " AND " if w else " WHERE ") + f"{ysql} BETWEEN {YEAR_MIN} AND {YEAR_MAX}"
            sv = f"TRIM({_q(scol)}::STRING)" if scol else "NULL"
            sql = (f"WITH base AS (SELECT {ysql} y, {sv} v FROM {t}{w}), "
                   f"top AS (SELECT v FROM base WHERE NULLIF(v, '') IS NOT NULL GROUP BY v ORDER BY COUNT(*) DESC LIMIT 5) "
                   f"SELECT y, CASE WHEN v IN (SELECT v FROM top) THEN v ELSE 'Other' END s, COUNT(*) n "
                   f"FROM base GROUP BY 1, 2 ORDER BY 1")
            years_map, totals = {}, {}
            for y, sv_, n in _run_or_fail(sql, params, "overview trend").rows:
                if y is None:
                    continue
                row = years_map.setdefault(int(y), {"year": int(y), "count": 0, "by": {}})
                row["count"] += n
                row["by"][sv_] = row["by"].get(sv_, 0) + n
                totals[sv_] = totals.get(sv_, 0) + n
            trend = [years_map[y] for y in sorted(years_map)]
            order = sorted((k for k in totals if k != "Other"), key=lambda k: -totals[k])
            if "Other" in totals:
                order.append("Other")
            stack = {"column": scol, "label": _label(spec, scol) if scol else None, "values": order}
        yield {"trend": trend, "stack": stack}

        # last 12 months vs the 12 before, plus newest / oldest date
        kpis = {}
        latest = []
        dsql = _date_sql(spec)
        if dsql:
            params = {}
            w = _where(spec, f, args["q"], yrs, params)
            sane = f"d <= DATEADD(day, 1, CURRENT_DATE()) AND YEAR(d) >= {YEAR_MIN}"
            r = _run_or_fail(
                "SELECT COUNT_IF(d >= DATEADD(month, -12, CURRENT_DATE())), "
                "COUNT_IF(d >= DATEADD(month, -24, CURRENT_DATE()) AND d < DATEADD(month, -12, CURRENT_DATE())), "
                f"MAX(d), MIN(d) FROM (SELECT {dsql} d FROM {t}{w}) WHERE {sane}", params, "overview kpis").rows[0]
            kpis = {"last_12m": r[0], "prior_12m": r[1], "newest": _to_json_safe(r[2]), "oldest": _to_json_safe(r[3])}
            yield {"kpis": kpis}

            tags = [c for c in spec["facets"] if c in cols][:2]
            pick = [spec["title"], spec.get("key"), spec.get("url")]
            sel = ", ".join(f"{_q(c)}::STRING" if c and c in cols else "NULL" for c in pick + tags)
            res = _run_or_fail(
                f"SELECT * FROM (SELECT {sel}, {dsql} d FROM {t}{w}) x WHERE {sane} "
                f"ORDER BY d DESC NULLS LAST LIMIT 8", params, "overview latest")
            for row in res.rows:
                row = [_to_json_safe(v) for v in row]
                latest.append({"title": (row[0] or "")[:300], "key": row[1], "url": row[2],
                               "tags": [{"column": c, "value": v} for c, v in zip(tags, row[3:3 + len(tags)]) if v],
                               "date": row[-1]})

        yield {"kpis": kpis, "latest": latest}

    return _stream("cortellis-overview", args, bypass_cache, parts)


@cortellis_router.get("/insights/{tid}")
def get_insights(
    tid: str,
    filters: str = Query(""),
    q: str = Query("", max_length=200),
    years: str = Query(""),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(PERM),
):
    """The heavier extras: heatmap, top-N list, number distributions, completeness.
    Separate from /overview so the main charts show first."""
    spec = _spec(tid)
    f = _parse_filters(spec, filters)
    yrs = _parse_years(years)
    args = {"tid": tid, "f": f, "q": q.strip(), "y": yrs}

    def parts():
        t = _fq(spec)
        cols = set(_columns(spec, bypass_cache))
        out: Dict[str, Any] = {"id": tid}

        # heatmap: top 8 rows x top 6 columns
        a, b = spec.get("cross") or (None, None)
        if a in cols and b in cols:
            params = {}
            w = _where(spec, f, args["q"], yrs, params)
            sql = (f"WITH base AS (SELECT TRIM({_q(a)}::STRING) a, TRIM({_q(b)}::STRING) b FROM {t}{w}), "
                   f"ta AS (SELECT a FROM base WHERE NULLIF(a, '') IS NOT NULL GROUP BY a ORDER BY COUNT(*) DESC LIMIT 8), "
                   f"tb AS (SELECT b FROM base WHERE NULLIF(b, '') IS NOT NULL GROUP BY b ORDER BY COUNT(*) DESC LIMIT 6) "
                   f"SELECT a, b, COUNT(*) n FROM base WHERE a IN (SELECT a FROM ta) AND b IN (SELECT b FROM tb) GROUP BY 1, 2")
            cells = [{"row": r, "col": c_, "count": n} for r, c_, n in _run_or_fail(sql, params, "insights cross").rows]
            tot_r, tot_c = {}, {}
            for c_ in cells:
                tot_r[c_["row"]] = tot_r.get(c_["row"], 0) + c_["count"]
                tot_c[c_["col"]] = tot_c.get(c_["col"], 0) + c_["count"]
            out["cross"] = {"rows_col": a, "cols_col": b,
                            "rows": sorted(tot_r, key=lambda k: -tot_r[k]), "cols": sorted(tot_c, key=lambda k: -tot_c[k]),
                            "cells": cells}
            yield {"id": tid, "cross": out["cross"]}

        # top N of a high-cardinality column
        lead = spec.get("leaders")
        if lead and lead[0] in cols:
            col, label, split = lead
            params = {}
            w = _where(spec, f, args["q"], yrs, params)
            src = (f"(SELECT TRIM(s.value) v FROM {t} x, LATERAL SPLIT_TO_TABLE(x.{_q(col)}, ',') s{w})" if split
                   else f"(SELECT TRIM({_q(col)}::STRING) v FROM {t}{w})")
            res = _run_or_fail(f"SELECT v, COUNT(*) n FROM {src} WHERE NULLIF(v, '') IS NOT NULL "
                               f"AND v NOT IN ('N/A', '(N/A)', 'NA', 'null', 'undefined') GROUP BY v ORDER BY n DESC LIMIT 10",
                               params, "insights leaders")
            out["leaders"] = {"column": col, "label": label, "values": [{"value": v, "count": n} for v, n in res.rows]}
            yield {"leaders": out["leaders"]}

        # number columns: percentiles + a 20 bucket histogram between p1 and p99
        measures = []
        for col, label, unit in spec.get("measures", []):
            if col not in cols:
                continue
            params = {}
            w = _where(spec, f, args["q"], yrs, params)
            x = f"TRY_TO_DOUBLE({_q(col)}::STRING)"
            keep = f"x IS NOT NULL{' AND x > 0' if unit == 'year' else ''}"
            r = _run_or_fail(
                f"SELECT COUNT(*), AVG(x), APPROX_PERCENTILE(x, 0.01), APPROX_PERCENTILE(x, 0.25), APPROX_PERCENTILE(x, 0.5), "
                f"APPROX_PERCENTILE(x, 0.75), APPROX_PERCENTILE(x, 0.9), APPROX_PERCENTILE(x, 0.99) "
                f"FROM (SELECT {x} x FROM {t}{w}) WHERE {keep}", params, "insights measure").rows[0]
            n, avg, p1, p25, p50, p75, p90, p99 = [_to_json_safe(v) for v in r]
            m = {"column": col, "label": label, "unit": unit, "count": n, "avg": avg,
                 "p25": p25, "median": p50, "p75": p75, "p90": p90, "lo": p1, "hi": p99, "bins": []}
            if n and p1 is not None and p99 is not None and p99 > p1:
                params2 = dict(params)
                hist = _run_or_fail(
                    f"SELECT WIDTH_BUCKET(x, {float(p1)}, {float(p99)}, 20) b, COUNT(*) FROM (SELECT {x} x FROM {t}{w}) "
                    f"WHERE {keep} AND x BETWEEN {float(p1)} AND {float(p99)} GROUP BY 1 ORDER BY 1", params2, "insights hist").rows
                step = (p99 - p1) / 20
                by = {int(bk): c_ for bk, c_ in hist}
                m["bins"] = [{"from": p1 + (i - 1) * step, "to": p1 + i * step, "count": by.get(i, 0) + (by.get(21, 0) if i == 20 else 0)}
                             for i in range(1, 21)]
            measures.append(m)
            yield {"measures": list(measures)}
        yield {"id": tid, "measures": measures}

        # how full the key columns are
        grid = [c for c in spec["grid"] if c in cols]
        if grid:
            params = {}
            w = _where(spec, f, args["q"], yrs, params)
            parts = ", ".join(f"COUNT(NULLIF(TRIM({_q(c)}::STRING), ''))" for c in grid)
            r = _run_or_fail(f"SELECT COUNT(*), {parts} FROM {t}{w}", params, "insights completeness").rows[0]
            total = r[0] or 0
            out["completeness"] = {"total": total, "columns": [{"column": c, "filled": r[i + 1],
                                   "pct": round(100 * r[i + 1] / total, 1) if total else 0} for i, c in enumerate(grid)]}
            yield {"completeness": out["completeness"]}

    return _stream("cortellis-insights", args, bypass_cache, parts)


@cortellis_router.get("/table/{tid}")
def get_table(
    tid: str,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=MAX_PAGE),
    q: str = Query("", max_length=200),
    filters: str = Query(""),
    years: str = Query(""),
    sort: str = Query(""),
    desc: bool = Query(True),
    all_columns: bool = Query(False),
    bypass_cache: bool = Query(False),
    auth: dict = Depends(PERM),
):
    spec = _spec(tid)
    cols = _columns(spec, bypass_cache)
    if not cols:
        raise HTTPException(status_code=404, detail=f"{_fq(spec)} has no columns (missing or no access)")
    f = _parse_filters(spec, filters)
    yrs = _parse_years(years)
    args = {"tid": tid, "page": page, "size": size, "q": q.strip(), "f": f, "y": yrs,
            "sort": sort, "desc": desc, "all": all_columns}

    def parts():
        show = cols if all_columns else [c for c in spec["grid"] if c in cols]
        key = spec.get("key")
        if key in cols and key not in show:
            show = show + [key]   # drawer needs it to load the full row
        params = {"limit": size, "offset": (page - 1) * size}
        where = _where(spec, f, args["q"], yrs, params)
        order = f" ORDER BY {_q(sort)} {'DESC' if desc else 'ASC'} NULLS LAST" if sort in cols else ""
        select = ", ".join(_q(c) for c in show)
        sql = f"SELECT {select} FROM {_fq(spec)}{where}{order} LIMIT %(limit)s OFFSET %(offset)s"
        res = _run_or_fail(sql, params, f"/table/{tid}")

        def cell(v):
            v = _to_json_safe(v)
            return v[:CELL_CUT] + "…" if isinstance(v, str) and len(v) > CELL_CUT else v

        yield {
            "id": tid, "label": spec["label"], "table": _fq(spec),
            "columns": [str(c) for c in res.columns], "rows": [],
            "page": page, "size": size, "sql": _render_sql_for_display(sql, params),
        }
        for i in range(0, len(res.rows), ROW_BATCH):
            yield {"rows+": [[cell(v) for v in r] for r in res.rows[i:i + ROW_BATCH]]}
        # count is the slow bit on filtered 500k rows, grid is already up by now
        yield {"total": _run_or_fail(f"SELECT COUNT(*) FROM {_fq(spec)}{where}", params, f"/table/{tid} count").rows[0][0]}

    return _stream("cortellis-table", args, bypass_cache, parts)


@cortellis_router.get("/record/{tid}")
def get_record(tid: str, key: str = Query(..., min_length=1, max_length=100), auth: dict = Depends(PERM)):
    """Full row for the drawer, grid cells are cut short."""
    spec = _spec(tid)
    k = spec.get("key")
    if not k:
        raise HTTPException(status_code=400, detail="This table has no record key")
    res = _run_or_fail(f"SELECT * FROM {_fq(spec)} WHERE {_q(k)}::STRING = %(k)s LIMIT 1", {"k": key}, "record")
    if not res.rows:
        raise HTTPException(status_code=404, detail="Record not found")
    return {"columns": [str(c) for c in res.columns], "row": [_to_json_safe(v) for v in res.rows[0]]}