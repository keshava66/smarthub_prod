"""
Tools the assistant is allowed to use.

Two kinds:

  - data tools  -> call the portal's own HTTP endpoints with the *user's* bearer
                   token forwarded, so the user only ever sees data their role
                   already exposes. The model never makes numbers up; it asks for
                   a tool, we run it, it gets back real JSON.

  - client tools -> things that happen in the browser (navigate, highlight). The
                    backend doesn't execute these; it hands them back down the SSE
                    stream as an "action" and the React app carries them out.

Every tool declares the permission it needs. build_tool_specs() only hands the
model the tools the current user is allowed to use, so the model can't even try
to call something out of scope.
"""

import logging
import os
import requests

from . import rbac, memory, portal_map

logger = logging.getLogger(__name__)

# These tool calls all hit our own backend. Opening a fresh TCP connection for
# every single one adds up fast across a multi-tool-call turn, so keep one
# session with connection pooling / keep-alive instead.
_session = requests.Session()


def _configure_tls():
    """
    ASSISTANT_SELF_TLS_VERIFY controls how the session verifies the server it's
    calling back into (ASSISTANT_SELF_BASE_URL) — this is only relevant when
    that URL is an https:// host in front of a self-signed / internal cert;
    it does nothing for a plain http://localhost:8000 self-call.

      unset / "true" / "1"   -> normal verification (default, secure)
      "false" / "0"          -> verification off entirely — logs a loud warning
                                 every time the process starts, since this is a
                                 real security regression if it ever ends up
                                 set in a production value by accident
      any other value        -> treated as a path to a CA bundle / cert file
                                 (e.g. your self-signed cert's .pem), so the
                                 self-signed cert is trusted specifically,
                                 without turning off verification altogether
    """
    raw = (os.getenv("ASSISTANT_SELF_TLS_VERIFY") or "").strip()
    if not raw or raw.lower() in ("true", "1", "yes"):
        _session.verify = True
        logger.info("assistant tools: TLS verification ON for internal calls (ASSISTANT_SELF_TLS_VERIFY=%r)", raw or "<unset>")
    elif raw.lower() in ("false", "0", "no"):
        _session.verify = False
        logger.warning(
            "ASSISTANT_SELF_TLS_VERIFY=%s — TLS certificate verification is OFF for the "
            "assistant's internal calls to ASSISTANT_SELF_BASE_URL. Fine for a dev box with "
            "a self-signed cert; never leave this set in a production .env. Prefer pointing "
            "ASSISTANT_SELF_TLS_VERIFY at the cert file instead, so verification stays on.",
            raw,
        )
    else:
        _session.verify = raw  # path to a CA bundle / the self-signed cert itself
        logger.info("assistant tools: verifying internal calls against CA bundle %s", raw)


_configure_tls()


# Each entry: name -> (permission, openai function spec, handler-or-None)
# handler is None for client tools (executed in the browser).

# Where navigate_to can send people lives in portal_map.py - every page, tab
# and sub-tab with the params the page really reads. Don't list routes here.


def _spec(name, description, params, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
    }


# ---- data tool handlers -----------------------------------------------------
# They all take (base_url, token, args) and return a plain dict/list.

def _get(base_url, token, path, params=None):
    r = _session.get(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params or {},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _post(base_url, token, path, json_body=None, timeout=30):
    r = _session.post(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
        json=json_body or {},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def _kpi(base_url, token, args):
    return _get(base_url, token, "/api/overview/kpi", {"delta_mode": args.get("delta_mode", False)})


def _rules(base_url, token, args):
    data = _get(base_url, token, "/api/rules", {"delta_mode": args.get("delta_mode", False)})
    # Don't dump hundreds of rules into the context window. Filter + cap here.
    sev = (args.get("severity") or "").upper()
    status = (args.get("status") or "").upper()
    table = (args.get("target_table") or "").upper()
    rows = []
    for r in data:
        if sev and (r.get("severity") or "").upper() != sev:
            continue
        if status and (r.get("status") or "").upper() != status:
            continue
        if table and table not in (r.get("target_table") or "").upper():
            continue
        rows.append(r)
    return {"count": len(rows), "rules": rows[:40]}


def _dataset_health(base_url, token, args):
    return _get(base_url, token, "/api/dataset-health")


def _trend(base_url, token, args):
    rule_id = args["rule_id"]
    return _get(base_url, token, f"/api/trends/{rule_id}")


def _registry(base_url, token, args):
    rule_id = args["rule_id"]
    return _get(base_url, token, f"/api/rules/{rule_id}/registry")


# Denodo's global search takes 45-110s for a new term (repeats come from a 30
# minute cache), so this call gets a much longer timeout than the other tools.
SEARCH_TIMEOUT = float(os.getenv("ASSISTANT_SEARCH_TIMEOUT", "200"))
SEARCH_VIEWS_SHOWN = 15
SEARCH_ROWS_SHOWN = 3
SEARCH_VALUE_CHARS = 120


def _search_catalog(base_url, token, args):
    query = (args.get("query") or "").strip()
    if not query:
        return {"error": "query is required"}
    try:
        data = _post(base_url, token, "/search", {"search_text": query, "max_records_per_view": 10},
                     timeout=SEARCH_TIMEOUT)
    except requests.Timeout:
        return {"error": "Denodo didn't answer in time. Suggest opening Fuzzy Match and running the search there.",
                "page": "/fuzzy-match"}
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code in (502, 503, 504):
            return {"error": "Denodo is slow or unavailable right now. Suggest trying again in a minute "
                             "or running the search on the Fuzzy Match page.", "page": "/fuzzy-match"}
        raise

    # The full answer can be hundreds of views; give the model the counts and a
    # few sample rows of the biggest views. The page has everything else.
    results = data.get("results") or {}
    ranked = sorted(results.items(), key=lambda kv: -(kv[1].get("record_count") or 0))
    views = []
    for name, v in ranked[:SEARCH_VIEWS_SHOWN]:
        rows = []
        for rec in (v.get("records") or [])[:SEARCH_ROWS_SHOWN]:
            rows.append({k: (str(val)[:SEARCH_VALUE_CHARS] if val is not None else None) for k, val in rec.items()})
        views.append({"view": v.get("view") or name, "database": v.get("database"),
                      "source": v.get("datasource"), "matches": v.get("record_count"),
                      "matches_capped": bool(v.get("total_capped")), "sample_rows": rows})
    return {
        "term": data.get("term"), "fuzzy": data.get("fuzzy"), "typos_allowed": data.get("edit_distance", 0),
        "total_matches": data.get("total_records", 0), "view_count": data.get("view_count", len(results)),
        "views": views, "views_not_shown": max(0, len(results) - len(views)),
        "notes": data.get("notes") or [], "cached": data.get("cached"),
        "page": "/fuzzy-match?q=" + requests.utils.quote(query, safe=""),
    }


def _pending_reviews(base_url, token, args):
    data = _get(base_url, token, "/api/review/rules", {"delta_mode": args.get("delta_mode", False)})
    rules = [r for r in data.get("rules", []) if r.get("review_status") == "NOT_REVIEWED"]
    # Trim the SQL/AI narrative fields — they're not needed to answer "what's
    # waiting on review", and can be large; keep the queue response small.
    trimmed = [
        {k: v for k, v in r.items() if k not in ("sql_predicate", "ai_output")}
        for r in rules[:30]
    ]
    return {"pending_count": len(rules), "rules": trimmed}


def _consolidated_summary(base_url, token, args):
    data = _get(base_url, token, "/api/consolidated-report/latest", {"delta_mode": args.get("delta_mode", False)})
    if not data.get("found"):
        return {"found": False}
    preview = data.get("preview", {})
    # The full preview can be thousands of rows; a handful is plenty of
    # context for "what does the consolidated report look like" questions —
    # anything deeper belongs in the actual downloaded report, not the chat.
    return {
        "found": True,
        "report_name": data.get("report_name"),
        "summary": data.get("summary", {}),
        "preview_columns": preview.get("columns", []),
        "preview_sample_rows": preview.get("rows", [])[:5],
    }


def _unstructured_summary(base_url, token, args):
    return _get(base_url, token, "/api/unstructured/summary")


def _gra_data_domains_summary(base_url, token, args):
    data = _get(base_url, token, "/api/gra-data-domains/domains")
    domains = data.get("domains", [])
    return {
        "domain_count": len(domains),
        "total_records": sum(d.get("count", 0) for d in domains),
        "domains": domains,
    }


def _rbac_overview(base_url, token, args):
    data = _get(base_url, token, "/api/rbac/config")
    cfg = data.get("config", {})
    builtin = cfg.get("builtin_roles", {}) or {}
    custom = cfg.get("custom_roles", {}) or {}
    def granted(r):
        return sorted(k for k, v in (r.get("permissions") or {}).items() if v is True)

    return {
        "roles": [
            {"id": rid, "name": r.get("name", rid), "custom": False,
             "description": r.get("description", ""), "permissions": granted(r)}
            for rid, r in builtin.items()
        ] + [
            {"id": rid, "name": r.get("name", rid), "custom": True,
             "description": r.get("description", ""), "permissions": granted(r)}
            for rid, r in custom.items()
        ],
        # email -> {"roles": [...], "keep_ad": bool}: roles given by hand. keep_ad
        # means their AD group's role counts too; off means it's ignored.
        "user_roles": cfg.get("user_roles", {}),
        # email -> main role, for older answers ("who has admin").
        "user_role_overrides": cfg.get("user_role_overrides", {}),
        # email -> {"add": [...], "remove": [...]}: custom access on top of the role.
        "user_permission_overrides": cfg.get("user_permission_overrides", {}),
    }


def _audit_stats(base_url, token, args):
    return _get(base_url, token, "/api/audit/stats")


def _data_api_overview(base_url, token, args):
    data = _get(base_url, token, "/data-api/list")
    apis = data.get("apis", []) or []
    # api_key is a live shareable secret — never let it reach the model/chat.
    return {
        "total_apis": len(apis),
        "apis": [
            {
                "name": a.get("name"),
                "slug": a.get("slug"),   # what navigate_to params {"api": ...} wants
                "description": a.get("description"),
                "table_aliases": a.get("aliases"),
                "requests_7d": a.get("requests_7d"),
                "is_active": a.get("is_active"),
                "table_count": a.get("table_count"),
                "created_by": a.get("created_by"),
                "created_at": a.get("created_at"),
                "last_used_at": a.get("last_used_at"),
            }
            for a in apis
        ],
    }


def _remember_preference(base_url, token, args, email=None):
    # Doesn't hit the portal backend at all — writes straight to the
    # assistant's own long-term memory store (Postgres, if configured).
    key = (args.get("key") or "").strip()
    value = (args.get("value") or "").strip()
    if not key or not value:
        return {"error": "both key and value are required"}
    if not email:
        return {"error": "no user identity available to attach this to"}
    ok = memory.set_preference(email, key, value)
    if ok:
        from . import router   # late import - router imports tools
        router.forget_memory_cache(email)
    if not ok:
        return {"remembered": False, "reason": "long-term memory isn't configured on this server, or you already have too many saved preferences"}
    return {"remembered": True}


# ---- the registry -----------------------------------------------------------

TOOLS = {
    "get_portal_kpi": {
        "permission": "viewDQE",
        "spec": _spec(
            "get_portal_kpi",
            "Overall data-quality KPIs for the portal: number of tables under check, "
            "number of rules, and the average pass percentage. Use for 'how are we doing "
            "overall' type questions.",
            {"delta_mode": {"type": "boolean", "description": "Use the delta (last-run change) views instead of the full picture."}},
            [],
        ),
        "handler": _kpi,
    },
    "list_rules": {
        "permission": "viewDQE",
        "spec": _spec(
            "list_rules",
            "List Business Defined Checks rules with their pass percentage, severity, latest "
            "execution status and target table. Only rules whose review is Approved are returned "
            "(same as the Active Rules table) - for rules awaiting review use get_pending_reviews. "
            "Filter when you can (severity, status, target_table) so you get a focused set.",
            {
                "severity": {"type": "string", "description": "e.g. CRITICAL, HIGH, MEDIUM, LOW"},
                "status": {"type": "string", "description": "Latest execution status as shown in the table, e.g. PASS or FAIL. Not the review status."},
                "target_table": {"type": "string", "description": "Substring match on the target table name."},
                "delta_mode": {"type": "boolean"},
            },
            [],
        ),
        "handler": _rules,
    },
    "get_dataset_health": {
        "permission": "viewDQE",
        "spec": _spec(
            "get_dataset_health",
            "Per-dataset health: total rules, pass rate and a status per dataset. Use to compare "
            "datasets or find the weakest one.",
            {},
            [],
        ),
        "handler": _dataset_health,
    },
    "get_rule_trend": {
        "permission": "viewDQE",
        "spec": _spec(
            "get_rule_trend",
            "Pass-percentage over time for one rule. Use when asked whether a rule is improving or "
            "degrading, or for history.",
            {"rule_id": {"type": "string"}},
            ["rule_id"],
        ),
        "handler": _trend,
    },
    "get_rule_definition": {
        "permission": "viewDQE",
        "spec": _spec(
            "get_rule_definition",
            "The actual SQL predicate and target table behind a rule. Use when asked what a rule "
            "checks or why rows fail it.",
            {"rule_id": {"type": "string"}},
            ["rule_id"],
        ),
        "handler": _registry,
    },
    "search_catalog": {
        "permission": "viewData",
        "spec": _spec(
            "search_catalog",
            "Fuzzy Match: search the Denodo data catalog across every connected database and view "
            "for a word or value. Use when asked to find where a term/column/value appears anywhere "
            "in the connected data, or to look something up in the catalog. A plain word matches whole "
            "words exactly; add ~0.9 to allow 1 typo per word or ~0.8 for 2 (e.g. 'stelara~0.9'). "
            "A new term can take up to a couple of minutes; repeats are instant. Returns counts per "
            "view and a few sample rows; 'page' is a link that opens the same search on the page.",
            {"query": {"type": "string", "description": "The word or value to search for, optionally with ~0.9 or ~0.8."}},
            ["query"],
        ),
        "handler": _search_catalog,
    },
    "get_pending_reviews": {
        "permission": "viewBusinessChecks",
        "spec": _spec(
            "get_pending_reviews",
            "Business Defined Checks awaiting sign-off (not yet Approved or Rejected), with severity, "
            "dataset and pass percentage. Use for 'what's waiting on review' / 'what needs sign-off' "
            "questions, or to get the pending count for the Review tab's badge.",
            {"delta_mode": {"type": "boolean", "description": "Use the Delta-mode review queue instead of the full one."}},
            [],
        ),
        "handler": _pending_reviews,
    },
    "get_consolidated_report_summary": {
        "permission": "viewBusinessChecks",
        "spec": _spec(
            "get_consolidated_report_summary",
            "Summary of the most recently generated consolidated failed-rows report: total rows, "
            "duplicates removed, rules included, and a small sample of the data. Use for 'what's in "
            "the consolidated report' / 'when was it last generated' type questions — not for reading "
            "the full report, which the user should download.",
            {"delta_mode": {"type": "boolean"}},
            [],
        ),
        "handler": _consolidated_summary,
    },
    "get_unstructured_summary": {
        "permission": "viewUnstructuredRimDocs",  # reads /api/unstructured/summary (RIMDocs)
        "spec": _spec(
            "get_unstructured_summary",
            "Headline counts for the Unstructured data area: total records and distinct file names "
            "currently in view. Use for 'how much unstructured data is there' type questions.",
            {},
            [],
        ),
        "handler": _unstructured_summary,
    },
    "get_gra_data_domains_summary": {
        "permission": "viewGraDataDomains",
        "spec": _spec(
            "get_gra_data_domains_summary",
            "Every domain on the Structured Explorer (Find it) page, with its record count, plus the total "
            "domain count and total record count. Use for 'how many domains are there' / 'what "
            "domains exist' / 'how many GRA data domains' type questions.",
            {},
            [],
        ),
        "handler": _gra_data_domains_summary,
    },
    "get_rbac_overview": {
        "permission": "viewUserManagement",
        "spec": _spec(
            "get_rbac_overview",
            "The portal's roles (built-in and custom) and any per-user role overrides on file. Use "
            "for 'what roles exist', 'who is an admin', or 'what role does X have' type questions. "
            "Does not include AD-group-derived roles for users without an explicit override — only "
            "the override list itself.",
            {},
            [],
        ),
        "handler": _rbac_overview,
    },
    "get_audit_stats": {
        "permission": "viewAudit",
        "spec": _spec(
            "get_audit_stats",
            "Summary counts for the audit dashboard: total events, today's events, role changes and "
            "unique users. Use for 'how much activity today' / audit-summary type questions rather "
            "than raw log rows.",
            {},
            [],
        ),
        "handler": _audit_stats,
    },
    "get_data_api_overview": {
        "permission": "manageDataAPIExports",
        "spec": _spec(
            "get_data_api_overview",
            "List of Data API Export definitions: name, description, active status, how many "
            "tables each exposes, who created it, and when it was created/last used. Use for "
            "'how many data APIs do we have', 'is X API active', 'who set up the Y export' type "
            "questions. Never contains the actual API key — for that, direct the user to the "
            "Data API Export page.",
            {},
            [],
        ),
        "handler": _data_api_overview,
    },
    "remember_preference": {
        "permission": "viewAssistantAvatar",
        "spec": _spec(
            "remember_preference",
            "Save a short standing fact about how this user wants you to work with them "
            "for FUTURE sessions too — e.g. they always want delta mode, they prefer being "
            "called by a nickname, they always start with the DQE dashboard. Only call this "
            "when the user actually asks you to remember/always do something, not for one-off "
            "requests. `key` is a short label for the preference, `value` is the fact itself.",
            {
                "key": {"type": "string", "description": "Short label, e.g. 'default_view' or 'preferred_name'."},
                "value": {"type": "string", "description": "The fact to remember, in plain words."},
            },
            ["key", "value"],
        ),
        "handler": _remember_preference,
        "needs_email": True,
    },

    # ---- client tools (no handler — executed in the browser) ----
    "navigate_to": {
        "permission": "viewDashboard",
        "spec": None,   # built per user in _navigate_spec(), the destination list depends on access
        "handler": None,
    },
    "highlight_on_screen": {
        "permission": "viewDashboard",
        "spec": _spec(
            "highlight_on_screen",
            "Scroll to and visually ring anything on the current page so the user can see it. "
            "Point at a single thing with `target`, several at once with `targets` (e.g. highlight "
            "every failing row), or an arbitrary box with `region`. A `target` may be a ref from the "
            "CURRENT SCREEN 'controls'/'elements' lists, a CSS selector, or the element's visible text.",
            {
                "target": {"type": "string", "description": "A ref, CSS selector, or visible text of one element."},
                "targets": {"type": "array", "items": {"type": "string"}, "description": "Several refs/selectors/texts to highlight at once."},
                "region": {"type": "object", "description": "A free box {x,y,w,h} in CSS pixels (viewport coords)."},
                "note": {"type": "string", "description": "Short label to show on the highlight."},
            },
            [],
        ),
        "handler": None,
    },
    "do_action": {
        "permission": "viewDashboard",
        "spec": _spec(
            "do_action",
            "Actually perform an on-screen action for the user — click, type, select, hover, focus, "
            "check, or scroll to anything that's rendered. The avatar walks to the element and acts. "
            "`target` can be a ref from the CURRENT SCREEN 'controls'/'elements' lists, a CSS selector, "
            "or the element's visible text — you are NOT limited to a fixed list. Set `action` to the "
            "verb (click/type/select/hover/focus/check/scroll); if omitted it's inferred (typing when a "
            "value is given on an input, else click). Pass `value` for type/select. Use this whenever "
            "the user asks you to do something ('load it', 'run validation', 'close this', 'go to the "
            "trends tab', 'type X in search', 'pick the second row').",
            {
                "target": {"type": "string", "description": "Ref, CSS selector, or visible text of the element to act on."},
                "action": {"type": "string", "enum": ["click", "type", "select", "hover", "focus", "check", "scroll"], "description": "The verb. Optional; inferred when omitted."},
                "value": {"type": "string", "description": "Text to type, or option to select."},
                "action_id": {"type": "string", "description": "Deprecated alias for target (still accepted)."},
            },
            [],
        ),
        "handler": None,
    },
    "guide_tour": {
        "permission": "viewDashboard",
        "spec": _spec(
            "guide_tour",
            "Give a guided walkthrough: take the user there and point at each thing while you "
            "explain it. Use this whenever they ask you to explain, show, demonstrate, or walk them "
            "through how something works or where to find it. Provide ordered `steps`; for each step "
            "the avatar navigates (if `route` given), walks to the element, rings it, and shows your "
            "`say` text as a callout, paced to reading time. Because steps may run on a page you "
            "haven't captured yet, identify `target` by the element's EXACT visible text — a heading, "
            "tab name, button label, or cell value copied as-is from the CURRENT SCREEN "
            "'controls'/'elements' lists (e.g. \"Dataset Health Overview\", not \"the health section\"). "
            "Keep it short and literal, or use a stable CSS selector. Keep each `say` to one or two "
            "short sentences.",
            {
                "title": {"type": "string", "description": "Optional heading shown on the first step."},
                "steps": {
                    "type": "array",
                    "description": "Ordered walkthrough steps.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "destination": {"type": "string", "description": "Optional destination id (same ids as navigate_to) to open before this step. Prefer this over route."},
                            "route": {"type": "string", "description": "Optional path to navigate to first, e.g. /dqe. Only if no destination id fits."},
                            "params": {"type": "object", "description": "Optional query params for route."},
                            "target": {"type": "string", "description": "Element to point at — visible text or CSS selector."},
                            "say": {"type": "string", "description": "The short explanation for this step."},
                        },
                        "required": ["say"],
                    },
                },
            },
            ["steps"],
        ),
        "handler": None,
    },
}

CLIENT_TOOLS = {name for name, t in TOOLS.items() if t["handler"] is None}

# perms dicts come straight from auth.py's cached role resolution, so the same
# role always hands us an equivalent dict — cache the built spec list by role
# name instead of rebuilding the same list on every single chat request.
_tool_specs_cache = {}


def _navigate_spec(perms: dict) -> dict:
    ids = [x.id for x in portal_map.DESTINATIONS.values() if portal_map.can_access(x.id, perms)]
    catalog = "\n".join("  " + line[:170] for line in portal_map.catalog_lines(perms))
    return _spec(
        "navigate_to",
        "Take the user to an exact place in the portal. ALWAYS pass `destination`, the id of the "
        "MOST SPECIFIC place that matches what they asked for - a tab or sub-tab beats its page. "
        "'Business Defined Checks' is bdc, not dqe; 'the roles' is users.roles, not users. "
        "The ids below are the only places this user can open (id — where it is — what it is):\n"
        + catalog +
        "\nOptional `params` add specifics the id can't carry: api=<slug> on dataapi.*, "
        "domain=<domain name>&open=1 on structured, mode=delta (Semi-Monthly) or mode=quarterly on bdc.* - "
        "the only safe way to switch the data cadence, "
        "q/user/range on audit.*. Never build a URL yourself and never guess an id that isn't listed.",
        {
            "destination": {"type": "string", "enum": ids,
                            "description": "Destination id from the list."},
            "params": {"type": "object", "description": "Optional extra query params (see above)."},
            "route": {"type": "string", "description": "Deprecated. Only if no destination id fits."},
        },
        ["destination"],
    )


def build_tool_specs(perms: dict, role: str = None) -> list:
    """Only the tools this user's role is allowed to use. Pass `role` to cache
    the built list (permissions rarely change without a redeploy, so there's no
    reason to redo this filtering on every single chat request)."""
    # keyed on the actual grants too - an admin can edit a custom role's
    # permissions without a redeploy, and a stale list would offer the wrong places
    key = (role, frozenset(k for k, v in perms.items() if v is True)) if role is not None else None
    if key is not None:
        cached = _tool_specs_cache.get(key)
        if cached is not None:
            return cached
    out = []
    for name, t in TOOLS.items():
        if not rbac.has(perms, t["permission"]):
            continue
        out.append(_navigate_spec(perms) if name == "navigate_to" else t["spec"])
    if key is not None:
        _tool_specs_cache[key] = out
    return out


def run_tool(name: str, args: dict, perms: dict, base_url: str, token: str, email: str = None) -> dict:
    """Execute a data tool. Re-checks permission (never trust the model's choice).
    Returns a dict the model can read. Client tools should never reach here."""
    tool = TOOLS.get(name)
    if not tool:
        return {"error": f"unknown tool '{name}'"}
    if not rbac.has(perms, tool["permission"]):
        return {"error": "you don't have access to that in this portal"}
    if tool["handler"] is None:
        return {"error": "client tool — not runnable on the server"}
    try:
        if tool.get("needs_email"):
            return tool["handler"](base_url, token, args, email=email)
        return tool["handler"](base_url, token, args)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        logger.warning("tool %s -> HTTP %s", name, code)
        if code == 404:
            return {"error": "no data found for that request"}
        if code == 401:
            return {"error": "the portal didn't accept the sign-in token for this lookup (session may "
                             "have expired). This is NOT a role/permission problem - suggest refreshing the page."}
        if code == 403:
            return {"error": "the user's role doesn't allow this data (403)"}
        return {"error": f"the portal returned an error ({code})"}
    except requests.RequestException as e:
        logger.warning("tool %s connection error: %s", name, e)
        return {"error": "couldn't reach the portal to fetch that — try again in a moment"}
    except Exception:
        logger.exception("tool %s failed", name)
        return {"error": "something went wrong running that — it's been logged"}