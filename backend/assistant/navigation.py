"""
Server-side checks on navigation before anything reaches the browser.

The model asks for navigate_to / guide_tour; this turns that into a real URL,
refuses places the user can't open, and fixes the most common miss - stopping
at a parent page when the user named a tab ("business defined checks" ->
/dqe with no check). The browser only ever gets a route + params that exist.
"""

import re
from urllib.parse import urlsplit, parse_qsl

from . import portal_map as pm

# old route aliases the frontend also accepts, so a model reply that uses one
# doesn't fail here while working in the browser
_ROUTE_ALIASES = {
    "/structured-explorer": "/gra-data-domains", "/structured-packages": "/gra-data-domains",
    "/data-domains": "/gra-data-domains", "/unstructured": "/unstructured-explorer",
    "/data-quality": "/dqe", "/users": "/user-management", "/roles": "/user-management",
    "/audit-log": "/audit", "/data-api-export": "/data-api", "/studio": "/data-studio",
    "/home": "/dashboard", "/fuzzy": "/fuzzy-match", "/rags": "/rag",
    "/communications": "/communication-center", "/ingestion": "/ingestion-lab",
}


_NEGATED = re.compile(r"\b(not|instead|rather than|except|other than)\b")


def current_destination(screen: dict):
    """Destination id + breadcrumb for where the user is now, from the screen payload."""
    screen = screen or {}
    url = screen.get("url")
    if not url:
        route = screen.get("route") or ""
        url = route + (screen.get("search") or "")
    return pm.describe_url(url)


def _norm_route(route):
    if not isinstance(route, str) or not route.startswith("/"):
        return None, {}
    parts = urlsplit(route)
    path = parts.path.rstrip("/") or "/"
    path = _ROUTE_ALIASES.get(path, path)
    return (path if path in pm.ROUTES else None), dict(parse_qsl(parts.query))


def _deny(dest_id, perms):
    missing = [p for p in pm.required_perms(dest_id) if perms.get(p) is not True]
    return {
        "status": "not sent: the user doesn't have access to this place",
        "destination": pm.breadcrumb(dest_id),
        "missing_permissions": missing,
        "tell_user": "Say plainly they don't have access to it and that an admin can grant it "
                     "in User Management › Roles & Permissions. Don't navigate somewhere else instead.",
    }


_STICKY = {"/dqe": ("mode",), "/data-api": ("api",), "/unstructured-explorer": ("epoddataset",)}

_YES = re.compile(r"^\s*(yes|yeah|yep|yup|sure|ok|okay|please|please do|do it|go ahead|yes please|sounds good|"
                  r"correct|right|that one|y)\s*[.!]*\s*$", re.I)


def effective_request(history: list) -> str:
    """The latest user message - or, when it's a bare 'yes' to an offer, that
    offer too, so 'yes' after 'want me to switch to Semi-Monthly?' still means
    Semi-Monthly."""
    users = [m for m in history if m.get("role") == "user"]
    if not users:
        return ""
    q = users[-1].get("content") or ""
    if _YES.match(q):
        prev = next((m.get("content") or "" for m in reversed(history[:-1]) if m.get("role") == "assistant"), "")
        return f"{prev} {q}".strip()
    return q


def prepare_navigation(args: dict, perms: dict, question: str = "", current_id: str = None,
                       current_params: dict | None = None):
    """Returns (browser_args_or_None, result_for_model)."""
    args = args or {}
    extra = dict(args.get("params")) if isinstance(args.get("params"), dict) else {}
    # the model can ask for Quarterly explicitly
    if str(extra.get("mode", "")).lower() in ("quarterly", "standard", "off", "none", "false"):
        extra["mode"] = None
    dest_id = args.get("destination")
    note = None

    if dest_id and dest_id not in pm.DESTINATIONS:
        return None, {"status": f"not sent: '{dest_id}' is not a destination id",
                      "tell_user": "Don't guess. Pick an id from the navigate_to list, or ask the user."}

    if not dest_id:
        route, qs = _norm_route(args.get("route"))
        if not route:
            return None, {"status": f"not sent: no page at {args.get('route')!r}",
                          "tell_user": "Ask which page they meant rather than guessing a path."}
        params = pm.clean_params(route, {**qs, **extra})
        hit = pm.match_url(route, params)
        if not hit:
            return None, {"status": "not sent: that route/params combination isn't a known place"}
        dest_id = hit.id
        extra = {k: v for k, v in params.items() if pm.DESTINATIONS[dest_id].params.get(k) != v}

    # stopped at a parent of what the user plainly named -> go to the exact place.
    # Not when they're steering away from something ("the dqe page, not business checks").
    if question and not _NEGATED.search(question.lower()):
        r = pm.resolve(question, perms, current=current_id)
        best = r["best"]
        if best and not r["ambiguous"] and best["allowed"] and pm.is_ancestor(dest_id, best["id"]):
            note = f"upgraded from '{pm.breadcrumb(dest_id)}' to the exact place the user asked for"
            dest_id = best["id"]

    if not pm.can_access(dest_id, perms):
        return None, _deny(dest_id, perms)

    implied = pm.modifiers_for(question, dest_id) if question else {}
    wanted = {**implied, **extra}
    # moving between tabs of the same page keeps what the user picked there
    # (Semi-Monthly, the selected API, the EPOD dataset) unless they asked to change it
    dest_route = pm.DESTINATIONS[dest_id].route
    if current_id and current_params and pm.DESTINATIONS[current_id].route == dest_route:
        for k in _STICKY.get(dest_route, ()):
            if k not in wanted and k not in pm.DESTINATIONS[dest_id].params and current_params.get(k):
                wanted[k] = current_params[k]
    route, params = pm.url_for(dest_id, wanted)
    x = pm.DESTINATIONS[dest_id]

    now = pm.clean_params(route, current_params or {}) if current_params is not None else None
    same_place = current_id == dest_id if now is None else (current_id and pm.DESTINATIONS[current_id].route == route
                                                             and now == params)
    if current_id and same_place and not x.then_click:
        return None, {"status": "not sent: NOTHING CHANGED - the user is already on exactly this view",
                      "destination": pm.breadcrumb(dest_id),
                      "tell_user": "Nothing was switched or opened. Say they're already on it - do NOT say "
                                   "you switched, changed or opened anything."}

    browser = {"route": route, "params": params, "destination": dest_id,
               "label": pm.breadcrumb(dest_id)}
    if x.then_click:
        browser["then_click"] = x.then_click
    result = {"status": "sent", "destination": pm.breadcrumb(dest_id), "url": _url(route, params)}
    if note:
        result["note"] = note
    return browser, result


def prepare_tour(args: dict, perms: dict):
    """Expand destination ids on tour steps and drop steps that point at places
    the user can't open. Returns (browser_args, notes)."""
    steps = []
    notes = []
    for step in (args or {}).get("steps") or []:
        if not isinstance(step, dict):
            continue
        step = dict(step)
        dest = step.pop("destination", None)
        if dest or step.get("route"):
            nav, res = prepare_navigation({"destination": dest, "route": step.get("route"),
                                           "params": step.get("params")}, perms)
            if nav is None and "already on exactly this view" not in res.get("status", ""):
                notes.append(f"skipped a step: {res.get('status')}")
                continue
            if nav:
                step["route"], step["params"] = nav["route"], nav["params"]
            else:
                step.pop("route", None)
                step.pop("params", None)
        steps.append(step)
    out = dict(args or {})
    out["steps"] = steps
    return out, notes


def _url(route, params):
    return route + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else "")


def hint_block(question: str, perms: dict, current: dict | None) -> str | None:
    """What the deterministic resolver makes of the latest message. Kept as a
    hint - the model still decides whether the user wants to go anywhere."""
    lines = []
    if current:
        lines.append(f"USER IS CURRENTLY AT: {current['breadcrumb']} (destination id {current['id']}).")
    if not question:
        return "\n".join(lines) or None

    nav = pm.looks_like_navigation(question)
    r = pm.resolve(question, perms, current=current["id"] if current else None)
    best = r["best"]
    if not best and not nav:
        return "\n".join(lines) or None

    lines.append("NAVIGATION RESOLVER (string match of the user's latest message against the portal "
                 "map; use it only if they want to go somewhere or ask where something is):")
    if not best:
        lines.append("- No page or tab name in this message matched. If they're asking for data or an "
                     "explanation, ignore this block and answer normally. If they want to go somewhere "
                     "and it refers to something earlier ('there', 'that tab'), use the conversation; "
                     "otherwise say you couldn't find that place in the portal and ask what they meant. "
                     "Never guess a route.")
        return "\n".join(lines)

    def fmt(m):
        tag = "user has access" if m["allowed"] else \
            "USER HAS NO ACCESS (needs " + ", ".join(pm.required_perms(m["id"])) + ")"
        return f"{m['id']} = {m['breadcrumb']} [{tag}]"

    if r["ambiguous"]:
        close = [m for m in r["matches"] if m["score"] >= best["score"] - 0.15 and m["allowed"] == best["allowed"]]
        lines.append("- AMBIGUOUS, several places fit equally: " + "; ".join(fmt(m) for m in close))
        lines.append("- Offer EXACTLY these options, in one short question (unless the conversation "
                     "already makes it clear which one). Don't add other pages or features to the list - "
                     "if you think something else fits, it must be in PORTAL KNOWLEDGE, not a guess.")
    else:
        lines.append("- Best match: " + fmt(best))
        others = [m for m in r["matches"][1:4] if m["allowed"]]
        if others:
            lines.append("- Less likely: " + "; ".join(fmt(m) for m in others))
        if best["allowed"]:
            lines.append(f"- If navigating, call navigate_to with destination=\"{best['id']}\" "
                         "(not its parent page).")
        else:
            lines.append("- They can't open it. Say so plainly and don't navigate anywhere else instead.")
    return "\n".join(lines)


_IMPERATIVE = re.compile(
    r"\b(take me|bring me|go to|goto|go back to|navigate( me)? to|open|switch to|jump to|head to|"
    r"get me to|send me to|back to|go home|take us)\b", re.I)


def explicit_destination(question: str, perms: dict, current: dict | None):
    """Destination id for a plain imperative ('take me to the review tab') the
    resolver is sure about and the user can open - else None."""
    if not question or not _IMPERATIVE.search(question) or _NEGATED.search(question.lower()):
        return None
    r = pm.resolve(question, perms, current=current["id"] if current else None)
    best = r["best"]
    if not best or r["ambiguous"] or not best["allowed"]:
        return None
    if current and current["id"] == best["id"]:
        return None
    return best["id"]