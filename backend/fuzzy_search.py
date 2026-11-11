"""Fuzzy Match: Denodo catalog data search, metadata lookup and Excel export.

Endpoints (same paths the page has always used):
  POST /search    rows that match a term, grouped by view
  POST /metadata  catalog metadata for a term or a view name
  POST /download  .xlsx of a search (one sheet per view, plus summary/metadata)

Speed notes:
  * one pooled HTTPS session to Denodo (no new TLS handshake per search)
  * the raw Denodo answer is cached per term for a few minutes, so changing the
    fuzzy threshold, switching sources or exporting does not hit Denodo again
  * identical searches that arrive together share one Denodo call
  * responses are orjson + gzip
"""
import base64
import gzip
import logging
import math
import re
import threading
import time
import zlib
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from io import BytesIO
from typing import Dict, List, Optional

import orjson
import requests
import xlsxwriter
from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response
from rapidfuzz.distance import OSA
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth

from auth import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Fuzzy Match"])

DEFAULT_THRESHOLD = 80
MAX_TERM_LEN = 500
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

_cfg = {
    "user": None,
    "password": None,
    "base_url": None,
    "verify": False,
    "connect_timeout": 10,
    "read_timeout": 180,
    "search_limit": 1000000,
    "cache_ttl": 1800,
    "native_query": True,
    "selectable_database_ids": None,   # None: every database Denodo has indexed
    "metadata_database_ids": None,     # None: metadata searches every database
    "known_sources": ["grail", "insight", "insight7", "lift", "rimdocs", "urp"],
    "server_ids": [1, 2],
}

# A database is picked by one int on the page. Server 1 keeps its plain id so
# saved picks still work, other servers are moved up by SERVER_SPAN.
SERVER_SPAN = 1_000_000


def db_key(server_id: int, db_id: int) -> int:
    return db_id if server_id == 1 else server_id * SERVER_SPAN + db_id


def split_key(key: int):
    """key -> (server_id, database_id). VDP-only keys (negative) have no id."""
    k = abs(key)
    sid = k // SERVER_SPAN or 1
    return sid, (k % SERVER_SPAN if sid != 1 else k)

_session = requests.Session()
_session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=32))
_session.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=32))


def configure(config: Optional[dict]) -> None:
    """Called once from app.py with dq_config.json. Optional keys under
    denodo_service_account: verify_ssl (bool or CA bundle path),
    connect_timeout, read_timeout, search_limit, cache_ttl_seconds, server_ids."""
    acct = (config or {}).get("denodo_service_account") or {}
    # Data Catalog VDP servers to read. Some databases (gra_dh_rimdocs_views)
    # only live on server 2.
    _cfg["server_ids"] = [int(x) for x in acct.get("server_ids") or [1, 2]]
    _cfg["user"] = acct.get("username")
    _cfg["password"] = acct.get("password")
    _cfg["base_url"] = (acct.get("base_url") or "").rstrip("/") or None
    _cfg["verify"] = acct.get("verify_ssl", False)
    _cfg["connect_timeout"] = float(acct.get("connect_timeout", 10))
    _cfg["read_timeout"] = float(acct.get("read_timeout", 180))
    _cfg["search_limit"] = int(acct.get("search_limit", 1000000))
    # Denodo takes 45 to 110s per search and does not cache, and its index is
    # refreshed on a schedule, so a longer cache costs little freshness.
    _cfg["cache_ttl"] = float(acct.get("cache_ttl_seconds", 1800))
    # databases a user may pick on the page. Not set: every database Denodo has
    # indexed (DEV: 70, 71, 89). Set it to limit what the portal exposes.
    # There is no default pick, a records search only covers what the user picked.
    sel = acct.get("selectable_database_ids")
    _cfg["selectable_database_ids"] = [int(x) for x in sel] if sel else None
    # metadata does not need the data index, so it can cover every database the
    # Data Catalog knows (sent as an empty databaseIds list). Set
    # metadata_database_ids to limit it.
    msel = acct.get("metadata_database_ids")
    _cfg["metadata_database_ids"] = [int(x) for x in msel] if msel else None
    # true: exact terms go to Denodo as a "quoted phrase" and fuzzy ones as word~
    # (the index is Lucene based). false: send the bare term like before.
    _cfg["native_query"] = bool(acct.get("native_query_syntax", True))
    _cfg["known_sources"] = [str(s).lower() for s in acct.get("known_sources")
                             or ["grail", "insight", "insight7", "lift", "rimdocs", "urp"]]


def is_configured() -> bool:
    return bool(_cfg["user"] and _cfg["password"] and _cfg["base_url"])


# ---------------------------------------------------------------------------
# Small TTL cache. Raw search answers can be big, so it is bounded by a
# weight (number of documents), not only by entry count.
# ---------------------------------------------------------------------------

class _TTLCache:
    def __init__(self, max_items: int, max_weight: int):
        self._items: "OrderedDict[str, tuple]" = OrderedDict()
        self._lock = threading.Lock()
        self._inflight: Dict[str, threading.Event] = {}
        self._max_items = max_items
        self._max_weight = max_weight
        self._weight = 0

    def _get_locked(self, key):
        hit = self._items.get(key)
        if not hit:
            return None
        expires, value, _ = hit
        if expires <= time.monotonic():
            self._drop_locked(key)
            return None
        self._items.move_to_end(key)
        return value

    def _drop_locked(self, key):
        hit = self._items.pop(key, None)
        if hit:
            self._weight -= hit[2]

    def _put_locked(self, key, value, weight):
        if weight > self._max_weight:
            return
        self._drop_locked(key)
        self._items[key] = (time.monotonic() + _cfg["cache_ttl"], value, weight)
        self._weight += weight
        while self._items and (len(self._items) > self._max_items or self._weight > self._max_weight):
            old_key = next(iter(self._items))
            self._drop_locked(old_key)

    def get_or_load(self, key, loader, weigh=lambda v: 1):
        """Returns (value, from_cache). Concurrent callers for the same key
        wait for the first one instead of all going to Denodo."""
        while True:
            with self._lock:
                value = self._get_locked(key)
                if value is not None:
                    return value, True
                waiter = self._inflight.get(key)
                if waiter is None:
                    event = threading.Event()
                    self._inflight[key] = event
                    break
            # someone is already loading it. If they fail, the next loop
            # makes us the loader.
            waiter.wait(timeout=_cfg["read_timeout"] + 15)

        try:
            value = loader()
            with self._lock:
                self._put_locked(key, value, weigh(value))
            return value, False
        finally:
            with self._lock:
                self._inflight.pop(key, None)
            event.set()

    def invalidate(self, key):
        with self._lock:
            self._drop_locked(key)


_search_cache = _TTLCache(max_items=12, max_weight=400_000)
_meta_cache = _TTLCache(max_items=512, max_weight=512)
_rows_cache = _TTLCache(max_items=200, max_weight=2000)
_fields_cache = _TTLCache(max_items=4000, max_weight=4000)


# ---------------------------------------------------------------------------
# Denodo calls
# ---------------------------------------------------------------------------

def _denodo_post(path: str, payload: dict):
    if not is_configured():
        raise HTTPException(status_code=500, detail="Service account not configured")
    url = f"{_cfg['base_url']}{path}"
    try:
        resp = _session.post(
            url,
            auth=HTTPBasicAuth(_cfg["user"], _cfg["password"]),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            data=orjson.dumps(payload),
            verify=_cfg["verify"],
            timeout=(_cfg["connect_timeout"], _cfg["read_timeout"]),
        )
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Denodo took too long to answer. Try a more specific term.")
    except requests.exceptions.ConnectionError:
        raise HTTPException(status_code=503, detail="Could not reach Denodo. Try again in a moment.")

    # Never hand Denodo's 401/403 to the browser: the page would read it as
    # its own sign-in expiring.
    if resp.status_code in (401, 403):
        logger.error(f"Denodo rejected the service account ({resp.status_code}) on {path}")
        raise HTTPException(status_code=502, detail="Denodo rejected the service account")
    if resp.status_code != 200:
        logger.error(f"Denodo {path} -> {resp.status_code}: {resp.text[:500]}")
        raise HTTPException(status_code=502, detail=f"Denodo returned {resp.status_code}")
    try:
        return orjson.loads(resp.content)
    except orjson.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Denodo sent an answer that is not JSON")


def _iter_groups(data):
    if isinstance(data, list):
        yield from data
    elif isinstance(data, dict) and "groupSearch" in data:
        gs = data.get("groupSearch") or {}
        yield from gs.get("groups") or []
        if "documents" in gs:
            yield gs


def _doc_count(data) -> int:
    n = 0
    for g in _iter_groups(data):
        if isinstance(g, dict):
            n += len(g.get("documents") or [])
    return max(n, 1)


_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def edit_distance_for(threshold: int) -> int:
    """The page's similarity maps to Lucene's edit distance: 90+ allows one
    typo per word, anything lower allows two (Lucene's maximum)."""
    return 1 if threshold >= 90 else 2


MIN_PREFIX = 3   # 'a*' would pull in half the index


def split_endings(term: str):
    """'customer*' -> ('customer', True): also match longer words that start
    with it (customers, customer_id). Only a trailing * means this; a * inside
    the term is just a character."""
    t = (term or "").strip()
    if t.endswith("*") and not t.endswith("\\*"):
        return t.rstrip("*").strip(), True
    return t, False


def denodo_text(term: str, fuzzy: bool, threshold: int = 80) -> str:
    """What goes in Denodo's "text". Its index takes Lucene syntax (checked on
    DEV with probe 4):
      customer              -> "customer"   a phrase, so two words stay together
      customer~0.9          -> customer~1   per word, Denodo finds the typos
      customer*             -> customer*    also longer words
      customer*~0.9         -> customer* OR customer~1
    Lucene stops at 2 typos (~3 acts as ~2) and a leading * is a 500, so a
    user's own * and ? are always escaped. Wildcard and fuzzy terms skip the
    analyzer, so they go in lower case like the index."""
    base, ends = split_endings(term)
    if not _cfg["native_query"]:
        return term
    if not (fuzzy or ends):
        return '"' + base.replace("\\", "\\\\").replace('"', '\\"') + '"'
    dist = edit_distance_for(threshold) if fuzzy else 0
    parts = []
    for w in base.split():
        esc = _LUCENE_SPECIAL.sub(r"\\\1", w.lower())
        opts = []
        if ends and len(w) >= MIN_PREFIX:
            opts.append(f"{esc}*")
        if fuzzy:
            opts.append(f"{esc}~{dist}")
        if not opts:
            opts.append(esc)
        parts.append(opts[0] if len(opts) == 1 else "(" + " OR ".join(opts) + ")")
    if len(parts) == 1 and parts[0].startswith("("):
        return parts[0][1:-1]
    return " ".join(parts)


def _search_raw(term: str, fuzzy: bool, refresh: bool, threshold: int, database_ids: List[int], server_id: int = 1):
    text = denodo_text(term, fuzzy, threshold)
    dbs = sorted(database_ids)
    key = f"s{server_id}|{','.join(map(str, dbs))}|{text}"
    if refresh:
        _search_cache.invalidate(key)
    payload = {
        "databaseIds": dbs,
        "limit": _cfg["search_limit"],
        "offset": 0,
        "text": text,
        "viewIds": [],
    }
    return _search_cache.get_or_load(
        key,
        lambda: _denodo_post(f"/public/api/search/data?serverId={server_id}", payload),
        weigh=_doc_count,
    )


def _metadata_raw(payload: dict, server_id: int = 1):
    key = f"s{server_id}|" + orjson.dumps(payload, option=orjson.OPT_SORT_KEYS).decode()
    return _meta_cache.get_or_load(
        key, lambda: _denodo_post(f"/public/api/search/metadata?serverId={server_id}", payload)
    )


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def parse_fuzzy_query(search_text: str):
    """'customer' -> exact word match. 'customer~0.8' or 'customer~80' -> fuzzy
    with that similarity. 'customer~' -> fuzzy at the default 80."""
    text = (search_text or "").strip()
    if "~" not in text:
        return text, 100, False
    term, _, raw = text.rpartition("~")
    term = term.replace("~", " ").strip()
    raw = raw.strip()
    try:
        threshold = float(raw) if raw else float(DEFAULT_THRESHOLD)
        if not math.isfinite(threshold):
            raise ValueError
    except ValueError:
        # 'foo~bar' is not a threshold, keep the words
        term = f"{term} {raw}".strip()
        threshold = float(DEFAULT_THRESHOLD)
    if threshold <= 1:
        threshold *= 100
    threshold = int(round(min(100.0, max(0.0, threshold))))
    return term, threshold, True


_WORD_RE = re.compile(r"[^\W_]+")


def _make_matcher(term: str, threshold: int, fuzzy: bool):
    """Returns f(values) -> True when any cell of the row matches."""
    term, ends = split_endings(term)
    if fuzzy or ends:
        # Same rule as Denodo's word~N: a word in the cell within N letter
        # changes (insert, delete, replace, swap) of a word in the term, or
        # with endings on, a word that starts with it.
        dist = edit_distance_for(threshold) if fuzzy else 0
        needles = [w for w in _WORD_RE.findall(term.lower()) if w]
        prefixes = [n for n in needles if len(n) >= MIN_PREFIX] if ends else []
        osa = OSA.distance

        def match(values) -> bool:
            for v in values:
                if not v:
                    continue
                for tok in _WORD_RE.findall(v.lower()):
                    if len(tok) < 2:
                        continue
                    if any(tok.startswith(p) for p in prefixes):
                        return True
                    if any(abs(len(tok) - len(n)) <= dist and osa(n, tok, score_cutoff=dist) <= dist
                           for n in needles):
                        return True
            return False
        return match

    # Whole word, case-insensitive. Lookarounds instead of \b so terms that
    # start or end with a symbol (C++, #ID, 10%) still match. The cells are
    # joined with a non-word separator so one regex pass covers the row.
    pattern = re.compile(r"(?<!\w)" + re.escape(term) + r"(?!\w)", re.IGNORECASE)
    search = pattern.search
    return lambda values: search("\x1f".join(values)) is not None


_VIEW_NAME_RE = re.compile(r"^\s*([^\s(]+)")


def _view_name(group: dict) -> str:
    m = _VIEW_NAME_RE.match(str(group.get("displayName") or ""))
    if m:
        return m.group(1)
    return str(group.get("name") or "Unknown")


def datasource_of(view_name: str) -> str:
    """First known source named in the view, e.g. bv_rdh_grail_tbl_x -> grail,
    f_p_iv_urp_src_index -> urp. Anything else is 'other'."""
    tokens = view_name.lower().split("_")
    for t in tokens:
        if t in _cfg["known_sources"]:
            return t
    return "other"


def _cell(value) -> str:
    return "" if value is None else str(value)


_DB_IN_NAME_RE = re.compile(r"\(([^)]+)\)\s*$")
DENODO_ROWS_PER_VIEW = 10      # what Denodo returns per view, whatever the limit
DENODO_VALUE_CAP = 300         # text values come back cut to this many characters
DENODO_HITS_CAP = 10000        # 'length' stops counting here


def _group_fields(group: dict):
    """All field names. Older answers have no group level list, so fall back
    to the documents' own keys."""
    names = group.get("fieldNames") or []
    if not names:
        seen = {}
        for doc in group.get("documents") or []:
            f = doc.get("fields") if isinstance(doc, dict) else None
            if isinstance(f, list):
                f = f[0] if f else None
            if isinstance(f, dict):
                for k in f:
                    seen.setdefault(k, None)
        names = list(seen)
    return names


def match_results(data, term: str, threshold: int, fuzzy: bool,
                  datasources: Optional[set] = None,
                  max_records_per_view: Optional[int] = None) -> Dict[str, dict]:
    """Denodo decides which rows match. Its index sees the full text, but
    the values it hands back are cut at 300 characters, so a local check would
    wrongly drop rows whose match sits further in (seen on DEV). Here every row
    Denodo returned is kept; each gets a 'hit' flag saying whether the match is
    visible in the returned text, and hits are listed first."""
    matches = _make_matcher(term, threshold if fuzzy else 100, fuzzy)
    out: Dict[str, dict] = {}

    for group in _iter_groups(data):
        if not isinstance(group, dict):
            continue
        view = _view_name(group)
        if datasources and datasource_of(view) not in datasources:
            continue
        m = _DB_IN_NAME_RE.search(str(group.get("displayName") or ""))
        database = m.group(1) if m else ""
        names = _group_fields(group)
        rows = []
        for doc in group.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            fields = doc.get("fields", {})
            if isinstance(fields, list):
                fields = fields[0] if fields else {}
            if not isinstance(fields, dict):
                continue
            record = {n: _cell(fields.get(n)) for n in names}
            rows.append((record, bool(matches(record.values())), str(doc.get("self") or "")))
        if not rows:
            continue

        rows.sort(key=lambda r: not r[1])          # stable: hits first, Denodo order kept
        if max_records_per_view is not None:
            rows = rows[:max_records_per_view]

        key = view
        if key in out and out[key]["database"] != database:
            key = f"{view} ({database})"
        try:
            total = int(group.get("length"))
        except (TypeError, ValueError):
            total = len(rows)
        total = max(total, len(rows))
        out[key] = {
            "view": view,
            "database": database,
            "datasource": datasource_of(view),
            "records": [r[0] for r in rows],
            "hits": [r[1] for r in rows],
            "links": [r[2] for r in rows],
            "record_count": total,
            "rows_returned": len(rows),
            "total_capped": total >= DENODO_HITS_CAP,
            "truncated": total > len(rows),
            "value_cap": DENODO_VALUE_CAP,
        }
    return out


def _clean_sources(datasources) -> Optional[set]:
    if not datasources:
        return None
    return {str(d).strip().lower() for d in datasources if str(d).strip()} or None


def _json(request: Request, obj) -> Response:
    body = orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS)
    headers = {"Cache-Control": "no-store"}
    if len(body) > 4096 and "gzip" in request.headers.get("accept-encoding", "").lower():
        body = gzip.compress(body, compresslevel=5)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(content=body, media_type="application/json", headers=headers)


def _has(auth: dict, perm: str) -> bool:
    return (auth.get("permissions") or {}).get(perm) is True


def _same_name(keys, wait: bool = False) -> Dict[int, List[int]]:
    """Primary key -> every key with the same database name. Servers 1 and 2
    share most databases, so the page shows each name once and a search goes
    to every server that has it. Catalog keys come first, then lower servers."""
    names = _all_names(wait)
    groups: Dict[str, List[int]] = {}
    for k in keys:
        n = names.get(k)
        groups.setdefault(n.lower() if n else f"#{k}", []).append(k)
    out = {}
    for ks in groups.values():
        ks.sort(key=lambda k: (k < 0, split_key(k)[0], abs(k)))
        out[ks[0]] = ks
    return out


def selectable_ids(wait: bool = True) -> List[int]:
    """Databases a user may pick: the configured list, else every database
    Denodo has, indexed or not."""
    if _cfg["selectable_database_ids"]:
        return list(_cfg["selectable_database_ids"])
    return sorted(_all_names(wait))


def _allowed_dbs(database_ids) -> List[int]:
    if not database_ids:
        raise HTTPException(status_code=400, detail="Pick at least one database to search")
    try:
        wanted = {int(x) for x in database_ids}
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="database_ids must be numbers")
    bad = wanted - set(selectable_ids())
    if bad:
        raise HTTPException(status_code=403, detail=f"Database(s) not enabled for Fuzzy Match: {sorted(bad)}")
    return sorted(wanted)


def _search_notes(term: str, results: dict) -> List[str]:
    notes = []
    term, ends = split_endings(term)
    if ends:
        notes.append("Longer words that start with the term are included. Short terms can match a lot.")
    if re.search(r"[^\w\s]", term):
        notes.append("Symbols such as + # / are ignored by the Denodo index, so matches are on the letters and numbers only.")
    if results and not any(any(v["hits"]) for v in results.values()):
        notes.append("Denodo matched these rows on text beyond the first 300 characters it returns. Open a row's full record to see it.")
    if any(v["total_capped"] for v in results.values()):
        notes.append("Some views have 10,000 or more matches. Try a more specific term.")
    return notes


def _skip_notes(database_ids) -> List[str]:
    # only VDP-only databases are left out, Denodo has no id for them
    siblings = _same_name(_all_names(wait=False).keys())
    skip = [i for i in (database_ids or []) if all(k < 0 for k in siblings.get(i, [i]))]
    if not skip:
        return []
    names = _all_names(wait=False)
    return [f"{', '.join(names.get(i, str(i)) for i in skip)} is not in the Data Catalog, "
            "so Denodo can't search it."]


def _run_search(search_text: str, datasources=None, max_records_per_view=None, refresh=False, database_ids=None):
    term, threshold, fuzzy = parse_fuzzy_query(search_text)
    if not term:
        raise HTTPException(status_code=400, detail="Enter a search term")
    if len(term) > MAX_TERM_LEN:
        raise HTTPException(status_code=400, detail=f"Search term is longer than {MAX_TERM_LEN} characters")
    # every picked database goes to Denodo, indexed or not, whatever it finds is shown.
    # VDP-only ones (negative keys) have no catalog id, so they can't be sent.
    by_server: Dict[int, List[int]] = {}
    siblings = _same_name(_all_names(wait=False).keys())
    for k in _allowed_dbs(database_ids):
        for kk in siblings.get(k, [k]):
            if kk >= 0:
                sid, dbid = split_key(kk)
                by_server.setdefault(sid, []).append(dbid)
    groups, cached = [], True
    for sid, dbs in sorted(by_server.items()):
        try:
            data, c = _search_raw(term, fuzzy, refresh, threshold, dbs, sid)
        except HTTPException:
            # if Denodo chokes on a database without an index, try again with just the indexed ones
            idx = [d for d in dbs if db_key(sid, d) in _database_names(wait=False)]
            if not idx or idx == dbs:
                raise
            logger.warning(f"Denodo search failed on server {sid} with {dbs}, retrying with indexed {idx}")
            data, c = _search_raw(term, fuzzy, refresh, threshold, idx, sid)
        groups.extend(_iter_groups(data))
        cached = cached and c
    data = groups
    cached = cached and bool(by_server)
    results = match_results(data, term, threshold, fuzzy, _clean_sources(datasources), max_records_per_view)
    return term, threshold, fuzzy, results, cached


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/search")
def search_catalog(
        request: Request,
        search_text: str = Body(...),
        datasources: Optional[List[str]] = Body(default=None),
        max_records_per_view: Optional[int] = Body(default=None, ge=1),
        refresh: bool = Body(default=False),
        database_ids: Optional[List[int]] = Body(default=None),
        auth: dict = Depends(require_permission("viewData")),
):
    """Rows matching the term, grouped by view. Views with no match are left out.
    refresh=true skips the few-minute cache and asks Denodo again."""
    t0 = time.perf_counter()
    term, threshold, fuzzy, results, cached = _run_search(search_text, datasources, max_records_per_view, refresh,
                                                          database_ids=database_ids)
    return _json(request, {
        "results": results,
        "term": term,
        "fuzzy": fuzzy,
        "threshold": threshold if fuzzy else None,
        "edit_distance": edit_distance_for(threshold) if fuzzy else 0,
        "rows_per_view_cap": DENODO_ROWS_PER_VIEW,
        "notes": _search_notes(term, results) + _skip_notes(database_ids),
        "view_count": len(results),
        "total_records": sum(v["record_count"] for v in results.values()),
        "cached": cached,
        "took_ms": int((time.perf_counter() - t0) * 1000),
    })


def _metadata_items(data, server_id: int = 1) -> list:
    if isinstance(data, dict):
        elements = data.get("elements") or []
    elif isinstance(data, list):
        elements = data
    else:
        elements = []

    items = []
    for el in elements:
        if not isinstance(el, dict):
            continue
        db_info = el.get("database") if isinstance(el.get("database"), dict) else {}
        item = {k: v for k, v in el.items() if k != "database"}
        item.update({
            "id": el.get("id", ""),
            "name": el.get("name", "Unknown"),
            "description": el.get("description") or "",
            "database_name": db_info.get("databaseName", ""),
            "database_id": db_info.get("databaseId", ""),
            "categories": el.get("categories") or [],
            "tags": el.get("tags") or [],
            "matched_fields": el.get("matchedFields", 0),
            "matched_fields_tagged": el.get("matchedFieldsTagged", 0),
            "matched_custom_properties": el.get("matchedCustomProperties", 0),
            "score": el.get("ranking", 0),
            "name_matches": el.get("nameMatchesOffsetData") or [],
            "description_matches": el.get("descriptionMatchesOffsetData") or [],
            "server_id": server_id,
        })
        items.append(item)
    return items


def _metadata_payload(search_text="", element_type="Views", with_endorsements=False, with_warnings=False,
                      with_deprecations=False, category_ids=None, tag_ids=None, database_ids=None,
                      search_type="EXACT_MATCH", offset=0, limit=10000000, where_to_search_list=None):
    payload = {
        "text": search_text or "",
        "elementType": element_type,
        "withEndorsements": with_endorsements,
        "withWarnings": with_warnings,
        "withDeprecations": with_deprecations,
        "categoryIds": category_ids or [],
        "tagIds": tag_ids or [],
        # metadata only: an empty list is no filter, i.e. every database
        "databaseIds": list(database_ids or []),
        "searchType": search_type,
        "offset": offset,
        "limit": limit,
    }
    if where_to_search_list:
        payload["whereToSearchList"] = list(where_to_search_list)
    return payload


def _db_id(database: Optional[str]) -> Optional[int]:
    """Denodo database name -> key, indexed ones first (never waits)."""
    if not database:
        return None
    for names in (_database_names(wait=False), _all_names(wait=False)):
        for k, n in names.items():
            if n == database and k >= 0:
                return k
    return None


def _server_of(database: Optional[str]) -> int:
    """Which server a database (by name) lives on. Server 1 when unknown."""
    k = _db_id(database)
    return split_key(k)[0] if k is not None else 1


def metadata_for_view(view_name: str, database: Optional[str] = None) -> Optional[dict]:
    """The catalog entry whose name is exactly this view, or None. Looks in the
    view's own database when it is known, else in the default ones."""
    db = _db_id(database)
    sid = split_key(db)[0] if db is not None else 1
    ids = [split_key(db)[1]] if db is not None else [split_key(k)[1] for k in metadata_ids() if split_key(k)[0] == sid]
    data, _ = _metadata_raw(_metadata_payload(search_text=view_name, where_to_search_list=["ELEMENT_NAME"],
                                              database_ids=ids), sid)
    for item in _metadata_items(data, sid):
        if item.get("name") == view_name and (not database or item.get("database_name") in ("", database)):
            return item
    return None


@router.post("/metadata")
def get_metadata(
        request: Request,
        search_text: str = Body(default=""),
        element_type: str = Body(default="Views"),
        with_endorsements: bool = Body(default=False),
        with_warnings: bool = Body(default=False),
        with_deprecations: bool = Body(default=False),
        category_ids: list = Body(default=[]),
        tag_ids: list = Body(default=[]),
        database_ids: list = Body(default=[]),
        search_type: str = Body(default="EXACT_MATCH"),
        offset: int = Body(default=0, ge=0),
        limit: int = Body(default=10000000, ge=1),
        datasources: Optional[List[str]] = Body(default=None),
        exact_name: bool = Body(default=False),
        where_to_search_list: Optional[List[str]] = Body(default=None),
        database: Optional[str] = Body(default=None),
        auth: dict = Depends(require_permission("viewMetadata")),
):
    """Catalog metadata. exact_name=true keeps only the entry named search_text;
    database (a name) narrows that lookup to the view's own database."""
    t0 = time.perf_counter()
    term = (search_text or "").strip()
    if len(term) > MAX_TERM_LEN:
        raise HTTPException(status_code=400, detail=f"Search term is longer than {MAX_TERM_LEN} characters")
    # every database unless the config limits it; a database_ids sent by an
    # older page is ignored
    database_ids = metadata_ids()
    if database:
        # a view's own entry: narrow to its database when we know its id
        db = _db_id(database)
        if db is not None and (not database_ids or db in database_ids):
            database_ids = [db]
    where = ["ELEMENT_NAME"] if exact_name else where_to_search_list
    if database:
        servers = [_server_of(database)]
    else:
        servers = list(_cfg["server_ids"])
    items, total, cached, errors = [], 0, True, []
    for sid in servers:
        # database ids are per server, so a narrowed lookup only goes to its own server
        ids = [split_key(k)[1] for k in database_ids if k >= 0 and split_key(k)[0] == sid]
        if database_ids and not ids:
            continue
        payload = _metadata_payload(term, element_type, with_endorsements, with_warnings, with_deprecations,
                                    category_ids, tag_ids, ids, search_type, offset, limit, where)
        try:
            data, c = _metadata_raw(payload, sid)
        except HTTPException as e:
            errors.append(e)
            continue
        got = _metadata_items(data, sid)
        items.extend(got)
        total += data.get("elementsCount", len(got)) if isinstance(data, dict) else len(got)
        cached = cached and c
    if errors and not items and len(errors) == len(servers):
        raise errors[0]

    if exact_name:
        items = [i for i in items if i.get("name") == term
                 and (not database or i.get("database_name") in ("", database))]
    sources = _clean_sources(datasources)
    if sources:
        items = [i for i in items if datasource_of(str(i.get("name") or "")) in sources]

    return _json(request, {
        "metadata": items,
        "total_count": total,
        "cached": cached,
        "took_ms": int((time.perf_counter() - t0) * 1000),
    })


_FIELD_NAME_KEYS = ("fieldName", "name", "columnName", "field")


def _field_names(data, skip: str = "") -> List[str]:
    """Field names out of Denodo's matched-with-search-term answer. Its shape
    isn't documented, so every dict that names something is read, in order."""
    out: List[str] = []
    seen = set()

    def walk(x, depth=0):
        if depth > 6:
            return
        if isinstance(x, list):
            for y in x:
                walk(y, depth + 1)
        elif isinstance(x, dict):
            for k in _FIELD_NAME_KEYS:
                v = x.get(k)
                if isinstance(v, str) and v and v != skip and v not in seen:
                    seen.add(v)
                    out.append(v)
                    break
            for k, v in x.items():
                if isinstance(v, (list, dict)) and k not in ("tags", "categories", "database"):
                    walk(v, depth + 1)
    walk(data)
    return out


@router.post("/metadata/matched-fields")
def metadata_matched_fields(
        request: Request,
        element_id: int = Body(..., ge=0),
        search_text: str = Body(...),
        view: Optional[str] = Body(default=None),
        server_id: int = Body(default=1, ge=1),
        auth: dict = Depends(require_permission("viewMetadata")),
):
    """Which fields of one catalog view matched the term (the metadata search
    only says how many). One read-only GET to Denodo, cached."""
    term = (search_text or "").strip()
    if not term:
        raise HTTPException(status_code=400, detail="Enter a search term")
    if len(term) > MAX_TERM_LEN:
        raise HTTPException(status_code=400, detail=f"Search term is longer than {MAX_TERM_LEN} characters")
    from urllib.parse import urlencode
    params = {"filter": term, "whereToSearch": ["FIELD_NAME", "FIELD_DESC"], "searchType": "ALL_WORDS", "serverId": server_id}
    path = f"/public/api/search/views/{int(element_id)}/matched-with-search-term?" + urlencode(params, doseq=True)
    data, cached = _fields_cache.get_or_load(f"{server_id}|{element_id}|{term.lower()}", lambda: _denodo_post_get(path))
    fields = _field_names(data, skip=view or "")
    return _json(request, {"element_id": element_id, "fields": fields[:500], "count": len(fields), "cached": cached})


# ---------------------------------------------------------------------------
# Databases and full records
# ---------------------------------------------------------------------------

_db_names: Dict[int, str] = {}
NAMES_TTL = 86400     # the lists hardly change
NAMES_RETRY = 300     # after a failed load, don't make every request wait on Denodo again
_names_state = {
    "index": {"names": _db_names, "at": 0.0, "failed": None, "lock": threading.Lock()},
    "all": {"names": {}, "at": 0.0, "failed": None, "lock": threading.Lock()},
}


def _reset_names() -> None:
    for st in _names_state.values():
        st["names"].clear()
        st["at"] = 0.0
        st["failed"] = None


def _rows_to_names(rows, id_keys=("id", "databaseId"), name_keys=("name", "databaseName")) -> Dict[int, str]:
    if isinstance(rows, dict):
        rows = rows.get("content") or rows.get("databases") or rows.get("elements") or []
    out: Dict[int, str] = {}
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        i = next((r[k] for k in id_keys if r.get(k) is not None), None)
        n = next((r[k] for k in name_keys if r.get(k)), None)
        try:
            if i is not None:
                out[int(i)] = str(n or i)
        except (TypeError, ValueError):
            continue
    return out


def _load_index_names() -> Dict[int, str]:
    # about a minute on DEV
    # servers are asked at the same time, one slow server shouldn't hold the other up
    def one(sid):
        try:
            got = _rows_to_names(_denodo_post_get(f"/public/api/indexes/databases?serverId={sid}"),
                                 id_keys=("databaseId", "id"), name_keys=("databaseName", "name"))
        except HTTPException:
            logger.warning(f"Could not load the indexed databases of Denodo server {sid}")
            return {}
        return {db_key(sid, i): n for i, n in got.items()}
    out: Dict[int, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(_cfg["server_ids"]))) as pool:
        for got in pool.map(one, _cfg["server_ids"]):
            out.update(got)
    return out


def _vdp_id(name: str, server_id: int = 1) -> int:
    # databases that are in VDP but not synced to the Data Catalog have no id,
    # so they get a stable negative key. They can't be searched.
    return -db_key(server_id, zlib.crc32(name.encode()) % (SERVER_SPAN - 1) + 1)


def _load_all_names() -> Dict[int, str]:
    """Every database on every configured server: Data Catalog ones with their
    ids (user/databases as the fallback), plus VDP-only ones by name."""
    out: Dict[int, str] = {}
    for sid in _cfg["server_ids"]:
        names: Dict[int, str] = {}
        for path in (f"/public/api/database-management/local/databases?serverId={sid}",
                     f"/public/api/database-management/user/databases?serverId={sid}"):
            try:
                names = _rows_to_names(_denodo_post_get(path))
            except HTTPException:
                continue
            if names:
                break
        try:
            vdp = _denodo_post_get(f"/public/api/database-management/vdp/databases?serverId={sid}")
        except HTTPException:
            vdp = []
        for i, n in names.items():
            out[db_key(sid, i)] = n
        known = {n.lower() for n in names.values()}
        for n in vdp if isinstance(vdp, list) else []:
            if isinstance(n, str) and n and n.lower() not in known:
                out[_vdp_id(n, sid)] = n
                known.add(n.lower())
    return out


def _cached_names(kind: str, loader, wait: bool) -> Dict[int, str]:
    """Cached id -> name list. wait=False never blocks: it returns what is
    there and starts a load in the background. Concurrent callers share one
    load instead of each asking Denodo."""
    st = _names_state[kind]
    now = time.monotonic()
    if st["names"] and now - st["at"] < NAMES_TTL:
        return dict(st["names"])
    recently_failed = st["failed"] is not None and now - st["failed"] < NAMES_RETRY

    def load():
        with st["lock"]:
            n = time.monotonic()
            if st["names"] and n - st["at"] < NAMES_TTL:
                return
            if st["failed"] is not None and n - st["failed"] < NAMES_RETRY:
                return
            try:
                got = loader()
            except HTTPException:
                got = {}
            if got:
                st["names"].clear()
                st["names"].update(got)
                st["at"] = time.monotonic()
                st["failed"] = None
            else:
                st["failed"] = time.monotonic()

    if not wait:
        if not recently_failed and not st["lock"].locked() and is_configured():
            threading.Thread(target=load, daemon=True).start()
    elif not recently_failed:
        load()
    return dict(st["names"])


def names_loading() -> bool:
    """True while either list is still on its first load (or loading again).
    The page keeps asking until this is false."""
    now = time.monotonic()
    for st in _names_state.values():
        if st["lock"].locked():
            return True
        if not st["names"] and (st["failed"] is None or now - st["failed"] >= NAMES_RETRY) and is_configured():
            return True
    return False


def index_state() -> str:
    """ok, loading or failed, for the list of indexed databases."""
    st = _names_state["index"]
    if st["names"]:
        return "ok"
    if st["lock"].locked() or st["failed"] is None:
        return "loading"
    return "failed"


def _database_names(wait: bool = True) -> Dict[int, str]:
    """id -> name for the databases that have a data search index."""
    return _cached_names("index", _load_index_names, wait)


def _all_names(wait: bool = True) -> Dict[int, str]:
    """id -> name for every database, indexed ones included even if the
    full list failed to load."""
    names = _cached_names("all", _load_all_names, wait)
    names.update(_database_names(wait))
    return names


def _not_indexed(ids) -> List[int]:
    idx = _database_names(wait=False)
    if not idx:
        return []   # index list not loaded, let Denodo decide
    return [i for i in ids if i not in idx]


def metadata_ids() -> List[int]:
    """Databases a metadata search covers. Empty means every database: the
    Data Catalog takes an empty databaseIds list as no filter."""
    return list(_cfg["metadata_database_ids"] or [])


def _denodo_post_get(path: str):
    if not is_configured():
        raise HTTPException(status_code=500, detail="Service account not configured")
    try:
        resp = _session.get(f"{_cfg['base_url']}{path}", auth=HTTPBasicAuth(_cfg["user"], _cfg["password"]),
                            headers={"Accept": "application/json"}, verify=_cfg["verify"],
                            timeout=(_cfg["connect_timeout"], _cfg["read_timeout"]))
    except requests.exceptions.RequestException:
        raise HTTPException(status_code=503, detail="Could not reach Denodo. Try again in a moment.")
    if resp.status_code != 200:
        logger.error(f"Denodo GET {path.split('?')[0]} -> {resp.status_code}: {resp.text[:300]}")
        raise HTTPException(status_code=502, detail=f"Denodo returned {resp.status_code}")
    try:
        return orjson.loads(resp.content)
    except orjson.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Denodo sent an answer that is not JSON")


def warm_up():
    """Fetch the slow database list in the background at startup."""
    if is_configured():
        _all_names(wait=False)


@router.get("/search/databases")
def search_databases(auth: dict = Depends(require_permission("viewData", "viewMetadata", require_all=False))):
    """Databases a user may pick for a records search. Nothing is picked by
    default. Metadata always searches every database, so it needs no list."""
    # never block here: Denodo takes about a minute for this list. The page
    # asks again while "loading" is true.
    names = _all_names(wait=False)
    indexed = _database_names(wait=False)
    idx_state = index_state()
    out = []
    for i, ks in _same_name(selectable_ids(wait=False)).items():
        cat = [k for k in ks if k >= 0]
        out.append({
            "id": i,
            "name": names.get(i, str(i)),
            # None until Denodo's index list is in, so the page doesn't say "not indexed" too early
            "indexed": any(k in indexed for k in ks) if idx_state == "ok" else None,
            "catalog": bool(cat),
            "servers": sorted({split_key(k)[0] for k in cat}),
            "db_id": split_key(i)[1] if i >= 0 else None,
        })
    return {"loading": names_loading(), "index_state": idx_state, "databases": out}


def _check_database(database: str) -> None:
    allowed = {n for i, n in _all_names().items() if i in set(selectable_ids())}
    if not allowed:
        raise HTTPException(status_code=503, detail="Still loading the database list from Denodo. Try again in a minute.")
    if database not in allowed:
        raise HTTPException(status_code=400, detail="That database is not enabled for Fuzzy Match")


# Per-view reads (advanced-search, live-data) run one by one on Denodo's side
# and start failing permission checks when many arrive together (DEV probe:
# 854 of 935 parallel calls failed). Keep only a few in flight across all users.
_view_calls = threading.BoundedSemaphore(3)
_VIEW_NAME_OK = re.compile(r"^[A-Za-z0-9_$]{1,200}$")
VIEW_PAGE_MAX = 500          # a 10,000-row page took 31s on DEV


def _ordered(rec: dict, columns: Optional[List[str]]) -> dict:
    """Per-view answers leave out empty fields and do not keep the view's field
    order. Lay every row out like the global search did: same columns, same
    order, blanks for missing ones, anything new at the end."""
    if not columns:
        return rec
    out = {c: rec.get(c, "") for c in columns}
    for k, v in rec.items():
        if k not in out:
            out[k] = v
    return out


def view_rows(database: str, view: str, term: str, threshold: int, fuzzy: bool, offset: int, limit: int,
              columns: Optional[List[str]] = None) -> dict:
    """One page of matching rows from one view, from Denodo's per-view search.
    Unlike the global search this is not capped at 10 rows."""
    params = {"databaseName": database, "viewName": view, "query": denodo_text(term, fuzzy, threshold),
              "offset": offset, "limit": limit, "serverId": _server_of(database)}
    from urllib.parse import urlencode
    with _view_calls:
        data = _denodo_post_get("/public/api/indexes/advanced-search?" + urlencode(params))
    elements = data.get("elements") if isinstance(data, dict) else None
    matches = _make_matcher(term, threshold if fuzzy else 100, fuzzy)
    records, hits, links = [], [], []
    for el in elements or []:
        if not isinstance(el, dict):
            continue
        f = el.get("fields")
        if isinstance(f, list):
            f = f[0] if f else {}
        if not isinstance(f, dict):
            continue
        rec = _ordered({k: _cell(v) for k, v in f.items()}, columns)
        records.append(rec)
        hits.append(bool(matches(rec.values())))
        links.append(str(el.get("self") or ""))
    try:
        count = int(data.get("count"))
    except (TypeError, ValueError, AttributeError):
        count = offset + len(records)
    return {"records": records, "hits": hits, "links": links, "count": count, "offset": offset}


@router.post("/search/view-rows")
def search_view_rows(
        request: Request,
        search_text: str = Body(...),
        database: str = Body(...),
        view: str = Body(...),
        offset: int = Body(default=0, ge=0, le=1000000),
        limit: int = Body(default=200, ge=1, le=VIEW_PAGE_MAX),
        columns: Optional[List[str]] = Body(default=None),
        auth: dict = Depends(require_permission("viewData")),
):
    """More rows for one view than the 10 the global search gives. 'columns' is
    the field order the page already shows for this view."""
    term, threshold, fuzzy = parse_fuzzy_query(search_text)
    if not term or len(term) > MAX_TERM_LEN:
        raise HTTPException(status_code=400, detail="Enter a search term")
    if not _VIEW_NAME_OK.match(view):
        raise HTTPException(status_code=400, detail="That view name is not valid")
    _check_database(database)
    columns = [str(c) for c in (columns or [])][:2000] or None
    key = orjson.dumps([database, view, denodo_text(term, fuzzy, threshold), threshold if fuzzy else 100,
                       offset, limit, columns]).decode()
    page, cached = _rows_cache.get_or_load(key, lambda: view_rows(database, view, term, threshold, fuzzy, offset, limit,
                                                                  columns),
                                           weigh=lambda p: max(1, len(p["records"]) // 50))
    return _json(request, {**page, "cached": cached, "value_cap": DENODO_VALUE_CAP})


@router.post("/search/record")
def full_record(
        request: Request,
        database: str = Body(...),
        link: str = Body(...),
        auth: dict = Depends(require_permission("viewData")),
):
    """The whole row behind a search hit, uncut, read live from Denodo."""
    _check_database(database)
    if len(link) > 2000 or not link.startswith(f"{database}/"):
        raise HTTPException(status_code=400, detail="That record link is not valid")
    from urllib.parse import urlencode
    with _view_calls:
        data = _denodo_post_get("/public/api/indexes/live-data?" + urlencode(
            {"databaseName": database, "self": link, "serverId": _server_of(database)}))
    values = data.get("values") if isinstance(data, dict) else None
    row = {}
    for v in values or []:
        if isinstance(v, dict) and v.get("column") is not None:
            row[str(v["column"])] = _cell(v.get("value"))
    return _json(request, {"record": row})


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------

_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_BAD_SHEET_CHARS = re.compile(r"[\\/*?:\[\]]")
_EXCEL_MAX_CELL = 32767
_EXCEL_MAX_ROWS = 1048575


def _xl(value) -> str:
    s = _ILLEGAL_XML.sub("", _cell(value))
    return s if len(s) <= _EXCEL_MAX_CELL else s[:_EXCEL_MAX_CELL - 15] + " ...[truncated]"


def _sheet_name(view: str, used: set) -> str:
    base = _BAD_SHEET_CHARS.sub("_", view).strip("'") or "Sheet"
    name = base[:31]
    n = 2
    while name.lower() in used:
        suffix = f"~{n}"
        name = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(name.lower())
    return name


def _names(items) -> str:
    return "; ".join(str(i.get("name", i) if isinstance(i, dict) else i) for i in (items or []))


def build_workbook(results: Dict[str, dict], metadata: Optional[Dict[str, dict]], term: str) -> bytes:
    out = BytesIO()
    wb = xlsxwriter.Workbook(out, {"in_memory": True, "strings_to_numbers": False,
                                   "strings_to_formulas": False, "strings_to_urls": False})
    head = wb.add_format({"bold": True, "bg_color": "#F1EFED", "border": 1})
    link = wb.add_format({"font_color": "#0F68B2", "underline": 1})
    used = {"summary", "metadata"}

    summary = wb.add_worksheet("Summary")
    summary.write_string(0, 0, "Search term", head)
    summary.write_string(0, 1, _xl(term))
    summary.write_string(1, 0, "Exported", head)
    summary.write_string(1, 1, datetime.now().strftime("%Y-%m-%d %H:%M"))
    summary.write_string(2, 0, "Note", head)
    summary.write_string(2, 1, "Total hits is the real number of matches. Rows in sheet is what was exported "
                               "(10 per view, or up to 5,000 with all rows). Denodo cuts long text at 300 characters.")
    summary.write_row(3, 0, ["View", "Database", "Data source", "Total hits", "Rows in sheet", "Sheet"], head)
    summary.set_column(0, 0, 55)
    summary.set_column(1, 1, 28)
    summary.set_column(2, 5, 14)

    row_s = 4
    for view, data in results.items():
        records = data.get("records") if isinstance(data, dict) else data
        if not records:
            continue
        sheet = _sheet_name(view, used)
        ws = wb.add_worksheet(sheet)

        columns: List[str] = []
        seen = set()
        for rec in records:
            for k in rec.keys():
                if k not in seen:
                    seen.add(k)
                    columns.append(k)

        for c, col in enumerate(columns):
            ws.write_string(0, c, _xl(col), head)
        widths = [min(max(len(col), 8), 60) for col in columns]
        rows = records[:_EXCEL_MAX_ROWS]
        for r, rec in enumerate(rows, start=1):
            for c, col in enumerate(columns):
                v = _xl(rec.get(col))
                if v:
                    ws.write_string(r, c, v)
                    if r <= 200 and len(v) > widths[c]:
                        widths[c] = min(len(v), 60)
        for c, w in enumerate(widths):
            ws.set_column(c, c, w + 2)
        ws.freeze_panes(1, 0)
        if columns:
            ws.autofilter(0, 0, len(rows), len(columns) - 1)

        info = data if isinstance(data, dict) else {}
        summary.write_url(row_s, 0, f"internal:'{sheet}'!A1", link, _xl(info.get("view") or view))
        summary.write_string(row_s, 1, _xl(info.get("database", "")))
        summary.write_string(row_s, 2, info.get("datasource") or datasource_of(view))
        summary.write_number(row_s, 3, int(info.get("record_count") or len(records)))
        summary.write_number(row_s, 4, len(records))
        summary.write_string(row_s, 5, sheet)
        row_s += 1

    if row_s == 4:
        wb.close()
        raise HTTPException(status_code=400, detail="No data to export")

    if metadata:
        ws = wb.add_worksheet("Metadata")
        cols = ["View", "Database", "Description", "Categories", "Tags", "Score", "Matched fields",
                "Endorsements", "Warnings", "Deprecations", "Catalog ID"]
        ws.write_row(0, 0, cols, head)
        r = 1
        for view in results:
            m = metadata.get(view)
            if not m:
                ws.write_string(r, 0, _xl(view))
                ws.write_string(r, 2, "Not registered in the catalog")
                r += 1
                continue
            vals = [view, m.get("database_name"), m.get("description"), _names(m.get("categories")),
                    _names(m.get("tags")), m.get("score"), m.get("matched_fields"),
                    m.get("countEndorsements"), m.get("countWarnings"), m.get("countDeprecations"), m.get("id")]
            for c, v in enumerate(vals):
                s = _xl(v)
                if s:
                    ws.write_string(r, c, s)
            r += 1
        ws.set_column(0, 0, 50)
        ws.set_column(1, 1, 24)
        ws.set_column(2, 2, 70)
        ws.set_column(3, 10, 16)
        ws.freeze_panes(1, 0)

    wb.close()
    return out.getvalue()


EXPORT_ROWS_PER_VIEW = 5000


def _fill_all_rows(results: Dict[str, dict], term: str, threshold: int, fuzzy: bool) -> Dict[str, dict]:
    """For the 'all rows' export: replace each view's first 10 rows with up to
    EXPORT_ROWS_PER_VIEW, read page by page. A view that fails keeps its 10."""
    out = {}
    for key, entry in results.items():
        if not entry.get("truncated") or not entry.get("database"):
            out[key] = entry
            continue
        rows, total = [], entry.get("record_count") or 0
        columns = list(entry["records"][0].keys()) if entry.get("records") else None
        try:
            while len(rows) < min(total, EXPORT_ROWS_PER_VIEW):
                page = view_rows(entry["database"], entry["view"], term, threshold, fuzzy, len(rows), VIEW_PAGE_MAX,
                                 columns)
                if not page["records"]:
                    break
                rows.extend(page["records"])
        except HTTPException as e:
            logger.warning(f"all-rows export: {entry.get('view')} kept 10 rows ({e.detail})")
            out[key] = entry
            continue
        out[key] = {**entry, "records": rows[:EXPORT_ROWS_PER_VIEW] or entry["records"]}
    return out


@router.post("/download")
def download_excel(
        request: Request,
        search_text: Optional[str] = Body(default=None),
        datasources: Optional[List[str]] = Body(default=None),
        views: Optional[List[str]] = Body(default=None),
        include_metadata: bool = Body(default=False),
        all_rows: bool = Body(default=False),
        database_ids: Optional[List[int]] = Body(default=None),
        results: Optional[dict] = Body(default=None),
        metadata: Optional[dict] = Body(default=None),
        auth: dict = Depends(require_permission("viewData")),
):
    """Preferred body: {search_text, datasources?, views?, include_metadata}.
    The rows come from the server's search cache, so the browser does not
    upload the whole result set again. The old body {results, metadata} still
    works."""
    if include_metadata and not _has(auth, "viewMetadata"):
        raise HTTPException(status_code=403, detail="Insufficient permissions for metadata")

    term = ""
    if search_text and search_text.strip():
        term, threshold, fuzzy, results, _ = _run_search(search_text, datasources, database_ids=database_ids)
        if views:
            wanted = [v for v in dict.fromkeys(views) if v in results]
            results = {v: results[v] for v in wanted}
        if all_rows and results:
            results = _fill_all_rows(results, term, threshold, fuzzy)
    if not results:
        raise HTTPException(status_code=400, detail="No data to export")

    meta_by_view = None
    if include_metadata:
        names = list(results.keys())

        def one(name):
            try:
                entry = results.get(name) or {}
                return name, metadata_for_view(entry.get("view") or name, entry.get("database"))
            except HTTPException:
                return name, None
        with ThreadPoolExecutor(max_workers=min(3, len(names))) as pool:
            meta_by_view = dict(pool.map(one, names))
    elif metadata and _has(auth, "viewMetadata"):
        meta_by_view = metadata

    content = build_workbook(results, meta_by_view, term)
    stamp = datetime.now().strftime("%Y-%m-%d")

    if "spreadsheetml" in request.headers.get("accept", ""):
        return Response(
            content=content,
            media_type=XLSX_MIME,
            headers={"Content-Disposition": f'attachment; filename="fuzzy_search_{stamp}.xlsx"',
                     "Cache-Control": "no-store"},
        )
    # older page build expects base64 inside JSON
    return {"file": base64.b64encode(content).decode(), "sheets": len(results)}