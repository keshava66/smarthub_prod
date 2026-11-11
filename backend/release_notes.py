import ast
import json
import logging
import re
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

import db
from audit import write_audit_event
from auth import require_auth

logger = logging.getLogger(__name__)

EASTERN = pytz.timezone("America/New_York")

BACKEND = Path(__file__).parent
REPO = BACKEND.parent

release_notes_router = APIRouter(prefix="/api/release-notes", tags=["What's New"])

KINDS = ("feature", "improvement", "fix", "removal", "commit")
STATUSES = ("draft", "ready", "announced", "hidden")

# Routes that exist for plumbing rather than for users. A new /health is not
# news, and a row for it is one more thing to bin every time.
BORING = re.compile(
    r"^/(health|healthz|ping|metrics|docs|openapi|redoc)\b|/(config|ping|health)$", re.I)


# ============================================================================
# DATABASE
# ============================================================================

# Tables: smarthub_core.release_notes and release_note_snapshots.
_db = db.transaction

# Held for the whole of a scan's write transaction. Every worker scans at
# startup; without this two of them diff against the same snapshot and file
# the same change twice.
_SCAN_LOCK = "release-notes-scan"


def _now() -> str:
    return datetime.now(EASTERN).isoformat()


def _row(r) -> Dict[str, Any]:
    return dict(r)


# ============================================================================
# HARVEST — the same reading of the code build_kb.py does
# ============================================================================

def _router_prefixes(tree: ast.AST) -> Dict[str, str]:
    prefixes = {"app": ""}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not (isinstance(call.func, ast.Name) and call.func.id == "APIRouter"):
            continue
        prefix = ""
        for kw in call.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = kw.value.value
        for target in node.targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix
    return prefixes


def harvest_routes() -> Dict[str, Dict[str, str]]:
    """{"GET /api/foo": {"doc": ..., "file": ...}} for the whole backend."""
    found: Dict[str, Dict[str, str]] = {}
    for py_file in sorted(BACKEND.glob("*.py")):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"))
        except (SyntaxError, OSError):
            continue
        prefixes = _router_prefixes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                verb = dec.func.attr.lower()
                if verb not in ("get", "post", "put", "delete", "patch"):
                    continue
                obj = dec.func.value
                if not (isinstance(obj, ast.Name) and obj.id in prefixes):
                    continue
                if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                    continue
                path = prefixes[obj.id] + dec.args[0].value
                if BORING.search(path):
                    continue
                found[f"{verb.upper()} {path}"] = {
                    "doc": (ast.get_docstring(node) or "").strip(),
                    "file": py_file.name,
                    # The handler's own name. Moving a route changes its path
                    # but almost never its function, which is what lets the
                    # diff below call that a rename rather than a delete and
                    # an unrelated new endpoint.
                    "func": node.name,
                }
                break
    return found


def harvest_pages() -> Dict[str, Dict[str, str]]:
    """{"/dqe": {"component": "DQEngine"}} out of App.jsx."""
    app_jsx = REPO / "frontend" / "src" / "App.jsx"
    try:
        src = app_jsx.read_text(encoding="utf-8")
    except OSError:
        return {}
    wrappers = {"PermissionRoute", "ProtectedRoute", "Suspense", "Routes", "Route",
                "Navigate", "Fragment", "Layout"}
    found: Dict[str, Dict[str, str]] = {}
    for m in re.finditer(r'path="(/[^"]*)"\s*\n?\s*element=\{(.*?)\}\s*\n?\s*/>', src, re.DOTALL):
        path, block = m.group(1), m.group(2)
        if path in ("/", "/*") or path.startswith("/login") or path.startswith("/logout"):
            continue
        comps = re.findall(r'<(\w+)', block)
        real = next((c for c in reversed(comps) if c not in wrappers), None)
        if real:
            found[path] = {"component": real}
    return found


def _git_subjects(since_iso: Optional[str], limit: int = 40) -> List[Dict[str, str]]:
    """Commit subjects since the last scan. Empty list if this isn't a checkout."""
    if not (REPO / ".git").exists():
        return []
    cmd = ["git", "-C", str(REPO), "log", f"-{limit}", "--no-merges",
           "--pretty=format:%H\x1f%s\x1f%aI"]
    if since_iso:
        cmd.insert(4, f"--since={since_iso}")
    try:
        raw = subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:
        logger.info(f"git log unavailable for release notes: {exc}")
        return []
    out = []
    for line in raw.strip().splitlines():
        parts = line.split("\x1f")
        if len(parts) == 3 and parts[1].strip():
            out.append({"sha": parts[0][:10], "subject": parts[1].strip(), "at": parts[2]})
    return out


# ============================================================================
# SNAPSHOT + DIFF
# ============================================================================

def _read_snapshot(conn, key: str) -> Optional[Dict[str, Any]]:
    row = conn.execute("SELECT payload, taken_at FROM release_note_snapshots WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        return {"payload": json.loads(row["payload"]), "taken_at": row["taken_at"]}
    except Exception:
        return None


def _write_snapshot(conn, key: str, payload: Any) -> None:
    conn.execute(
        "INSERT INTO release_note_snapshots (key, payload, taken_at) VALUES (?, ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET payload = EXCLUDED.payload, taken_at = EXCLUDED.taken_at",
        (key, json.dumps(payload), _now()))


def _insert(conn, *, title: str, summary: str, detail: str,
            area: str, kind: str, source: str, fingerprint: str, by: str,
            subject: str = "") -> bool:
    """Returns False when this exact thing has already been recorded.

    ON CONFLICT rather than catching the error: in PostgreSQL a failed
    statement aborts the whole transaction, and the rest of the scan with it.
    """
    row = conn.execute(
        "INSERT INTO release_notes (id, title, summary, detail, area, kind, status, "
        "source, fingerprint, subject, created_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?) "
        "ON CONFLICT (fingerprint) DO NOTHING RETURNING id",
        (str(uuid.uuid4()), title[:200], summary[:1000], detail[:4000], area[:200],
         kind, source, fingerprint, subject[:200], _now(), by)).fetchone()
    return row is not None


def _page_ids(path: str, meta: Dict[str, str]) -> List[str]:
    """Every identity this page may be filed under, the durable one first.

    The component survives a move and the path does not, so the component is
    what a rename is tracked by. The path form is still answered to, because
    rows filed before components were recorded carry it - matching on both is
    what stops an upgrade from stranding everything already on the list.
    """
    ids = [f"page:{path}"]
    component = meta.get("component")
    if component:
        ids.insert(0, f"page:{component}")
    return ids


def _route_ids(key: str, meta: Dict[str, str]) -> List[str]:
    """Same, for an endpoint: its handler function first, then its path."""
    ids = [f"route:{key}"]
    if meta.get("file") and meta.get("func"):
        ids.insert(0, f"route:{meta['file']}:{meta['func']}")
    return ids


def _pending_rows(conn, ids: List[str]) -> List[Dict[str, Any]]:
    """Rows about this thing that nobody has been told about yet."""
    if not ids:
        return []
    return conn.execute(
        "SELECT * FROM release_notes WHERE subject = ANY(?) "
        "AND status IN ('draft','ready') ORDER BY created_at DESC, id", (list(ids),)).fetchall()


def _was_announced(conn, ids: List[str]) -> bool:
    if not ids:
        return False
    return conn.execute(
        "SELECT 1 FROM release_notes WHERE subject = ANY(?) "
        "AND status = 'announced' LIMIT 1", (list(ids),)).fetchone() is not None


def _drop_pending(conn, ids: List[str]) -> int:
    if not ids:
        return 0
    cur = conn.execute(
        "DELETE FROM release_notes WHERE subject = ANY(?) "
        "AND status IN ('draft','ready')", (list(ids),))
    return cur.rowcount


def _pretty_page(path: str, component: str) -> str:
    # "/gra-data-domains" -> "GRA Data Domains" is wrong more often than not, so
    # prefer the component name, which someone actually chose.
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", component or "").strip()
    return words or path.strip("/").replace("-", " ").title()


def _pair_renames(added: Dict[str, Dict[str, str]],
                  removed: Dict[str, Dict[str, str]],
                  identity) -> Tuple[List[Tuple[str, str, Dict[str, str]]],
                                     Dict[str, Dict[str, str]],
                                     Dict[str, Dict[str, str]]]:
    """Match what appeared against what vanished, and call the matches renames.

    Moving a page from /gra-data-domains to /structured-explorer is one event,
    not "a page was deleted" plus "an unrelated page appeared". `identity`
    returns the thing that survives a move - the component for a page, the
    handler function for a route - and anything that shows up on both sides
    under the same identity is the same thing in a new place.

    Returns (renames, still-added, still-removed).
    """
    def usable(ident) -> bool:
        # A tuple of Nones is truthy, so an entry from a snapshot written
        # before handler names were recorded would otherwise file every one
        # of its routes under the same (None, None) key and pair them up at
        # random. No identity means no rename pairing, which is the safe way
        # to be wrong: it comes out as a removal and an addition instead.
        if isinstance(ident, tuple):
            return all(part for part in ident)
        return bool(ident)

    by_identity: Dict[Any, str] = {}
    for key, meta in removed.items():
        ident = identity(meta)
        if usable(ident):
            by_identity.setdefault(ident, key)

    renames: List[Tuple[str, str, Dict[str, str]]] = []
    for key, meta in list(added.items()):
        ident = identity(meta)
        old_key = by_identity.pop(ident, None) if usable(ident) else None
        if old_key and old_key in removed:
            renames.append((old_key, key, meta))
            added.pop(key)
            removed.pop(old_key)
    return renames, added, removed


def _sweep_stale(conn, routes: Dict[str, Dict[str, str]],
                 pages: Dict[str, Dict[str, str]]) -> List[str]:
    """Bin pending rows for things that are not in the portal any more.

    The diff below only notices a removal if it catches the transition, and a
    transition can be missed: a page added and deleted while a version that
    did not look for removals was running left a row behind AND a snapshot
    with no trace of it, so the thing is now in neither side of the diff and
    no later scan has anything to pair up. That row would sit in Waiting
    forever with no way out.

    So do not rely only on transitions. Reconcile: anything the scanner filed,
    that nobody has been told about, and that no longer exists, is not news.

    Deliberately narrow. Only rows the scanner itself detected (source
    "routes"), never a note somebody typed in or a commit subject. Never an
    announced row - users heard about that, and it stays on the record.
    Never a removal row either: its whole subject is a thing that is gone,
    which is exactly what this would otherwise sweep.
    """
    live = set()
    for path, meta in pages.items():
        live.update(_page_ids(path, meta))
    for key, meta in routes.items():
        live.update(_route_ids(key, meta))

    rows = conn.execute(
        "SELECT id, subject, title FROM release_notes WHERE source = 'routes' "
        "AND status IN ('draft','ready') AND kind <> 'removal' AND subject <> ''"
    ).fetchall()

    stale = [r for r in rows if r["subject"] not in live]
    if stale:
        conn.execute("DELETE FROM release_notes WHERE id = ANY(?)", ([r["id"] for r in stale],))
    return [r["title"] for r in stale]


def _snapshot_meta(previous: Optional[Dict[str, Any]], section: str) -> Dict[str, Dict[str, str]]:
    """The last harvest as {key: meta}.

    Snapshots written before removals were tracked held a bare list of keys.
    Read those as keys with no metadata: the diff still works, rename pairing
    just sits out one cycle until the richer snapshot is written below.
    """
    stored = (previous or {}).get("payload", {}).get(section) or {}
    if isinstance(stored, list):
        return {key: {} for key in stored}
    return {key: (meta or {}) for key, meta in stored.items()}


def scan(performed_by: str = "system", include_git: bool = True) -> Dict[str, Any]:
    """Diff the portal against the last snapshot and record what changed.

    Additions, removals and renames all land as draft rows. Removals matter as
    much as additions - "the old export button is gone" is exactly the sort of
    thing users find out the hard way - but they are noisier, because a
    refactor that consolidates three endpoints into one looks like three
    deletions to a diff. So they come in as drafts like everything else, and
    binning the ones nobody outside the team would notice is part of the job.

    The very first run is the odd one out: everything is "new" on a portal that
    has never been scanned, and 200 draft rows saying the Audit Log exists is
    not what anyone wants. So the first run only takes the snapshot.
    """
    routes, pages = harvest_routes(), harvest_pages()
    added_routes: List[str] = []
    added_pages: List[str] = []
    gone: List[str] = []
    moved: List[str] = []
    # Pending rows that quietly followed a rename, and pending rows binned
    # because the thing was gone before anyone heard of it. Neither is news.
    relocated: List[str] = []
    cancelled: List[str] = []
    added_commits = 0

    # git runs before the write transaction opens, so a slow checkout never
    # holds the scan lock or a transaction open. Should another worker scan in
    # between, its snapshot is newer and these commits are a superset of what
    # it saw; the fingerprint stops any being filed twice.
    commits: List[Dict[str, str]] = []
    if include_git:
        with _db(read_only=True) as conn:
            earlier = _read_snapshot(conn, "portal")
        if earlier is not None:
            commits = _git_subjects(earlier.get("taken_at"))

    with _db() as conn:
        db.advisory_xact_lock(conn, _SCAN_LOCK, timeout_ms=60000)
        previous = _read_snapshot(conn, "portal")
        first_run = previous is None

        if not first_run:
            old_pages = _snapshot_meta(previous, "pages")
            old_routes = _snapshot_meta(previous, "routes")

            new_pages = {k: v for k, v in pages.items() if k not in old_pages}
            lost_pages = {k: v for k, v in old_pages.items() if k not in pages}
            new_routes = {k: v for k, v in routes.items() if k not in old_routes}
            lost_routes = {k: v for k, v in old_routes.items() if k not in routes}

            page_renames, new_pages, lost_pages = _pair_renames(
                new_pages, lost_pages, lambda m: m.get("component"))
            route_renames, new_routes, lost_routes = _pair_renames(
                new_routes, lost_routes, lambda m: (m.get("file"), m.get("func")))

            # ── moved ──────────────────────────────────────────────────────
            for old_key, new_key, meta, ids in (
                    [(a, b, m, _page_ids(b, m)) for a, b, m in page_renames]
                    + [(a, b, m, _route_ids(b, m)) for a, b, m in route_renames]):
                pending = _pending_rows(conn, ids)
                if pending:
                    # Nobody has been told this thing exists, so it has not
                    # "moved" as far as any reader is concerned - it is still
                    # just the thing that is coming, at a different address.
                    # Follow it rather than filing a second row about it, and
                    # pin it to the durable identity on the way past so the
                    # next move finds it by that rather than by where it sits.
                    conn.execute(
                        "UPDATE release_notes SET area = ?, fingerprint = ?, subject = ? "
                        "WHERE id = ?",
                        (new_key.split(" ", 1)[-1][:200],
                         f"{pending[0]['fingerprint']}|{new_key}", ids[0], pending[0]["id"]))
                    relocated.append(f"{old_key} -> {new_key}")
                    continue
                if _insert(conn,
                           title=f"{_pretty_page(new_key, meta.get('component', ''))} moved"
                                 if meta.get("component") else f"{new_key} moved",
                           summary="",
                           detail=f"Was {old_key}, now {new_key}.",
                           area=new_key.split(" ", 1)[-1], kind="improvement", source="routes",
                           fingerprint=f"moved:{old_key}->{new_key}", by=performed_by,
                           subject=ids[0]):
                    moved.append(f"{old_key} -> {new_key}")

            # ── added ──────────────────────────────────────────────────────
            for path, meta in new_pages.items():
                name = _pretty_page(path, meta.get("component", ""))
                if _insert(conn,
                           title=name,
                           summary="",
                           detail=f"New page at {path}, rendered by {meta.get('component')}.",
                           area=path, kind="feature", source="routes",
                           fingerprint=f"page:{path}", by=performed_by,
                           subject=_page_ids(path, meta)[0]):
                    added_pages.append(path)

            for key, meta in new_routes.items():
                # A new endpoint under a page that is itself new is the same
                # news twice. The page row covers it.
                if any(key.split(" ", 1)[1].startswith(p.rstrip("/")) for p in added_pages):
                    continue
                if _insert(conn,
                           title=key,
                           summary="",
                           detail=meta.get("doc") or "",
                           area=key.split(" ", 1)[1], kind="improvement", source="routes",
                           fingerprint=f"route:{key}", by=performed_by,
                           subject=_route_ids(key, meta)[0]):
                    added_routes.append(key)

            # ── removed ────────────────────────────────────────────────────
            # Something added and then taken away again before anyone was told
            # is not news in either direction. Drop the rows still waiting and
            # file a removal only if users were actually told it existed.
            for path, meta in lost_pages.items():
                ids = _page_ids(path, meta)
                dropped = _drop_pending(conn, ids)
                if not _was_announced(conn, ids):
                    if dropped:
                        cancelled.append(path)
                    continue
                if _insert(conn,
                           title=f"{_pretty_page(path, meta.get('component', ''))} removed",
                           summary="",
                           detail=f"The page at {path} is gone.",
                           area=path, kind="removal", source="routes",
                           fingerprint=f"page-gone:{path}", by=performed_by,
                           subject=ids[0]):
                    gone.append(path)

            for key, meta in lost_routes.items():
                # Same reasoning as above, in reverse: endpoints under a page
                # that has itself been removed are covered by the page row.
                if any(key.split(" ", 1)[1].startswith(p.rstrip("/")) for p in gone):
                    continue
                ids = _route_ids(key, meta)
                dropped = _drop_pending(conn, ids)
                if not _was_announced(conn, ids):
                    if dropped:
                        cancelled.append(key)
                    continue
                if _insert(conn,
                           title=f"{key} removed",
                           summary="",
                           detail=f"The endpoint {key} no longer exists.",
                           area=key.split(" ", 1)[1], kind="removal", source="routes",
                           fingerprint=f"route-gone:{key}", by=performed_by,
                           subject=ids[0]):
                    gone.append(key)

            if include_git:
                for commit in commits:
                    if _insert(conn,
                               title=commit["subject"],
                               summary="",
                               detail=f"Commit {commit['sha']} on {commit['at'][:10]}.",
                               area="", kind="commit", source="git",
                               fingerprint=f"git:{commit['sha']}", by=performed_by):
                        added_commits += 1

            # Catches anything the transition-based diff above could not,
            # including rows stranded by an older version of this file.
            cancelled.extend(_sweep_stale(conn, routes, pages))

        # Keys with their metadata now, so the next diff can pair renames.
        _write_snapshot(conn, "portal", {"routes": routes, "pages": pages})

    result = {
        "first_run": first_run,
        "new_pages": added_pages,
        "new_routes": added_routes,
        "removed": gone,
        "moved": moved,
        "relocated": relocated,
        "cancelled": cancelled,
        "new_commits": added_commits,
        "total_new": (len(added_pages) + len(added_routes) + len(gone)
                      + len(moved) + added_commits),
        "routes_seen": len(routes),
        "pages_seen": len(pages),
    }
    # No log line here on purpose: every caller says something better about the
    # result than a bare count would - scan_quietly() writes the startup
    # summary, and /scan answers the admin who pressed the button.
    return result


def scan_quietly() -> None:
    """Startup hook. A failure here must never stop the backend coming up.

    Says what it found either way. A scan that finds nothing and a scan that
    never ran look identical in a quiet log, and the difference is the whole
    feature.
    """
    try:
        result = scan(performed_by="startup")
    except Exception as exc:
        logger.warning(f"What's New scan failed: {exc}")
        return

    if result["first_run"]:
        logger.info(
            "What's New: first run — snapshot taken of %d routes and %d pages. "
            "Anything added from here on gets picked up.",
            result["routes_seen"], result["pages_seen"])
        return

    with _db(read_only=True) as conn:
        waiting = conn.execute(
            "SELECT COUNT(*) AS n FROM release_notes WHERE status IN ('draft','ready') "
            "AND kind <> 'commit'").fetchone()["n"]

    quiet = len(result["relocated"]) + len(result["cancelled"])
    tail = (f", {quiet} pending item(s) followed or dropped because nobody had "
            f"been told about them") if quiet else ""

    if result["total_new"]:
        logger.info(
            "What's New: %d change(s) — %d page(s), %d endpoint(s), %d removed, "
            "%d moved, %d commit(s)%s. %d waiting to be announced.",
            result["total_new"], len(result["new_pages"]), len(result["new_routes"]),
            len(result["removed"]), len(result["moved"]), result["new_commits"],
            tail, waiting)
    else:
        logger.info("What's New: nothing new since the last scan%s, %d waiting to be "
                    "announced", tail, waiting)


# ============================================================================
# READS USED BY THE DRAFTER
# ============================================================================

def unannounced(limit: int = 25, include_commits: bool = False) -> List[Dict[str, Any]]:
    """Everything shipped but not yet told to anyone, newest first."""
    kinds = KINDS if include_commits else tuple(k for k in KINDS if k != "commit")
    with _db(read_only=True) as conn:
        rows = conn.execute(
            "SELECT * FROM release_notes WHERE status IN ('draft','ready') "
            "AND kind = ANY(?) ORDER BY created_at DESC, id LIMIT ?",
            (list(kinds), max(1, min(limit, 100)))).fetchall()
    return [_row(r) for r in rows]


def by_ids(ids: List[str]) -> List[Dict[str, Any]]:
    if not ids:
        return []
    with _db(read_only=True) as conn:
        rows = conn.execute(
            "SELECT * FROM release_notes WHERE id = ANY(?) ORDER BY created_at DESC, id",
            (list(ids),)).fetchall()
    return [_row(r) for r in rows]


def mark_announced(ids: List[str], communication_id: str) -> int:
    """Called after a send actually goes out — not when a draft is written."""
    if not ids:
        return 0
    with _db() as conn:
        cur = conn.execute(
            "UPDATE release_notes SET status = 'announced', announced_at = ?, "
            "announced_in = ? WHERE id = ANY(?) AND status <> 'announced'",
            (_now(), communication_id, list(ids)))
        return cur.rowcount


def digest(limit: int = 25, include_commits: bool = False) -> str:
    """The unannounced list as plain text for a prompt or a tool result."""
    rows = unannounced(limit, include_commits)
    if not rows:
        return "Nothing is waiting to be announced."
    lines = []
    for r in rows:
        head = r["title"]
        if r["area"]:
            head += f"  ({r['area']})"
        lines.append(f"- [{r['id']}] {head}")
        text = (r["summary"] or r["detail"] or "").strip()
        if text:
            lines.append(f"    {text[:400]}")
    return "\n".join(lines)


# ============================================================================
# ROUTES
# ============================================================================

class NoteRequest(BaseModel):
    title: str
    summary: str = ""
    detail: str = ""
    area: str = ""
    kind: str = "feature"
    status: str = "ready"


@release_notes_router.get("")
def list_notes(
    status: str = Query("open", description="open | announced | all | draft | ready | hidden"),
    include_commits: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    auth: dict = Depends(require_auth),
):
    """What has shipped, and whether users have been told about it yet."""
    where, params = [], []
    if status == "open":
        where.append("status IN ('draft','ready')")
    elif status in STATUSES:
        where.append("status = ?")
        params.append(status)
    if not include_commits:
        where.append("kind <> 'commit'")
    clause = f"WHERE {' AND '.join(where)}" if where else ""

    # Counted through the same commit filter the list uses. Counting all rows
    # made the Waiting badge say 2 over a list showing one item, because a raw
    # commit row is hidden from the list but was still counted - a number
    # nobody could reconcile against what they saw. Tallies, last scan time
    # and the page come back in one statement (one round trip); the snapshot's
    # payload, which can be large, isn't read at all.
    counted = "" if include_commits else "WHERE kind <> 'commit'"
    with _db(read_only=True) as conn:
        rows = conn.execute(
            f"""SELECT c.tallies, s.taken_at AS last_scan, p.*
                  FROM (SELECT json_object_agg(status, n) AS tallies
                          FROM (SELECT status, COUNT(*) AS n FROM release_notes {counted}
                                 GROUP BY status) AS x) AS c
                  LEFT JOIN release_note_snapshots s ON s.key = 'portal'
                  LEFT JOIN LATERAL (
                        SELECT * FROM release_notes {clause}
                         ORDER BY created_at DESC, id LIMIT ?
                       ) AS p ON true""",
            (*params, limit)).fetchall()

    head = rows[0]
    counts = json.loads(head["tallies"] or "{}")
    notes = [{k: v for k, v in r.items() if k not in ("tallies", "last_scan")}
             for r in rows if r["id"] is not None]
    return {
        "notes": notes,
        "counts": counts,
        "last_scan": head["last_scan"],
    }


@release_notes_router.post("/scan")
def rescan(include_git: bool = Query(True), auth: dict = Depends(require_auth)):
    """Re-read the portal and record anything that wasn't there last time."""
    email = auth["user"]["email"]
    result = scan(performed_by=email, include_git=include_git)
    if result["total_new"]:
        write_audit_event(
            action="release_notes_scanned",
            performed_by=email,
            target=f"{result['total_new']} change(s)",
            details=", ".join(result["new_pages"] + result["new_routes"]
                              + result["removed"] + result["moved"])[:500],
        )
    return result


@release_notes_router.post("")
def create_note(req: NoteRequest, auth: dict = Depends(require_auth)):
    """Record something that shipped. For anything a route diff can't see."""
    if not req.title.strip():
        raise HTTPException(status_code=400, detail="Title is required")
    kind = req.kind if req.kind in KINDS else "feature"
    status = req.status if req.status in STATUSES else "ready"
    note_id = str(uuid.uuid4())
    with _db() as conn:
        conn.execute(
            "INSERT INTO release_notes (id, title, summary, detail, area, kind, status, "
            "source, fingerprint, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'manual', ?, ?, ?)",
            (note_id, req.title.strip()[:200], req.summary[:1000], req.detail[:4000],
             req.area[:200], kind, status, f"manual:{note_id}", _now(),
             auth["user"]["email"]))
    return {"success": True, "id": note_id}


@release_notes_router.put("/{note_id}")
def update_note(note_id: str, req: NoteRequest, auth: dict = Depends(require_auth)):
    """Edit a note — usually to write the summary on something detected."""
    kind = req.kind if req.kind in KINDS else "feature"
    status = req.status if req.status in STATUSES else "ready"
    with _db() as conn:
        cur = conn.execute(
            "UPDATE release_notes SET title = ?, summary = ?, detail = ?, area = ?, "
            "kind = ?, status = ? WHERE id = ?",
            (req.title.strip()[:200], req.summary[:1000], req.detail[:4000],
             req.area[:200], kind, status, note_id))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="No such note")
    return {"success": True}


@release_notes_router.delete("/{note_id}")
def delete_note(note_id: str, auth: dict = Depends(require_auth)):
    """Bin a note.

    The snapshot still holds the route it came from, so a rescan will not put
    it back — set status "hidden" instead if you want it kept out of the way
    but still on the record.
    """
    with _db() as conn:
        cur = conn.execute("DELETE FROM release_notes WHERE id = ?", (note_id,))
        if not cur.rowcount:
            raise HTTPException(status_code=404, detail="No such note")
    return {"success": True}


_scanned_this_process = False


async def _startup_scan() -> None:
    """Scan once when the portal comes up.

    Guarded, because include_router() copies a router's startup handlers onto
    the app while the router keeps its own, so this fires twice per boot. The
    second scan is harmless - it diffs against the snapshot the first one just
    wrote and finds nothing - but it parses every backend file and shells out
    to git to prove it, and it says so twice in the log.
    """
    global _scanned_this_process
    if _scanned_this_process:
        return
    _scanned_this_process = True
    # Off the event loop: the harvest is all file parsing and a subprocess.
    import asyncio
    await asyncio.to_thread(scan_quietly)


release_notes_router.add_event_handler("startup", _startup_scan)