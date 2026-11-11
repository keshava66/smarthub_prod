"""
Runs Regwiz against the real Azure OpenAI deployment and scores what it does.
The pytest suites use a scripted model; this one checks the live model actually
follows the rules - exact navigation, asking when unsure, refusing to invent.

Needs the same AZURE_OPENAI_* settings as the backend (backend/.env). Build the
KB first (python -m assistant.build_kb) so answers are grounded.

    cd backend
    python -m assistant.tests.eval_live             # everything
    python -m assistant.tests.eval_live -k roles    # scenarios whose text has 'roles'
    python -m assistant.tests.eval_live --role it_viewer
    python -m assistant.tests.eval_live --workers 6     # run scenarios in parallel

Exit code is non-zero if anything fails, so it can gate a release.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient

from assistant import env  # noqa: F401
from assistant import router as r
from assistant.tests.test_navigation import CASES

import auth

# (message, check, history). check is one of:
#   ("nav", dest_id)        navigated to exactly this place
#   ("no_nav", regex)       did NOT navigate, reply matches regex
#   ("say", regex)          reply matches regex (navigation allowed)
BEHAVIOUR = [
    ("what time is it?", ("no_nav", r"(can't|cannot|outside|not able)"), []),
    ("write me a python script to sort a list", ("no_nav", r"(portal|can't|cannot|outside)"), []),
    ("take me to the quarterly budget forecast page", ("no_nav", r"(couldn't|can't|cannot|no such|isn't|not find|don't see|doesn't)"), []),
    # ambiguous for someone who can open several of the options - but if the role can
    # open only one, going straight there is right. "auto" works out which applies.
    ("take me to the overview", ("auto", None), []),
    ("open the data table", ("auto", None), []),
    ("where can I see row counts?", ("say", r"IT Checks"), []),
    ("how do I create a new business rule?", ("say", r"(Superuser|Rule Governance)"), []),
    ("what does the review tab do?", ("say", r"(Accept|Reject|sign)"), []),
    ("what is ingestion lab?", ("say", r"(ingestion|step)"), []),
    ("what's the difference between IT checks and business defined checks?", ("say", r"(row count|rule)"), []),
    ("take me there", ("nav", "bdc.review"),
     [{"role": "user", "content": "what's on the review tab?"},
      {"role": "assistant", "content": "The Review tab on Business Defined Checks is where admins accept or reject each rule."}]),
    ("now the trends one", ("nav", "bdc.trends"),
     [{"role": "user", "content": "open failed rows"},
      {"role": "assistant", "content": "I've opened Business Defined Checks › Failed Rows."}]),
]


def build_app(perms, email="eval@example.com"):
    app = FastAPI()
    app.include_router(r.router)

    async def fake_auth():
        return {"user": {"email": email}, "token": os.getenv("EVAL_BEARER", "eval"),
                "role": "eval", "permissions": perms}
    app.dependency_overrides[r._auth] = fake_auth
    return TestClient(app)


def ask(client, text, history, url):
    body = {"messages": [*history, {"role": "user", "content": text}],
            "screen": {"route": url.split("://x")[1].split("?")[0], "url": url}}
    res = client.post("/assistant/chat", json=body)
    events = [json.loads(line[5:]) for line in res.text.split("\n\n") if line.startswith("data:")]
    reply = "".join(e["text"] for e in events if e["type"] == "token")
    navs = [e["action"]["args"] for e in events if e["type"] == "action" and e["action"]["name"] == "navigate_to"]
    errors = [e["message"] for e in events if e["type"] == "error"]
    return reply, navs, errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default="", help="only scenarios containing this text")
    ap.add_argument("--role", default="admin", choices=sorted(auth.BUILTIN_ROLES))
    ap.add_argument("--url", default="https://x/dashboard", help="page the user is 'on'")
    ap.add_argument("--workers", type=int, default=1,
                    help="scenarios run at once (4-6 is safe for most Azure quotas)")
    args = ap.parse_args()

    if not (os.getenv("AZURE_OPENAI_ENDPOINT") and (os.getenv("AZURE_OPENAI_API_KEY") or r._get_aad_token_provider())):
        print("AZURE_OPENAI_ENDPOINT / key not set - nothing to evaluate against.")
        return 2
    os.environ.setdefault("ASSISTANT_ACTION_ACK_TIMEOUT", "0.2")   # no browser to ack
    import logging
    logging.disable(logging.WARNING)   # httpx/audit INFO lines drown the results

    perms = dict(auth.BUILTIN_ROLES[args.role]["permissions"])
    perms["viewAssistantAvatar"] = True
    # the portal's 30/min per-user limit is for real users, not a test run
    r._rate_limit = lambda *a, **k: None
    from assistant import portal_map as pm

    scenarios = [(t, ("nav", d), []) for t, d in CASES] + BEHAVIOUR
    scenarios = [s for s in scenarios if args.k.lower() in s[0].lower()]

    local = threading.local()   # one test client per worker thread

    def run_one(scenario):
        text, (kind, want), history = scenario
        if not hasattr(local, "client"):
            local.client = build_app(perms)
        if kind == "auto":
            res = pm.resolve(text, perms)
            if res["best"] and res["best"]["allowed"] and not res["ambiguous"]:
                kind, want = "nav", res["best"]["id"]
            else:
                kind, want = "no_nav", r"(\?|access|permission)"
        if kind == "nav" and not pm.can_access(want, perms):
            kind, want = "no_nav", r"(access|permission)"
        url = args.url
        # a "take me to X" case can't start on X - the right answer there is "you're already on it"
        if kind == "nav" and pm.describe_url(url) and pm.describe_url(url)["id"] == want:
            url = "https://x/fuzzy-match" if want == "dashboard" else "https://x/dashboard"
        reply, navs, errors = ask(local.client, text, history, url)
        # the server tags every navigation with the destination it resolved to -
        # reading the URL back can't tell apart places that share one (BDC / Executive View)
        got = None
        if navs:
            got = navs[-1].get("destination")
            if not got:
                hit = pm.match_url(navs[-1]["route"], navs[-1]["params"])
                got = hit.id if hit else navs[-1]["route"]
        if errors:
            ok = False
        elif kind == "nav":
            ok = got == want
        elif kind == "no_nav":
            ok = not navs and re.search(want, reply, re.I) is not None
        else:
            ok = re.search(want, reply, re.I) is not None
        print(("PASS" if ok else "FAIL"), f"{text!r}", f"-> {got or '-'}", flush=True)
        if args.workers == 1:
            time.sleep(0.3)
        return None if ok else {"message": text, "expected": f"{kind} {want}", "got": got,
                                "navigations": navs, "reply": reply, "errors": errors}

    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(run_one, scenarios))   # keeps scenario order
    failed = [x for x in results if x]
    failures = len(failed)
    print(f"\ntook {time.time() - started:.0f}s with {args.workers} worker(s)")
    print(f"\n{len(scenarios) - failures}/{len(scenarios)} passed as role {args.role}")
    if failed:
        print("\nFAILURES:")
        for f in failed:
            print(f"- {f['message']!r}\n    expected: {f['expected']}\n    got:      {f['got'] or 'no navigation'}"
                  f"\n    reply:    {f['reply'][:220]!r}" + (f"\n    errors:   {f['errors']}" if f["errors"] else ""))
        out = f"eval_failures_{args.role}.json"
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(failed, fh, indent=2)
        print(f"\nfull details written to {out}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())