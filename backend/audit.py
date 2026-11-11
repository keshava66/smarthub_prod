import atexit
import contextvars
import csv
import io
import json
import os
import queue
import re
import uuid
import logging
import threading
import time
import pytz
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, List, Dict

from fastapi import APIRouter, Query, Header, HTTPException, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

try:
    import fcntl
except ImportError:          # Windows: the spool still works, just unlocked
    fcntl = None

import db
from auth import require_auth, require_permission

logger = logging.getLogger(__name__)

EASTERN = pytz.timezone('America/New_York')

audit_router = APIRouter(prefix="/api/audit", tags=["Audit"])


# ============================================================================
# DATABASE — tables live in smarthub_core (see migrations/smarthub_core)
# ============================================================================

_db = db.transaction


# The dashboard cards, the Analytics tab, the actions filter and the people
# list all aggregate over the whole log, which only grows (every page view is
# a row: roughly two million a year at 500 users, where those queries take
# around a second). So they are served from memory:
#   - fresh for AGGREGATE_TTL_SECONDS;
#   - after that, still served at once while one background refresh runs
#     (stale-while-revalidate), for up to AGGREGATE_MAX_STALE_SECONDS;
#   - computed in the request only when there's nothing usable cached.
# A delete drops the lot, so the admin who made it sees the change at once.
AGGREGATE_TTL_SECONDS = 60
AGGREGATE_MAX_STALE_SECONDS = 300
_aggregates: Dict[tuple, tuple] = {}
_refreshing: set = set()
_inflight: Dict[tuple, Future] = {}
_aggregates_lock = threading.Lock()
_refresher = ThreadPoolExecutor(max_workers=2, thread_name_prefix="audit-aggregates")
_generation = 0


def _drop_aggregates() -> None:
    global _generation
    with _aggregates_lock:
        _aggregates.clear()
        _generation += 1
    with _today_lock:
        _today_cache["value"] = None
    with _suggest_lock:
        _suggest_cache.clear()


def _store(key: tuple, value, generation: int) -> None:
    with _aggregates_lock:
        if generation != _generation:
            return                            # a delete happened meanwhile
        if len(_aggregates) > 256:            # filters are user input; stay bounded
            _aggregates.clear()
        _aggregates[key] = (time.monotonic(), value)


def _refresh(key: tuple, compute, generation: int) -> None:
    try:
        _store(key, compute(), generation)
    except Exception as e:
        logger.warning(f"Audit aggregate refresh failed, serving the last copy: {type(e).__name__}: {e}")
    finally:
        with _aggregates_lock:
            _refreshing.discard(key)


def _cached(key: tuple, compute):
    now = time.monotonic()
    with _aggregates_lock:
        hit = _aggregates.get(key)
        generation = _generation
        age = now - hit[0] if hit else None
        if hit and age >= AGGREGATE_TTL_SECONDS and age < AGGREGATE_MAX_STALE_SECONDS \
                and key not in _refreshing:
            _refreshing.add(key)
            _refresher.submit(_refresh, key, compute, generation)
        if hit and age < AGGREGATE_MAX_STALE_SECONDS:
            return hit[1]
        # Nothing usable: one caller computes, everyone else asking for the
        # same thing waits for that answer instead of starting their own
        # (twenty people opening the page together = one COUNT, not twenty).
        waiter = _inflight.get(key)
        leader = waiter is None
        if leader:
            waiter = _inflight[key] = Future()
    if not leader:
        return waiter.result()
    try:
        value = compute()
        _store(key, value, generation)
        waiter.set_result(value)
        return value
    except BaseException as e:
        waiter.set_exception(e)
        raise
    finally:
        with _aggregates_lock:
            _inflight.pop(key, None)


# ============================================================================
# ACTIVE USERS TRACKING  (in-memory — unchanged from original)
# ============================================================================

_active_users: Dict[str, datetime] = {}
_active_users_lock = threading.Lock()
ACTIVE_TIMEOUT_MINUTES = 5  # consider user inactive after 5 min without heartbeat


def _record_heartbeat(email: str):
    with _active_users_lock:
        _active_users[email] = datetime.utcnow()


def _clear_heartbeat(email: str):
    """Remove a user from the active list (called on logout / session timeout)."""
    with _active_users_lock:
        _active_users.pop(email, None)


def _get_active_users() -> List[Dict]:
    cutoff = datetime.utcnow() - timedelta(minutes=ACTIVE_TIMEOUT_MINUTES)
    with _active_users_lock:
        stale = [e for e, t in _active_users.items() if t < cutoff]
        for e in stale:
            del _active_users[e]
        return [
            {"email": email, "last_seen": ts.isoformat() + "Z"}
            for email, ts in _active_users.items()
        ]


# ============================================================================
# PUBLIC HELPER — called from Backend.py and other modules
# ============================================================================

def count_known_users() -> int:
    """Distinct people who have ever shown up in the audit log (case-insensitive),
    leaving out API keys and events with no user attached."""
    with _db(read_only=True) as conn:
        # GROUP BY rather than COUNT(DISTINCT): it can hash and run in
        # parallel, where COUNT(DISTINCT) sorts every row.
        return conn.execute(
            "SELECT COUNT(*) FROM (SELECT LOWER(performed_by) FROM audit_log "
            "WHERE performed_by <> '' AND performed_by <> 'unknown' "
            "AND LOWER(performed_by) NOT LIKE '%@apikey.local' GROUP BY 1) AS people"
        ).fetchone()[0]


# ── writer ────────────────────────────────────────────────────────────────
# Callers never wait on the database and never see it fail: an event goes on
# a queue and one thread per process writes it, batching under load. If the
# database can't take it, the batch is appended (fsynced) to a spool file and
# replayed once it's back. Ids are fixed up front and inserts are ON CONFLICT
# DO NOTHING, so a replay or a client retry can never double an event.
# Last resort, when even the spool can't be written: the full event goes to
# the error log, so it still exists somewhere.
SPOOL_PATH = Path(os.environ.get("SMARTHUB_AUDIT_SPOOL")
                  or Path(__file__).parent / "audit_spool" / "audit_spool.jsonl")
REPLAY_EVERY_SECONDS = 10
BATCH_MAX = 500
MAX_ACTION, MAX_TARGET, MAX_DETAILS, MAX_IP = 100, 1000, 8000, 100

_queue: "queue.Queue[dict]" = queue.Queue(100_000)
_writer: Optional[threading.Thread] = None
_writer_pid: Optional[int] = None
_writer_lock = threading.Lock()
# Set per request by AuditApiCallMiddleware. A dict, not a flag, so a write
# from the threadpool (which runs on a copy of the context) is still seen.
_request_audit: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar("request_audit", default=None)

_writer_status = {"last_write_at": None, "last_error": None, "last_error_at": None,
                  "written": 0, "spooled": 0}

_INSERT = ("INSERT INTO audit_log (id, timestamp, action, performed_by, target, details, ip_address) "
           "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING")
_FIELDS = ("id", "timestamp", "action", "performed_by", "target", "details", "ip_address")


def _lock(f, blocking=True) -> bool:
    if fcntl is None:
        return True
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return True
    except BlockingIOError:
        return False


def _unlock(f) -> None:
    if fcntl is not None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _is_outage(exc: BaseException) -> bool:
    import psycopg2
    return isinstance(exc, (db.DatabaseUnavailable, psycopg2.OperationalError, psycopg2.InterfaceError))


def _insert(events: List[dict]) -> None:
    """All in one transaction. On a data error (not an outage) fall back to one
    row at a time so a single bad event can't hold back the rest."""
    rows = [tuple(e[k] for k in _FIELDS) for e in events]
    try:
        with _db() as conn:
            conn.executemany(_INSERT, rows)
    except Exception as e:
        if _is_outage(e) or len(rows) == 1:
            raise
        for e_one, row in zip(events, rows):
            try:
                with _db(autocommit=True) as conn:
                    conn.execute(_INSERT, row)
            except Exception as one:
                if _is_outage(one):
                    raise
                logger.error("AUDIT-UNWRITABLE %s (%s)", _to_json(e_one), one)


def _to_json(e: dict) -> str:
    ts = e.get("timestamp")
    return json.dumps({**e, "timestamp": ts.isoformat() if isinstance(ts, datetime) else str(ts)},
                      ensure_ascii=False, default=str)


def _from_json(line: str) -> dict:
    e = json.loads(line)
    e["timestamp"] = datetime.fromisoformat(e["timestamp"])
    return e


def _spool(events: List[dict], reason: BaseException) -> None:
    payload = "".join(_to_json(e) + "\n" for e in events)
    try:
        SPOOL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(SPOOL_PATH, "a", encoding="utf-8") as f:
            _lock(f)
            try:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            finally:
                _unlock(f)
        _writer_status["spooled"] += len(events)
        logger.warning("Audit DB write failed (%s: %s); %d event(s) spooled to %s",
                       type(reason).__name__, reason, len(events), SPOOL_PATH)
    except Exception as e:
        for line in payload.splitlines():
            logger.error("AUDIT-SPOOL-FAILED %s (%s)", line, e)


def _write(events: List[dict]) -> None:
    try:
        _insert(events)
        _writer_status["written"] += len(events)
        _writer_status["last_write_at"] = datetime.now(EASTERN).isoformat()
    except Exception as e:
        _writer_status["last_error"] = f"{type(e).__name__}: {e}"[:300]
        _writer_status["last_error_at"] = datetime.now(EASTERN).isoformat()
        _spool(events, e)


def _replay_spool() -> None:
    """Move spooled events into the database. Whoever holds the file lock does
    it (any worker); the file is only emptied once every row is in."""
    if not SPOOL_PATH.exists() or SPOOL_PATH.stat().st_size == 0:
        return
    with open(SPOOL_PATH, "r+", encoding="utf-8") as f:
        if not _lock(f, blocking=False):
            return
        try:
            events = []
            for line in f.read().splitlines():
                if not line.strip():
                    continue
                try:
                    events.append(_from_json(line))
                except Exception as e:
                    logger.error("AUDIT-SPOOL-UNREADABLE %s (%s)", line, e)
            for i in range(0, len(events), BATCH_MAX):
                _insert(events[i:i + BATCH_MAX])     # raises on outage: file kept
            f.seek(0)
            f.truncate()
            if events:
                _writer_status["written"] += len(events)
                _writer_status["last_write_at"] = datetime.now(EASTERN).isoformat()
                logger.info("Audit spool replayed: %d event(s)", len(events))
        except Exception as e:
            _writer_status["last_error"] = f"replay: {type(e).__name__}: {e}"[:300]
            _writer_status["last_error_at"] = datetime.now(EASTERN).isoformat()
        finally:
            _unlock(f)


def _drain(batch: List[dict]) -> None:
    while len(batch) < BATCH_MAX:
        try:
            batch.append(_queue.get_nowait())
        except queue.Empty:
            return


def _writer_loop() -> None:
    next_replay = 0.0
    while True:
        try:
            batch = []
            try:
                batch.append(_queue.get(timeout=REPLAY_EVERY_SECONDS))
            except queue.Empty:
                pass
            _drain(batch)
            if batch:
                _write(batch)
            if time.monotonic() >= next_replay:
                next_replay = time.monotonic() + REPLAY_EVERY_SECONDS
                _replay_spool()
        except Exception as e:                       # the loop must never die
            logger.exception("Audit writer loop error: %s", e)
            time.sleep(1)


def _ensure_writer() -> None:
    global _writer, _writer_pid
    pid = os.getpid()
    if _writer is not None and _writer_pid == pid and _writer.is_alive():
        return
    with _writer_lock:
        if _writer is not None and _writer_pid == pid and _writer.is_alive():
            return
        _writer_pid = pid
        _writer = threading.Thread(target=_writer_loop, name="audit-writer", daemon=True)
        _writer.start()


def flush_audit_queue() -> None:
    """Write whatever is still queued, synchronously. Called on shutdown."""
    batch: List[dict] = []
    while True:
        _drain(batch)
        if not batch:
            return
        _write(batch)
        batch = []


atexit.register(flush_audit_queue)

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,36}$")


def write_audit_event(
    action: str,
    performed_by: str,
    target: str = "",
    details: str = "",
    ip_address: str = "",
    *,
    event_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> str:
    """Record one audit event. Returns at once and never raises: the write
    happens on the audit writer thread (see above). Returns the event id."""
    event = {
        "id": event_id if event_id and _UUID_RE.match(event_id) else str(uuid.uuid4()),
        "timestamp": timestamp or datetime.now(EASTERN),
        "action": str(action or "")[:MAX_ACTION],
        "performed_by": str(performed_by or "").strip().lower()[:320],
        "target": str(target or "")[:MAX_TARGET],
        "details": str(details or "")[:MAX_DETAILS],
        "ip_address": str(ip_address or "")[:MAX_IP],
    }
    try:
        _queue.put_nowait(event)
        mark = _request_audit.get()
        if mark is not None:
            mark["logged"] = True
        _ensure_writer()
    except queue.Full:
        _spool([event], RuntimeError("audit queue full"))
    except Exception as e:                           # thread couldn't start
        _spool([event], e)
    logger.info(f"[AUDIT] {event['action']} by {event['performed_by']} → {event['target']}")
    return event["id"]


# ============================================================================
# REQUEST / RESPONSE MODELS
# ============================================================================

class AuditEventRequest(BaseModel):
    action: str
    target: Optional[str] = ""
    details: Optional[str] = ""
    # Set by the browser outbox: a stable id so a retry is a no-op, and how
    # long ago the event happened so a late delivery keeps its real time.
    id: Optional[str] = None
    age_ms: Optional[int] = None


class AuditBatchRequest(BaseModel):
    events: List[Any]          # judged one by one, so one bad entry fails alone


class BulkDeleteRequest(BaseModel):
    ids: List[str]


class AssistantSettingsRequest(BaseModel):
    # All Optional[...] = None (not a concrete default) on purpose: the PUT
    # handler below only overwrites fields that are actually present in the
    # request body, so the enable/disable toggle and the avatar/voice panel
    # can each PUT their own slice of this row without clobbering the other's
    # last saved values.
    enabled: Optional[bool] = None
    avatar_id: Optional[str] = None
    appearance: Optional[str] = None
    reduce_motion: Optional[bool] = None
    text_size: Optional[str] = None
    avatar_size: Optional[str] = None
    auto_send_voice: Optional[bool] = None


# ============================================================================
# SHARED FILTER HELPER
# ============================================================================

# Categories group the raw action keys into the five buckets the Audit Log UI
# filters on. Keep this in sync with CATEGORY_OF() in AuditPage.jsx — the
# frontend colours rows with the same grouping.
_CATEGORY_SQL = {
    "Roles":      ("action LIKE 'role\\_%' ESCAPE '\\'", []),
    "Access":     ("action IN ('user_login','user_logout','session_timeout')", []),
    "Admin":      ("action LIKE 'audit\\_%' ESCAPE '\\'", []),
    "Navigation": ("(action LIKE 'page\\_%' ESCAPE '\\' OR action LIKE 'in-page%')", []),
}
# "Data" is the catch-all: anything that isn't one of the four above.
_CATEGORY_SQL["Data"] = (
    "NOT (" + " OR ".join(sql for sql, _ in _CATEGORY_SQL.values()) + ")",
    [],
)

# Actions the Audit Log flags for a second look (the "Needs a look" list,
# the Sensitive counts). Keep in sync with SENSITIVE in AuditPage.jsx.
_SENSITIVE_ACTIONS = [
    "role_deleted", "role_removed", "streamsets_secret_viewed",
    "pipeline_validation_secret_viewed", "data_api_key_regenerated",
    "audit_log_deleted", "audit_edit_mode_accessed",
]

# Actions only the server writes. A browser posting one of these would be
# forging a security event, so /log refuses them.
_SERVER_ONLY = re.compile(
    r"^(role_.*|audit_log_deleted|.*_secret_viewed|data_api_key_.*|api_call)$")

# The same five buckets as one expression, for counting per category in a
# single GROUP BY.
_CATEGORY_CASE = (
    "CASE WHEN " + _CATEGORY_SQL["Roles"][0] + " THEN 'Roles' "
    "WHEN " + _CATEGORY_SQL["Access"][0] + " THEN 'Access' "
    "WHEN " + _CATEGORY_SQL["Admin"][0] + " THEN 'Admin' "
    "WHEN " + _CATEGORY_SQL["Navigation"][0] + " THEN 'Navigation' "
    "ELSE 'Data' END"
)

_RANGE_HOURS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}

# Must match the expression audit_log_search_trgm_idx is built on
# (migrations/smarthub_core/V002) or the search can't use that index. chr(1)
# between the columns keeps a search term from matching across two of them.
_SEARCH_EXPR = "lower(performed_by || chr(1) || action || chr(1) || details || chr(1) || target)"


def _range_cutoff(range_key: Optional[str]) -> Optional[datetime]:
    """Turn a '24h' / '7d' / '30d' range key into the moment to compare
    against. 'all' (or anything unknown) means no lower bound."""
    hours = _RANGE_HOURS.get((range_key or "").lower())
    if not hours:
        return None
    return datetime.now(EASTERN) - timedelta(hours=hours)


def _day_start(day: date) -> datetime:
    """Midnight Eastern at the start of a calendar day. localize() rather than
    replace(tzinfo=) so the DST offset is the one that day actually has."""
    return EASTERN.localize(datetime(day.year, day.month, day.day))


def _parse_day(raw: str, field: str) -> date:
    try:
        return date.fromisoformat((raw or "").strip()[:10])
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field} must be a date (YYYY-MM-DD)")


def _like_escape(term: str) -> str:
    """So a % or _ someone types is searched for, not treated as a wildcard."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _build_where(
    search: Optional[str],
    action: Optional[str],
    performed_by: Optional[str],
    date_from: Optional[str],
    date_to: Optional[str],
    category: Optional[str] = None,
    range_key: Optional[str] = None,
) -> tuple[str, list]:
    """
    Build a parameterised WHERE clause from the common filter fields.
    Returns (where_sql, params_list).
    """
    clauses = []
    params = []

    if category and category.lower() != "all":
        cat = _CATEGORY_SQL.get(category.capitalize())
        if cat:
            clauses.append(cat[0])
            params.extend(cat[1])

    cutoff = _range_cutoff(range_key)
    if cutoff:
        clauses.append("timestamp >= ?")
        params.append(cutoff)

    if search:
        clauses.append(f"{_SEARCH_EXPR} LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(search.lower())}%")

    if action:
        clauses.append("action = ?")
        params.append(action)
    else:
        # api_call rows (the safety net) stay out of the stream, counts,
        # charts and export unless someone picks "API Call" as the action.
        clauses.append("action <> 'api_call'")

    if performed_by:
        clauses.append("LOWER(performed_by) LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(performed_by.lower())}%")

    if date_from:
        clauses.append("timestamp >= ?")
        params.append(_day_start(_parse_day(date_from, "date_from")))

    if date_to:
        # The whole of that day, Eastern: everything before the next midnight.
        clauses.append("timestamp < ?")
        params.append(_day_start(_parse_day(date_to, "date_to") + timedelta(days=1)))

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _require_admin(auth: dict):
    """Authorisation is resolved server-side from the caller's bearer token.
    The previous implementation trusted an X-User-Role header, which any
    client could set to "admin"."""
    if not auth.get("permissions", {}).get("manageUsers"):
        raise HTTPException(status_code=403, detail="Admin role required")


# ============================================================================
# ENDPOINTS
# ============================================================================

@audit_router.get("/logs")
def get_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    performed_by: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    range_key: Optional[str] = Query(None, alias="range"),
    auth: dict = Depends(require_permission("viewAudit")),
):
    """Return paginated, filtered audit log entries (newest first)."""
    x_user_email = auth["user"]["email"]
    where, params = _build_where(search, action, performed_by, date_from, date_to, category, range_key)

    offset = (page - 1) * page_size
    # The page is always read fresh (one round trip). The total behind the
    # pager is an exact COUNT over everything matching, which over a year of
    # page views costs far more than the page itself, so it comes from the
    # same short-lived cache as the dashboard numbers.
    with _db(read_only=True) as conn:
        rows = conn.execute(
            f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC, id LIMIT ? OFFSET ?",
            params + [page_size, offset],
        ).fetchall()
    logs = [dict(r) for r in rows]

    def _count():
        with _db(read_only=True) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM audit_log {where}", params).fetchone()[0]

    key = ("count", search, action, performed_by, date_from, date_to, category, range_key)
    total = _cached(key, _count)
    # Never let a cached total contradict the page in front of the user.
    total = max(total, offset + len(logs))
    return {
        "logs": logs,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@audit_router.get("/logs/ids")
def get_audit_log_ids(
    search: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    performed_by: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    range_key: Optional[str] = Query(None, alias="range"),
    auth: dict = Depends(require_permission("viewAudit")),
):
    """Return every log id matching the current filters, unpaginated — backs
    the audit-edit screen's 'select all matching' checkbox."""
    where, params = _build_where(search, action, performed_by, date_from, date_to, category, range_key)
    # Capped: unfiltered, this was every id in the log (millions) in one
    # response and then one DELETE. Newest first, so the cap keeps the
    # rows the person is looking at.
    with _db(read_only=True) as conn:
        rows = conn.execute(
            f"SELECT id FROM audit_log {where} ORDER BY timestamp DESC, id LIMIT ?",
            params + [IDS_LIMIT + 1],
        ).fetchall()
    return {"ids": [r["id"] for r in rows[:IDS_LIMIT]], "truncated": len(rows) > IDS_LIMIT}


IDS_LIMIT = 20_000


@audit_router.get("/stats")
def get_audit_stats(auth: dict = Depends(require_permission("viewAudit"))):
    """Return summary statistics for the audit dashboard cards."""
    return _cached(("stats",), _audit_stats)


def _audit_stats() -> dict:
    today = datetime.now(EASTERN).date()
    today_start, tomorrow_start = _day_start(today), _day_start(today + timedelta(days=1))

    # Same rule as the Roles category, so role_reset (and any role_* added
    # later) is counted; the old hand list missed role_reset.
    with _db(read_only=True) as conn:
        row = conn.execute(
            f"""SELECT (SELECT COUNT(*) FROM audit_log) AS total,
                      (SELECT COUNT(*) FROM audit_log
                        WHERE timestamp >= ? AND timestamp < ?) AS today,
                      (SELECT COUNT(*) FROM audit_log WHERE {_CATEGORY_SQL["Roles"][0]}) AS role_changes,
                      (SELECT COUNT(*) FROM (SELECT LOWER(performed_by) FROM audit_log
                                             WHERE performed_by <> '' AND performed_by <> 'unknown'
                                             AND LOWER(performed_by) NOT LIKE '%@apikey.local'
                                             GROUP BY 1) AS people) AS unique_users,
                      (SELECT COUNT(*) FROM audit_log
                        WHERE timestamp >= ? AND action = ANY(?)) AS sensitive_7d,
                      (SELECT json_agg(json_build_array(hour, n)) FROM (
                          SELECT EXTRACT(HOUR FROM timestamp AT TIME ZONE 'America/New_York')::int AS hour,
                                 COUNT(*) AS n
                          FROM audit_log WHERE timestamp >= ? AND timestamp < ?
                          GROUP BY 1) AS x) AS hourly""",
            (today_start, tomorrow_start,
             datetime.now(EASTERN) - timedelta(days=7), _SENSITIVE_ACTIONS,
             today_start, tomorrow_start),
        ).fetchone()
    total, today_count = row["total"], row["today"]
    role_changes, unique_users = row["role_changes"], row["unique_users"]

    hourly = [0] * 24
    for h, n in (json.loads(row["hourly"]) if row["hourly"] else []):
        if h is not None and 0 <= h < 24:
            hourly[h] = n

    return {
        "total": total,
        "today": today_count,
        "role_changes": role_changes,
        "unique_users": unique_users,
        "sensitive_7d": row["sensitive_7d"],
        "hourly_today": hourly,
        "current_hour": datetime.now(EASTERN).hour,
        "date": today.isoformat(),
    }


# The banner dial on the Audit Log reads this every 15 seconds while Live is
# on, so it has to be both fresh and cheap: one index range scan over today's
# rows, shared between everyone polling for a few seconds.
TODAY_TTL_SECONDS = 5
_today_cache = {"at": 0.0, "value": None}
_today_lock = threading.Lock()


@audit_router.get("/today")
def get_audit_today(auth: dict = Depends(require_permission("viewAudit"))):
    """Today's events per hour (Eastern) plus the newest few, read fresh."""
    with _today_lock:
        hit = _today_cache["value"]
        if hit is not None and time.monotonic() - _today_cache["at"] < TODAY_TTL_SECONDS:
            return hit
    value = _audit_today()
    with _today_lock:
        _today_cache["at"], _today_cache["value"] = time.monotonic(), value
    return value


def _audit_today() -> dict:
    now = datetime.now(EASTERN)
    today_start, tomorrow_start = _day_start(now.date()), _day_start(now.date() + timedelta(days=1))
    with _db(read_only=True) as conn:
        row = conn.execute(
            """SELECT
                 (SELECT json_agg(json_build_array(hour, n)) FROM (
                     SELECT EXTRACT(HOUR FROM timestamp AT TIME ZONE 'America/New_York')::int AS hour,
                            COUNT(*) AS n
                     FROM audit_log WHERE timestamp >= ? AND timestamp < ?
                       AND action <> 'api_call'   -- hidden from the stream, so not on the dial
                     GROUP BY 1) AS x) AS hourly,
                 (SELECT json_agg(json_build_array(id, timestamp, action) ORDER BY timestamp DESC, id) FROM (
                     SELECT id, timestamp, action FROM audit_log
                     WHERE timestamp >= ? AND action <> 'api_call'
                     ORDER BY timestamp DESC, id LIMIT 20) AS y) AS latest""",
            (today_start, tomorrow_start, today_start),
        ).fetchone()
    hourly = [0] * 24
    for h, n in (json.loads(row["hourly"]) if row["hourly"] else []):
        if h is not None and 0 <= h < 24:
            hourly[h] = n
    latest = json.loads(row["latest"]) if row["latest"] else []
    return {
        "date": now.date().isoformat(),
        "current_hour": now.hour,
        "hourly": hourly,
        "total": sum(hourly),
        "latest": [{"id": i, "timestamp": ts, "action": a} for i, ts, a in latest],
    }


DAILY_DAYS = 30


@audit_router.get("/analytics")
def get_audit_analytics(
    search: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    performed_by: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    range_key: Optional[str] = Query(None, alias="range"),
    auth: dict = Depends(require_permission("viewAudit")),
):
    """Aggregates behind the Analytics tab and the stream sidebar.

    Everything here honours the same filters as /logs, so whatever the user
    has narrowed the stream down to is what the charts describe. Counting is
    done in SQL rather than by pulling rows back and tallying them in Python,
    so a log with a few hundred thousand entries stays fast.
    """
    key = ("analytics", search, action, performed_by, date_from, date_to, category, range_key)
    return _cached(key, lambda: _audit_analytics(search, action, performed_by, date_from, date_to,
                                                 category, range_key))


def _audit_analytics(search, action, performed_by, date_from, date_to, category, range_key) -> dict:
    where, params = _build_where(search, action, performed_by, date_from, date_to, category, range_key)
    # The 30-day chart ignores the range (a 7-day range would leave it mostly
    # empty); the category chips ignore category and action (each chip shows
    # what it would return if picked).
    where_days, params_days = _build_where(search, action, performed_by, date_from, date_to, category, None)
    where_cats, params_cats = _build_where(search, None, performed_by, date_from, date_to, None, range_key)

    now = datetime.now(EASTERN)
    today_start = _day_start(now.date())
    month_start = _day_start((now - timedelta(days=DAILY_DAYS - 1)).date())

    def _and(extra_sql: str) -> str:
        return f"{where} AND {extra_sql}" if where else f"WHERE {extra_sql}"

    # Everything in one statement (one round trip, one snapshot). NOT
    # MATERIALIZED lets each aggregate read audit_log through its own best
    # index instead of spooling the filtered rows first.
    with _db(read_only=True) as conn:
        row = conn.execute(
            f"""WITH f AS NOT MATERIALIZED (
                    SELECT id, timestamp, action, performed_by, target FROM audit_log {where}
                )
                SELECT
                  (SELECT COUNT(*) FROM f) AS matched,
                  (SELECT json_agg(json_build_array(hour, n)) FROM (
                      SELECT EXTRACT(HOUR FROM timestamp AT TIME ZONE 'America/New_York')::int AS hour,
                             COUNT(*) AS n
                      FROM f WHERE timestamp >= ? GROUP BY 1) AS x) AS hourly,
                  (SELECT json_agg(json_build_array(dow, hour, n)) FROM (
                      SELECT EXTRACT(ISODOW FROM timestamp AT TIME ZONE 'America/New_York')::int AS dow,
                             EXTRACT(HOUR FROM timestamp AT TIME ZONE 'America/New_York')::int AS hour,
                             COUNT(*) AS n
                      FROM f GROUP BY 1, 2) AS x) AS heat,
                  (SELECT COUNT(*) FROM f WHERE action = ANY(?)) AS sensitive_count,
                  (SELECT json_agg(json_build_array(id, timestamp, action, performed_by, target)
                                   ORDER BY timestamp DESC) FROM (
                      SELECT id, timestamp, action, performed_by, target FROM f
                      WHERE action = ANY(?) ORDER BY timestamp DESC LIMIT 6) AS x) AS sensitive,
                  (SELECT json_agg(json_build_array(action, n) ORDER BY n DESC, action) FROM (
                      SELECT action, COUNT(*) AS n FROM f GROUP BY 1) AS x) AS actions,
                  (SELECT json_agg(json_build_array(email, n) ORDER BY n DESC, email) FROM (
                      SELECT LOWER(performed_by) AS email, COUNT(*) AS n
                      FROM f WHERE performed_by <> '' AND performed_by <> 'unknown'
                      GROUP BY 1 ORDER BY n DESC, email LIMIT 8) AS x) AS actors""",
            params + [today_start, _SENSITIVE_ACTIONS, _SENSITIVE_ACTIONS],
        ).fetchone()
        daily_sql = f"{where_days} AND timestamp >= ?" if where_days else "WHERE timestamp >= ?"
        daily_rows = conn.execute(
            f"""SELECT to_char(timestamp AT TIME ZONE 'America/New_York', 'YYYY-MM-DD') AS day,
                       COUNT(*) AS n
                FROM audit_log {daily_sql} GROUP BY 1""",
            params_days + [month_start],
        ).fetchall()
        cat_rows = conn.execute(
            f"SELECT {_CATEGORY_CASE} AS cat, COUNT(*) AS n FROM audit_log {where_cats} GROUP BY 1",
            params_cats,
        ).fetchall()

    def _pairs(text):
        return json.loads(text) if text else []

    matched = row["matched"]
    hourly_rows = [{"hour": h, "n": n} for h, n in _pairs(row["hourly"])]
    action_rows = [{"action": a, "n": n} for a, n in _pairs(row["actions"])]
    actor_rows = [{"email": e, "n": n} for e, n in _pairs(row["actors"])]

    hourly = [0] * 24
    for r in hourly_rows:
        if r["hour"] is not None and 0 <= r["hour"] < 24:
            hourly[r["hour"]] = r["n"]

    by_day = {r["day"]: r["n"] for r in daily_rows}
    daily = []
    for i in range(DAILY_DAYS - 1, -1, -1):
        d = (now - timedelta(days=i)).date()
        daily.append({"date": d.isoformat(), "count": by_day.get(d.isoformat(), 0)})

    return {
        "matched": matched,
        "hourly": hourly,
        "current_hour": now.hour,
        "daily": daily,
        "actions": [{"action": r["action"], "count": r["n"]} for r in action_rows],
        "actors": [{"email": r["email"], "count": r["n"]} for r in actor_rows],
        # 7 rows Mon..Sun x 24 hours, Eastern
        "heat": _heat(_pairs(row["heat"])),
        "sensitive_count": row["sensitive_count"],
        "sensitive": [
            {"id": i, "timestamp": ts, "action": a, "performed_by": p, "target": t}
            for i, ts, a, p, t in _pairs(row["sensitive"])
        ],
        "categories": {r["cat"]: r["n"] for r in cat_rows},
    }


def _heat(triples) -> list:
    grid = [[0] * 24 for _ in range(7)]
    for dow, hour, n in triples:
        if dow and 1 <= dow <= 7 and hour is not None and 0 <= hour < 24:
            grid[dow - 1][hour] = n
    return grid


@audit_router.get("/people")
def get_audit_people(
    search: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    performed_by: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    range_key: Optional[str] = Query(None, alias="range"),
    auth: dict = Depends(require_permission("viewAudit")),
):
    """The People tab: the most active people under the current filters, each
    with their sensitive count, last activity anywhere in the log, top three
    actions and a 21-day activity line."""
    key = ("people", search, action, performed_by, date_from, date_to, category, range_key)
    return _cached(key, lambda: _audit_people(search, action, performed_by, date_from, date_to,
                                              category, range_key))


PEOPLE_LIMIT = 24
PEOPLE_SPARK_DAYS = 21


def _audit_people(search, action, performed_by, date_from, date_to, category, range_key) -> dict:
    where, params = _build_where(search, action, performed_by, date_from, date_to, category, range_key)
    where_days, params_days = _build_where(search, action, performed_by, date_from, date_to, category, None)
    now = datetime.now(EASTERN)
    spark_start = _day_start((now - timedelta(days=PEOPLE_SPARK_DAYS - 1)).date())

    # Three passes, each one scan at most: who (and how much), what they did,
    # and their last 21 days. last_seen is an index probe per person on
    # audit_log_actor_idx (lower(performed_by), timestamp).
    with _db(read_only=True) as conn:
        rows = conn.execute(
            f"""WITH top AS (
                    SELECT LOWER(performed_by) AS email, COUNT(*) AS n,
                           COUNT(*) FILTER (WHERE action = ANY(?)) AS sens
                    FROM audit_log {where}
                    GROUP BY 1
                    HAVING LOWER(performed_by) <> '' AND LOWER(performed_by) <> 'unknown'
                    ORDER BY n DESC, email LIMIT ?
                )
                SELECT top.email, top.n, top.sens,
                       (SELECT MAX(a.timestamp) FROM audit_log a
                         WHERE LOWER(a.performed_by) = top.email) AS last_seen
                FROM top ORDER BY top.n DESC, top.email""",
            [_SENSITIVE_ACTIONS] + params + [PEOPLE_LIMIT],
        ).fetchall()
        emails = [r["email"] for r in rows]
        action_rows, spark_rows = [], []
        if emails:
            clause = f"{where} AND" if where else "WHERE"
            action_rows = conn.execute(
                f"""SELECT LOWER(performed_by) AS email, action, COUNT(*) AS n
                    FROM audit_log {clause} LOWER(performed_by) = ANY(?)
                    GROUP BY 1, 2""",
                params + [emails],
            ).fetchall()
            clause = f"{where_days} AND" if where_days else "WHERE"
            spark_rows = conn.execute(
                f"""SELECT LOWER(performed_by) AS email,
                           to_char(timestamp AT TIME ZONE 'America/New_York', 'YYYY-MM-DD') AS day,
                           COUNT(*) AS n
                    FROM audit_log {clause} timestamp >= ? AND LOWER(performed_by) = ANY(?)
                    GROUP BY 1, 2""",
                params_days + [spark_start, emails],
            ).fetchall()

    days = [(now - timedelta(days=i)).date().isoformat() for i in range(PEOPLE_SPARK_DAYS - 1, -1, -1)]
    per_day, per_action = {}, {}
    for r in spark_rows:
        per_day.setdefault(r["email"], {})[r["day"]] = r["n"]
    for r in action_rows:
        per_action.setdefault(r["email"], []).append((r["action"], r["n"]))

    def top3(email):
        ranked = sorted(per_action.get(email, []), key=lambda x: (-x[1], x[0]))[:3]
        return [{"action": a, "count": n} for a, n in ranked]

    return {
        "people": [
            {
                "email": r["email"],
                "count": r["n"],
                "sensitive": r["sens"],
                "last_seen": r["last_seen"],
                "top_actions": top3(r["email"]),
                "daily": [per_day.get(r["email"], {}).get(d, 0) for d in days],
            }
            for r in rows
        ],
    }


@audit_router.get("/logs/{log_id}/context")
def get_audit_log_context(log_id: str, auth: dict = Depends(require_permission("viewAudit"))):
    """One event plus the same person's two events either side of it, newest
    first - the detail panel's "around this moment" list, and how a shared
    link to an event opens when that event isn't on the current page."""
    with _db(read_only=True) as conn:
        row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (log_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Log entry not found")
        who, ts = row["performed_by"], row["timestamp"]
        newer = conn.execute(
            "SELECT * FROM audit_log WHERE LOWER(performed_by) = LOWER(?) AND (timestamp, id) > (?, ?) "
            "ORDER BY timestamp, id LIMIT 2",
            (who, ts, log_id),
        ).fetchall()
        older = conn.execute(
            "SELECT * FROM audit_log WHERE LOWER(performed_by) = LOWER(?) AND (timestamp, id) < (?, ?) "
            "ORDER BY timestamp DESC, id DESC LIMIT 2",
            (who, ts, log_id),
        ).fetchall()
    event = dict(row)
    return {
        "event": event,
        "around": [dict(r) for r in reversed(newer)] + [event] + [dict(r) for r in older],
    }


@audit_router.get("/export")
def export_audit_logs(
    search: Optional[str] = Query(None),
    action: Optional[str] = Query(None),
    performed_by: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    range_key: Optional[str] = Query(None, alias="range"),
    auth: dict = Depends(require_permission("viewAudit")),
):
    """Export filtered audit logs as a CSV download.

    Streamed a few thousand rows at a time, each batch its own short query
    continuing from where the last one stopped (keyset, not OFFSET). A year of
    page views is millions of rows: building that whole file in memory could
    take a worker down, and holding one query open for a slow download would
    tie up a database connection for minutes.
    """
    where, params = _build_where(search, action, performed_by, date_from, date_to, category, range_key)
    fields = ["id", "timestamp", "action", "performed_by", "target", "details", "ip_address"]
    batch = 5000

    def rows_as_csv():
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        after = None
        while True:
            clause, args = where, list(params)
            if after is not None:
                keyset = "(timestamp, id) < (?, ?)"
                clause = f"{where} AND {keyset}" if where else f"WHERE {keyset}"
                args += list(after)
            with _db(read_only=True) as conn:
                rows = conn.execute(
                    f"SELECT {', '.join(fields)} FROM audit_log {clause} "
                    "ORDER BY timestamp DESC, id DESC LIMIT ?",
                    args + [batch],
                ).fetchall()
            writer.writerows(rows)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)
            if len(rows) < batch:
                return
            after = (rows[-1]["timestamp"], rows[-1]["id"])

    filename = f"audit_log_{datetime.now(EASTERN).strftime('%Y%m%d_%H%M%S')}.csv"
    return StreamingResponse(
        rows_as_csv(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return getattr(request.client, "host", "") or ""


CLIENT_MAX_AGE_MS = 7 * 24 * 3600 * 1000


def _accept_client_event(raw: dict, email: str, ip: str) -> Optional[str]:
    """Validate one browser event and queue it. Returns None when accepted,
    otherwise why not. Actor always comes from the verified token."""
    action = str(raw.get("action") or "").strip()
    if not action or len(action) > MAX_ACTION:
        return "bad action"
    if _SERVER_ONLY.match(action):
        return "server-only action"
    try:
        age = min(max(int(raw.get("age_ms") or 0), 0), CLIENT_MAX_AGE_MS)
    except (TypeError, ValueError):
        age = 0
    write_audit_event(
        action=action,
        performed_by=email,
        target=str(raw.get("target") or ""),
        details=str(raw.get("details") or ""),
        ip_address=ip,
        event_id=str(raw.get("id") or "") or None,
        # From the server's clock minus the age the browser measured, so a
        # skewed client clock can't move it; only queued deliveries are aged.
        timestamp=datetime.now(EASTERN) - timedelta(milliseconds=age),
    )
    # Logout-type events take the user off the active list instead of
    # keeping them there for ACTIVE_TIMEOUT_MINUTES.
    if action in ("user_logout", "session_timeout"):
        _clear_heartbeat(email)
    elif age < ACTIVE_TIMEOUT_MINUTES * 60_000:
        _record_heartbeat(email)
    return None


@audit_router.post("/log")
def post_audit_event(event: AuditEventRequest, request: Request, auth: dict = Depends(require_auth)):
    raw = event.model_dump() if hasattr(event, "model_dump") else event.dict()
    reason = _accept_client_event(raw, auth["user"]["email"], client_ip(request))
    if reason:
        raise HTTPException(status_code=422, detail=reason)
    return {"success": True}


@audit_router.post("/log/batch")
def post_audit_batch(body: AuditBatchRequest, request: Request, auth: dict = Depends(require_auth)):
    """The browser outbox delivers queued events here, up to 200 at a time.
    Judged one by one: a bad event is reported, never blocks the others."""
    if len(body.events) > 200:
        raise HTTPException(status_code=413, detail="At most 200 events per batch")
    email, ip = auth["user"]["email"], client_ip(request)
    rejected = []
    for raw in body.events:
        reason = _accept_client_event(raw if isinstance(raw, dict) else {}, email, ip)
        if reason:
            rejected.append({"id": raw.get("id") if isinstance(raw, dict) else None, "reason": reason})
    return {"success": True, "accepted": len(body.events) - len(rejected), "rejected": rejected}


@audit_router.get("/health")
def get_audit_health(auth: dict = Depends(require_permission("viewAudit"))):
    """Is capture working? ok is false while events wait in the spool (the
    database refused them) or the writer thread is down."""
    try:
        backlog = SPOOL_PATH.stat().st_size if SPOOL_PATH.exists() else 0
    except OSError:
        backlog = 0
    pending = 0
    if backlog:
        try:
            with open(SPOOL_PATH, "rb") as f:
                pending = sum(1 for _ in f)
        except OSError:
            pending = -1
    alive = _writer is not None and _writer.is_alive()
    return {
        "ok": pending == 0 and (alive or _queue.qsize() == 0),
        "writer_alive": alive,
        "queued": _queue.qsize(),
        "spooled_pending": pending,
        **_writer_status,
    }

@audit_router.delete("/logs/{log_id}")
def delete_audit_log(
    log_id: str,
    silent: bool = Query(False),
    auth: dict = Depends(require_auth),
):
    _require_admin(auth)
    x_user_email = auth["user"]["email"]

    with _db(autocommit=True) as conn:
        # DELETE ... RETURNING: two admins deleting the same row can't both
        # succeed, and there's no gap between reading it and removing it.
        row = conn.execute("DELETE FROM audit_log WHERE id = ? RETURNING *", (log_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Log entry not found")
    _drop_aggregates()

    # Silent mode keeps it off the audit table, never off the server log.
    logger.warning("AUDIT-DELETE by %s silent=%s: %s", x_user_email, silent,
                   json.dumps(dict(row), default=str))
    if not silent:
        write_audit_event(
            action="audit_log_deleted",
            performed_by=x_user_email,
            target=log_id,
            details=f"Deleted entry: {row['action']} by {row['performed_by']} at {row['timestamp']}",
        )

    return {"success": True, "deleted": 1}


@audit_router.delete("/logs")
def delete_audit_logs_bulk(
    payload: BulkDeleteRequest,
    silent: bool = Query(False),
    auth: dict = Depends(require_auth),
):
    _require_admin(auth)
    x_user_email = auth["user"]["email"]

    ids = list(dict.fromkeys(i for i in payload.ids if i))
    if len(ids) > IDS_LIMIT:
        raise HTTPException(status_code=413, detail=f"At most {IDS_LIMIT} entries per delete")
    if not ids:
        return {"success": True, "deleted": 0}

    with _db(autocommit=True) as conn:
        # One array parameter however many ids "select all matching" sends.
        deleted = conn.execute(
            "DELETE FROM audit_log WHERE id = ANY(?)", (ids,)
        ).rowcount
    _drop_aggregates()

    logger.warning("AUDIT-DELETE by %s silent=%s: %d entries %s", x_user_email, silent, deleted, ids)
    if not silent and deleted:
        preview = ", ".join(ids[:5]) + ("…" if len(ids) > 5 else "")
        write_audit_event(
            action="audit_log_deleted",
            performed_by=x_user_email,
            target=f"{deleted} entries",
            details=f"Bulk-deleted {deleted} audit log entr{'y' if deleted == 1 else 'ies'} ({preview})",
        )

    return {"success": True, "deleted": deleted}


# ============================================================================
# HEARTBEAT — called every 30 s by the frontend to signal the user is active
# ============================================================================

@audit_router.post("/heartbeat")
def heartbeat(auth: dict = Depends(require_auth)):
    """Record that a user is currently active. Called periodically by the frontend."""
    x_user_email = auth["user"]["email"]
    if x_user_email:
        _record_heartbeat(x_user_email)
    return {"success": True}


@audit_router.get("/actions")
def get_distinct_actions(auth: dict = Depends(require_permission("viewAudit"))):
    """Return the action types actually present in the log, so the filter
    dropdown only ever offers choices that can return results — instead of a
    hand-maintained list that drifts from whatever the code actually logs."""
    return _cached(("actions",), _distinct_actions)


def _distinct_actions() -> dict:
    with _db(read_only=True) as conn:
        rows = conn.execute(
            "SELECT DISTINCT action FROM audit_log WHERE action <> '' ORDER BY action"
        ).fetchall()
    return {"actions": [r["action"] for r in rows]}


@audit_router.get("/unique-users")
def get_unique_users(auth: dict = Depends(require_permission("viewAudit"))):
    """Return all unique users with their total event count and last activity timestamp.

    Groups case-insensitively (same reasoning as get_audit_stats above) so a
    user isn't split into two entries just because their email was logged
    with different casing on different visits. The displayed email is
    whichever casing they used most recently.
    """
    return _cached(("unique-users",), _unique_users)


def _unique_users() -> dict:
    with _db(read_only=True) as conn:
        # Count and newest time per person in one hashed pass, then each
        # person's latest casing from audit_log_actor_idx (one index probe
        # each) - several times faster than windowing every row.
        rows = conn.execute("""
            SELECT (SELECT a.performed_by FROM audit_log a
                     WHERE LOWER(a.performed_by) = people.who
                     ORDER BY a.timestamp DESC LIMIT 1)            AS email,
                   people.event_count,
                   people.last_activity
            FROM (
                SELECT LOWER(performed_by) AS who,
                       COUNT(*)            AS event_count,
                       MAX(timestamp)      AS last_activity
                FROM audit_log
                WHERE performed_by <> '' AND performed_by <> 'unknown'
                GROUP BY 1
            ) AS people
            ORDER BY people.last_activity DESC, people.who
        """).fetchall()
    return {"users": [dict(r) for r in rows]}


# ── search suggestions ───────────────────────────────────────────────────────
# What the Audit Log search boxes suggest, taken from the events the page is
# currently showing: the same range / category / person / date filters as
# /logs. The box being typed in is left out of its own filter (the search text
# for the main box, the person for the person box), and so is the action, so
# a suggestion can switch to another one. Answers are cached briefly in a
# small cache of their own so typing never evicts the dashboard aggregates.
SUGGEST_TTL_SECONDS = 30
_suggest_cache: Dict[tuple, tuple] = {}
_suggest_lock = threading.Lock()


@audit_router.get("/suggest")
def get_audit_suggest(
    q: str = Query("", max_length=100),
    performed_by: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    range_key: Optional[str] = Query(None, alias="range"),
    auth: dict = Depends(require_permission("viewAudit")),
):
    term = q.strip().lower()
    key = (term, (performed_by or "").strip().lower(), date_from, date_to, category, range_key)
    now = time.monotonic()
    with _suggest_lock:
        hit = _suggest_cache.get(key)
        if hit and now - hit[0] < SUGGEST_TTL_SECONDS:
            return hit[1]

    # people: everything but the person filter; targets and actions: all of it
    p_where, p_params = _build_where(None, None, None, date_from, date_to, category, range_key)
    where, params = _build_where(None, None, performed_by, date_from, date_to, category, range_key)
    like = f"%{_like_escape(term)}%"
    person_like = "LOWER(performed_by) LIKE ? ESCAPE '\\'"
    target_like = "target <> '' AND LOWER(target) LIKE ? ESCAPE '\\'"

    def scoped(base: str, extra: str) -> str:
        return f"{base} AND {extra}" if base else f"WHERE {extra}"

    with _db(read_only=True) as conn:
        people = conn.execute(
            f"""SELECT LOWER(performed_by) AS email, COUNT(*) AS n FROM audit_log
                {scoped(p_where, person_like)}
                GROUP BY LOWER(performed_by) ORDER BY n DESC, email LIMIT 8""",
            (*p_params, like),
        ).fetchall()
        targets = conn.execute(
            f"""SELECT target, COUNT(*) AS n FROM audit_log
                {scoped(where, target_like)}
                GROUP BY target ORDER BY n DESC, target LIMIT 6""",
            (*params, like),
        ).fetchall()
        # every action in view — the page matches these against their labels
        actions = conn.execute(
            f"""SELECT action, COUNT(*) AS n FROM audit_log {where}
                GROUP BY action ORDER BY n DESC LIMIT 200""",
            params,
        ).fetchall()

    value = {
        "people": [{"email": r["email"], "count": r["n"]} for r in people],
        "targets": [{"target": r["target"], "count": r["n"]} for r in targets],
        "actions": [{"action": r["action"], "count": r["n"]} for r in actions],
    }
    with _suggest_lock:
        if len(_suggest_cache) > 500:
            _suggest_cache.clear()
        _suggest_cache[key] = (now, value)
    return value


@audit_router.get("/active-users")
def get_active_users(auth: dict = Depends(require_permission("viewAudit"))):
    """Return currently active users and a count."""
    users = _get_active_users()
    return {
        "count": len(users),
        "users": users,
        "timeout_minutes": ACTIVE_TIMEOUT_MINUTES,
    }


# ============================================================================
# ASSISTANT SETTINGS — per-user 3D avatar / widget preferences
#
# The email used to key these rows always comes from require_auth(), i.e. the
# server's own validation of the caller's bearer token — never from a
# client-supplied header. A header can be set to anything by the caller, which
# would otherwise let one user read or overwrite another user's settings.
# ============================================================================

@audit_router.get("/assistant-settings")
def get_assistant_settings(auth: dict = Depends(require_permission("viewAssistantAvatar"))):
    """Return the saved assistant settings for the current (authenticated) user.

    No row yet == the user has never touched these settings == assistant is
    off. `enabled` comes back False in that case, matching the opt-in default.
    """
    email = auth["user"]["email"]
    with _db(read_only=True) as conn:
        row = conn.execute(
            "SELECT * FROM assistant_settings WHERE user_email = ?",
            (email,),
        ).fetchone()

    if not row:
        return {
            "settings": {
                "enabled": False,
                "avatar_id": "",
                "appearance": "standard",
                "reduce_motion": False,
                "text_size": "medium",
                "avatar_size": "medium",
                "auto_send_voice": True,
            }
        }

    return {
        "settings": {
            "enabled": bool(row["enabled"]),
            "avatar_id": row["avatar_id"],
            "appearance": row["appearance"],
            "reduce_motion": bool(row["reduce_motion"]),
            "text_size": row["text_size"],
            "avatar_size": row["avatar_size"] or "medium",
            "auto_send_voice": bool(row["auto_send_voice"]),
        }
    }


@audit_router.put("/assistant-settings")
def put_assistant_settings(
    settings: AssistantSettingsRequest,
    auth: dict = Depends(require_permission("viewAssistantAvatar")),
):
    """Upsert the current (authenticated) user's assistant settings.

    Partial update: any field left out of the request body (None) keeps
    whatever was already stored for it, rather than resetting to a default —
    see AssistantSettingsRequest for why that matters.
    """
    email = auth["user"]["email"]
    # One statement: a field left out (None) keeps what is stored, and two
    # panels saving at the same moment can't overwrite each other's slice the
    # way a read-then-write would. Blank text falls back to its default, as it
    # always has.
    params = {
        "email": email,
        "enabled": settings.enabled,
        "avatar_id": settings.avatar_id,
        "appearance": settings.appearance,
        "reduce_motion": settings.reduce_motion,
        "text_size": settings.text_size,
        "avatar_size": settings.avatar_size,
        "auto_send_voice": settings.auto_send_voice,
        "now": datetime.now(EASTERN),
    }
    with _db(autocommit=True) as conn:
        row = conn.execute(
            """
            INSERT INTO assistant_settings AS s
                (user_email, enabled, avatar_id, appearance, reduce_motion,
                 text_size, avatar_size, auto_send_voice, updated_at)
            VALUES (
                :email,
                COALESCE(:enabled, false),
                COALESCE(:avatar_id, ''),
                COALESCE(NULLIF(:appearance, ''), 'standard'),
                COALESCE(:reduce_motion, false),
                COALESCE(NULLIF(:text_size, ''), 'medium'),
                COALESCE(NULLIF(:avatar_size, ''), 'medium'),
                COALESCE(:auto_send_voice, true),
                :now
            )
            ON CONFLICT (user_email) DO UPDATE SET
                enabled         = COALESCE(:enabled, s.enabled),
                avatar_id       = COALESCE(:avatar_id, s.avatar_id),
                appearance      = CASE WHEN :appearance IS NULL THEN s.appearance
                                       ELSE COALESCE(NULLIF(:appearance, ''), 'standard') END,
                reduce_motion   = COALESCE(:reduce_motion, s.reduce_motion),
                text_size       = CASE WHEN :text_size IS NULL THEN s.text_size
                                       ELSE COALESCE(NULLIF(:text_size, ''), 'medium') END,
                avatar_size     = CASE WHEN :avatar_size IS NULL THEN s.avatar_size
                                       ELSE COALESCE(NULLIF(:avatar_size, ''), 'medium') END,
                auto_send_voice = COALESCE(:auto_send_voice, s.auto_send_voice),
                updated_at      = EXCLUDED.updated_at
            RETURNING enabled, avatar_id, appearance, reduce_motion,
                      text_size, avatar_size, auto_send_voice
            """,
            params,
        ).fetchone()

    merged = {
        "enabled": bool(row["enabled"]),
        "avatar_id": row["avatar_id"],
        "appearance": row["appearance"],
        "reduce_motion": bool(row["reduce_motion"]),
        "text_size": row["text_size"],
        "avatar_size": row["avatar_size"],
        "auto_send_voice": bool(row["auto_send_voice"]),
    }
    return {"success": True, "settings": merged}

# ============================================================================
# SAFETY NET: every authenticated write to the API is recorded
#
# Most modules log their own rich events, but a new endpoint, or a browser
# event that never arrives, would otherwise leave no trace. This records each
# POST / PUT / PATCH / DELETE under /api as "api_call" (method + path, status,
# duration) once the response has gone out: nothing is added to the request's
# time. Unauthenticated calls are not recorded (anyone could flood the log).
# Set SMARTHUB_AUDIT_API_CALLS=false to turn it off.
# ============================================================================

# The event endpoints themselves, and audit deletes, which log their own
# event (or, in silent mode, deliberately only the server log).
_API_CALL_SKIP = {"/api/audit/log", "/api/audit/log/batch", "/api/audit/heartbeat", "/api/audit/logs"}


def _caller_email(headers: dict) -> str:
    from auth import _cached_identity, _identity_from_api_key
    key = headers.get(b"x-api-key", b"").decode("latin-1").strip()
    if key:
        ident = _identity_from_api_key(key)
        return ident["user"]["email"] if ident else ""
    parts = headers.get(b"authorization", b"").decode("latin-1").split()
    if len(parts) == 2 and parts[0].lower() == "bearer":
        ident = _cached_identity(parts[1])
        return (ident or {}).get("user", {}).get("email", "") if ident else ""
    return ""


class AuditApiCallMiddleware:
    def __init__(self, app):
        self.app = app
        self.enabled = (os.environ.get("SMARTHUB_AUDIT_API_CALLS") or "true").strip().lower() \
            not in ("0", "false", "no", "off")

    async def __call__(self, scope, receive, send):
        if (not self.enabled or scope["type"] != "http"
                or scope["method"] not in ("POST", "PUT", "PATCH", "DELETE")
                or not scope["path"].startswith("/api/")
                or scope["path"] in _API_CALL_SKIP
                or scope["path"].startswith("/api/audit/logs/")):
            return await self.app(scope, receive, send)

        started = time.monotonic()
        mark = {"logged": False}
        _request_audit.set(mark)
        status = {"code": 500}

        async def capture(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            try:
                headers = dict(scope.get("headers") or [])
                email = _caller_email(headers)
                # The endpoint logged its own event: no second, vaguer one.
                if email and not mark["logged"]:
                    fwd = headers.get(b"x-forwarded-for", b"").decode("latin-1")
                    ip = fwd.split(",")[0].strip() if fwd else (scope.get("client") or ("",))[0]
                    write_audit_event(
                        action="api_call",
                        performed_by=email,
                        target=f"{scope['method']} {scope['path']}",
                        details=f"status={status['code']} ms={round((time.monotonic() - started) * 1000)}",
                        ip_address=ip or "",
                    )
            except Exception as e:
                logger.warning("api_call audit skipped: %s", e)