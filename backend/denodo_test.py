#!/usr/bin/env python3
"""
Denodo probe 4 for Fuzzy Match: which search syntax does Denodo accept? READ ONLY.

Checks if anything looser than 2 typos works: ~3, wildcards (cust*, cu?tomer),
regex. For every query it records the status, the hit count and, for the rows
that come back, how many letters off the closest word is from the real term.
That last part shows what a query really matched, so we know if ~3 is a real
3 typos or quietly treated as 2.

  1. one global search for the plain term, to find a view that has it
  2. each query against that view through advanced-search (about 1s each)
  3. a few of them through the global search too (about 1 minute each)

Calls go to search/data and indexes/advanced-search only. Values are never
written to the output unless --show-snippets; only counts and distances are.

  python denodo_probe4.py --config path\\to\\dq_config.json
  python denodo_probe4.py --config ... --view gra_dh_urp_analysis_src.some_view --no-global
"""
import argparse
import getpass
import json
import os
import platform
import re
import sys
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urlencode

try:
    import requests
except ImportError:
    sys.exit("This needs the requests package: pip install requests")

SCRIPT_VERSION = "4.0"
ALLOWED = {("POST", "/public/api/search/data"), ("GET", "/public/api/indexes/advanced-search")}
WORD = re.compile(r"[^\W_]+", re.UNICODE)


def osa(a, b, cap=9):
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i]
        for j in range(1, len(b) + 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1]))
            if prev2 and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur.append(v)
        prev2, prev = prev, cur
    return prev[-1]


def drop_letters(word, n):
    """word with n inner letters taken out, e.g. customer, 3 -> cstmr"""
    w = list(word)
    vowels = [i for i in range(1, len(w) - 1) if w[i] in "aeiou"]
    others = [i for i in range(len(w) - 2, 0, -1) if i not in vowels]
    for i in sorted((vowels + others)[:n], reverse=True):
        del w[i]
    return "".join(w)


class Client:
    def __init__(self, base, user, pw, verify, timeout):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.auth = (user, pw)
        self.s.verify = verify
        self.s.headers["Accept"] = "application/json"
        self.timeout = timeout
        self.calls = 0

    def req(self, method, path, params=None, body=None):
        if (method, path) not in ALLOWED:
            raise RuntimeError(f"Refused: {method} {path} is not on the read-only list")
        params = dict(params or {})
        params.setdefault("serverId", 1)
        self.calls += 1
        t0 = time.perf_counter()
        try:
            if method == "GET":
                r = self.s.get(self.base + path + "?" + urlencode(params), timeout=self.timeout)
            else:
                r = self.s.post(self.base + path, params=params, json=body, timeout=self.timeout)
            ms = int((time.perf_counter() - t0) * 1000)
            try:
                data = r.json()
            except ValueError:
                data = None
            return r.status_code, ms, data, (r.text[:300] if r.status_code >= 400 else None)
        except requests.exceptions.RequestException as e:
            return None, int((time.perf_counter() - t0) * 1000), None, f"{type(e).__name__}: {str(e)[:200]}"


def closest(fields, term):
    """letters off for the closest word in a row, and that word"""
    best, word = 99, None
    for v in (fields or {}).values():
        for w in WORD.findall("" if v is None else str(v).lower()):
            if len(w) < 2:
                continue
            d = osa(term, w, 6)
            if d < best:
                best, word = d, w
    return best, word


def main():
    ap = argparse.ArgumentParser(description="Read-only Denodo probe 4: query syntax")
    ap.add_argument("--config")
    ap.add_argument("--base-url")
    ap.add_argument("--user")
    ap.add_argument("--term", default="customer", help="a word that is in the data")
    ap.add_argument("--db-ids", default="71")
    ap.add_argument("--view", help="db.view to test on; skips the first global search")
    ap.add_argument("--no-global", action="store_true", help="skip step 3 (saves about 4 minutes)")
    ap.add_argument("--show-snippets", action="store_true", help="write the matched words to the output")
    ap.add_argument("--verify-ssl", default="false")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default=f"denodo_probe4_{datetime.now():%Y%m%d_%H%M}.json")
    args = ap.parse_args()

    verify = {"false": False, "true": True}.get(args.verify_ssl.lower(), args.verify_ssl)
    if verify is False:
        try:
            import urllib3
            urllib3.disable_warnings()
        except Exception:
            pass
    user, pw, base = args.user or os.getenv("DENODO_USER"), os.getenv("DENODO_PASSWORD"), args.base_url
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            c = json.load(fh).get("denodo_service_account", {})
        user, pw, base = user or c.get("username"), pw or c.get("password"), base or c.get("base_url")
    if not base:
        sys.exit("Give --base-url or --config")
    user = user or input("Denodo user: ").strip()
    pw = pw or getpass.getpass(f"Password for {user}: ")

    cl = Client(base, user, pw, verify, args.timeout)
    db_ids = [int(x) for x in args.db_ids.split(",") if x.strip()]
    t = args.term.lower()
    t1, t2, t3 = drop_letters(t, 1), drop_letters(t, 2), drop_letters(t, 3)
    pre4, pre5 = t[:4], t[:5]
    report = {"script_version": SCRIPT_VERSION, "python": platform.python_version(), "base_url": base,
              "term": t, "typo_forms": {"1": t1, "2": t2, "3": t3}, "db_ids": db_ids,
              "credentials_in_file": False, "sections": {}}
    started = time.time()
    print(f"term {t}, 1 off {t1}, 2 off {t2}, 3 off {t3}")

    def global_search(text):
        body = {"text": text, "databaseIds": db_ids, "viewIds": [], "offset": 0, "limit": 1000000}
        st, ms, data, err = cl.req("POST", "/public/api/search/data", body=body)
        groups = ((data or {}).get("groupSearch") or {}).get("groups") or [] if isinstance(data, dict) else []
        views, dist = {}, Counter()
        for g in groups:
            m = re.match(r"^\s*([^\s(]+)\s*\(([^)]+)\)", str(g.get("displayName") or ""))
            if m:
                views[f"{m.group(2)}.{m.group(1)}"] = g.get("length")
            for d in g.get("documents") or []:
                dist[closest(d.get("fields"), t)[0]] += 1
        return {"status": st, "ms": ms, "error": err, "views": len(views),
                "hits": sum(v or 0 for v in views.values()),
                "rows_by_letters_off": {str(k): v for k, v in sorted(dist.items())}}, views

    # 1. find a view with the term ----------------------------------------------
    view = None
    if args.view:
        db, vn = args.view.split(".", 1)
        view = {"db": db, "view": vn}
    else:
        print(f'\n1. Global search for "{t}" to pick a view (about 1 minute) ...', end="", flush=True)
        res, views = global_search(f'"{t}"')
        print(f" {res['status']} in {res['ms']} ms, {res['views']} views")
        report["sections"]["pick"] = res
        if views:
            top = max(views.items(), key=lambda kv: kv[1] or 0)[0]
            db, vn = top.split(".", 1)
            view = {"db": db, "view": vn}
    if not view:
        sys.exit("No view found for that term. Try another --term or give --view db.view")
    report["view"] = f'{view["db"]}.{view["view"]}'
    print(f"  testing on {report['view']}")

    # 2. each query on that view ----------------------------------------------
    queries = [
        ("phrase", f'"{t}"', "what the page sends for an exact search"),
        ("bare", t, "plain word"),
        ("typo1_d1", f"{t1}~1", "1 off, 1 typo allowed: should find"),
        ("typo2_d1", f"{t2}~1", "2 off, 1 typo allowed: should find nothing new"),
        ("typo2_d2", f"{t2}~2", "2 off, 2 typos allowed: should find"),
        ("typo3_d2", f"{t3}~2", "3 off, 2 typos allowed: should find nothing"),
        ("typo3_d3", f"{t3}~3", "THE QUESTION: does ~3 work"),
        ("term_d3", f"{t}~3", "~3 on the real word"),
        ("typo3_bare_tilde", f"{t3}~", "~ alone (Lucene default is 2)"),
        ("typo1_float", f"{t1}~0.8", "old float style"),
        ("typo3_float", f"{t3}~0.5", "old float style, very loose"),
        ("prefix4", f"{pre4}*", "prefix wildcard"),
        ("prefix5", f"{pre5}*", "prefix wildcard"),
        ("single_char", t[:2] + "?" + t[3:], "one letter wildcard"),
        ("inner_star", f"{t[0]}*{t[-1]}", "first and last letter"),
        ("leading_star", f"*{t[2:]}", "leading wildcard, often turned off"),
        ("regex", f"/{t[:2]}.*{t[-2:]}/", "regular expression"),
        ("prefix_or_fuzzy", f"{pre5}* OR {t3}~2", "prefix or 2 typos together"),
        ("typo3_prefix", f"{t3[:4]}*", "prefix of the 3 off form"),
    ]
    print(f"\n2. {len(queries)} queries on that view")
    per = {}
    for label, q, why in queries:
        p = {"databaseName": view["db"], "viewName": view["view"], "query": q, "offset": 0, "limit": 200}
        st, ms, data, err = cl.req("GET", "/public/api/indexes/advanced-search", p)
        els = data.get("elements") if isinstance(data, dict) else None
        dist, words = Counter(), Counter()
        for e in els or []:
            f = (e or {}).get("fields")
            if isinstance(f, list):
                f = f[0] if f else {}
            d, w = closest(f, t)
            dist[d] += 1
            if w:
                words[w] += 1
        row = {"query": q, "why": why, "status": st, "ms": ms, "error": err,
               "count": data.get("count") if isinstance(data, dict) else None,
               "returned": len(els) if els is not None else None,
               "rows_by_letters_off": {str(k): v for k, v in sorted(dist.items())}}
        if args.show_snippets:
            row["closest_words"] = words.most_common(8)
        per[label] = row
        print(f"  {label:<18} {q:<22} {str(st):>4} {ms:>6} ms  count {row['count']}  off {row['rows_by_letters_off']}"
              + (f"  ERR {str(err)[:80]}" if err else ""))
    report["sections"]["per_view"] = per

    # 3. the interesting ones on the global search ------------------------------
    if not args.no_global:
        print("\n3. Same on the global search (about 1 minute each)")
        glob = {}
        for label in ("typo3_d3", "typo3_d2", "prefix5", "single_char"):
            q = per[label]["query"]
            print(f"  {label:<14} {q:<20} ...", end="", flush=True)
            res, _ = global_search(q)
            print(f" {res['status']} in {res['ms']} ms, {res['views']} views, off {res['rows_by_letters_off']}"
                  + (f"  ERR {str(res['error'])[:80]}" if res["error"] else ""))
            glob[label] = {"query": q, **res}
        report["sections"]["global"] = glob

    report["duration_s"] = round(time.time() - started, 1)
    report["total_calls"] = cl.calls
    report["run_at"] = datetime.now().isoformat(timespec="seconds")
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=1, default=str)
    print(f"\nDone in {report['duration_s']}s, {cl.calls} calls.")
    print(f"Output: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()