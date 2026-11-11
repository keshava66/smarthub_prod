"""
SmartHub's own database: PostgreSQL, every table in the smarthub_core schema.

Everything that keeps application state (RBAC, audit log, Communication Center,
Data API metadata, Data Studio history, What's New, assistant memory) comes
through here, so connection limits, timeouts, time zone and migrations are
decided once instead of per module.

Usage:

    import db

    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM audit_log WHERE id = ?", (log_id,)).fetchone()

Queries keep the ? / :name placeholders the modules already used; they are
rewritten to psycopg2's style once and cached. Rows come back as dicts that
also answer row[0]. Timestamps come back as the same ISO strings the modules
always produced (Eastern, with offset) so nothing downstream changes shape.

Config (environment):
    SMARTHUB_DB_DSN                  libpq DSN or URL. Required.
    SMARTHUB_DB_SCHEMA               default smarthub_core
    SMARTHUB_DB_POOL_MAX             default 20 connections per process
    SMARTHUB_DB_POOL_TIMEOUT_MS      wait for a free connection, default 5000
    SMARTHUB_DB_STATEMENT_TIMEOUT_MS default 30000
    SMARTHUB_DB_LOCK_TIMEOUT_MS      default 5000
    SMARTHUB_DB_IDLE_TX_TIMEOUT_MS   default 60000
    SMARTHUB_DB_CONNECT_TIMEOUT      seconds, default 5
    SMARTHUB_DB_MAX_LIFETIME         seconds a connection is reused, default 1800
    SMARTHUB_DB_AUTO_MIGRATE         apply pending migrations at startup, default true

Migrations: python db.py migrate | python db.py status
"""

import hashlib
import logging
import os
import re
import sys
import threading
import time
import weakref
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, List, Optional

import psycopg2
import psycopg2.errors
import psycopg2.extensions
import psycopg2.extras
import pytz

logger = logging.getLogger(__name__)

EASTERN = pytz.timezone("America/New_York")
MIGRATIONS_DIR = Path(__file__).parent / "migrations" / "smarthub_core"

# Re-exported so modules don't import psycopg2 just to catch these.
Error = psycopg2.Error
IntegrityError = psycopg2.IntegrityError
UniqueViolation = psycopg2.errors.UniqueViolation
ForeignKeyViolation = psycopg2.errors.ForeignKeyViolation
OperationalError = psycopg2.OperationalError


class DatabaseUnavailable(Exception):
    """No connection could be had: not configured, unreachable, or the pool
    stayed full for the whole wait. Callers should answer 503, not 500."""


def _mark(exc: BaseException) -> BaseException:
    """Tag an error as coming from this database. Other modules talk to other
    PostgreSQL servers through the same driver; the app's error handlers use
    the tag so a failing source system isn't reported as the portal's own
    database being down."""
    try:
        exc.smarthub_core = True
    except Exception:
        pass
    return exc


def is_core_error(exc: BaseException) -> bool:
    return isinstance(exc, DatabaseUnavailable) or bool(getattr(exc, "smarthub_core", False))


# ============================================================================
# SETTINGS
# ============================================================================

def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be a whole number, got {raw!r}") from None
    if not low <= value <= high:
        raise RuntimeError(f"{name} must be between {low} and {high}, got {value}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(f"{name} must be true or false, got {raw!r}")


class Settings:
    def __init__(self):
        # DATA_API_METADATA_DSN is what the Data API used before everything
        # moved here; honoured so an existing deployment keeps booting.
        self.dsn = (os.environ.get("SMARTHUB_DB_DSN") or os.environ.get("DATA_API_METADATA_DSN") or "").strip()
        self.schema = (os.environ.get("SMARTHUB_DB_SCHEMA") or "smarthub_core").strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", self.schema):
            raise RuntimeError("SMARTHUB_DB_SCHEMA must be a lowercase SQL identifier")
        self.pool_max = _env_int("SMARTHUB_DB_POOL_MAX", 20, 1, 500)
        self.pool_timeout = _env_int("SMARTHUB_DB_POOL_TIMEOUT_MS", 5000, 0, 120000) / 1000
        self.statement_timeout_ms = _env_int("SMARTHUB_DB_STATEMENT_TIMEOUT_MS", 30000, 0, 3600000)
        self.lock_timeout_ms = _env_int("SMARTHUB_DB_LOCK_TIMEOUT_MS", 5000, 0, 600000)
        self.idle_tx_timeout_ms = _env_int("SMARTHUB_DB_IDLE_TX_TIMEOUT_MS", 60000, 0, 3600000)
        self.connect_timeout = _env_int("SMARTHUB_DB_CONNECT_TIMEOUT", 5, 1, 120)
        self.max_lifetime = _env_int("SMARTHUB_DB_MAX_LIFETIME", 1800, 30, 86400)
        # An idle connection older than this gets a SELECT 1 before it is
        # handed out, so a database restart or a firewall dropping idle
        # sockets costs one retry instead of a failed request.
        self.ping_after = _env_int("SMARTHUB_DB_PING_AFTER", 15, 0, 3600)
        self.idle_timeout = _env_int("SMARTHUB_DB_IDLE_TIMEOUT", 300, 10, 86400)
        self.auto_migrate = _env_bool("SMARTHUB_DB_AUTO_MIGRATE", True)
        self.application_name = (os.environ.get("SMARTHUB_DB_APP_NAME") or "smarthub").strip()[:63]


_settings: Optional[Settings] = None
_settings_lock = threading.Lock()


def settings() -> Settings:
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                # backend/.env, loaded the same way the rest of the portal
                # does, so the app and the scripts both see SMARTHUB_DB_*.
                # Real environment variables still win.
                try:
                    import assistant.env  # noqa: F401
                except ImportError:
                    pass
                _settings = Settings()
    return _settings


def is_configured() -> bool:
    return bool(settings().dsn)


def schema() -> str:
    return settings().schema


# ============================================================================
# CONNECTIONS
# ============================================================================

# After a failed connect, further attempts fail at once for a few seconds
# instead of each waiting out connect_timeout. Several callers (auth on every
# request among them) run on the event loop; without this a database that
# stops answering would freeze the whole portal, not just its own pages.
_OUTAGE_BACKOFF_SECONDS = 3.0
_down_until = 0.0
_down_reason = ""
_down_lock = threading.Lock()


def _note_outage(reason: str) -> None:
    global _down_until, _down_reason
    with _down_lock:
        _down_until = time.monotonic() + _OUTAGE_BACKOFF_SECONDS
        _down_reason = reason


def _check_outage() -> None:
    if _down_until and time.monotonic() < _down_until:
        raise DatabaseUnavailable(f"SmartHub database unavailable (retrying shortly): {_down_reason}")


def open_connection(*, statement_timeout_ms: Optional[int] = None,
                    lock_timeout_ms: Optional[int] = None,
                    application_name: Optional[str] = None):
    """A raw psycopg2 connection with the session set up the way every query
    here expects. Also used by the Data API's own pools and the migrator."""
    s = settings()
    if not s.dsn:
        raise DatabaseUnavailable("SMARTHUB_DB_DSN is not set")
    _check_outage()
    try:
        raw = psycopg2.connect(
            s.dsn,
            connect_timeout=s.connect_timeout,
            application_name=application_name or s.application_name,
        )
    except psycopg2.OperationalError as exc:
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        _note_outage(reason)
        raise DatabaseUnavailable(f"Could not connect to the SmartHub database: {reason}") from exc

    try:
        raw.autocommit = True
        with raw.cursor() as cur:
            # set_config rather than SET so every value goes in as a bound
            # parameter; one round trip for the lot.
            cur.execute(
                "SELECT set_config('search_path', %s, false),"
                "       set_config('statement_timeout', %s, false),"
                "       set_config('lock_timeout', %s, false),"
                "       set_config('idle_in_transaction_session_timeout', %s, false),"
                "       set_config('TimeZone', 'America/New_York', false)",
                (
                    s.schema,
                    str(s.statement_timeout_ms if statement_timeout_ms is None else statement_timeout_ms),
                    str(s.lock_timeout_ms if lock_timeout_ms is None else lock_timeout_ms),
                    str(s.idle_tx_timeout_ms),
                ),
            )
        raw.autocommit = False
        # json columns come back as the text that was stored; the modules
        # json.loads them themselves, as they did with SQLite. jsonb (only
        # assistant memory uses it) keeps psycopg2's normal parsing.
        psycopg2.extras.register_default_json(raw, loads=lambda text: text)
    except psycopg2.Error as exc:
        _close_quietly(raw)
        raise DatabaseUnavailable(f"Could not set up a database session: {exc}".strip()) from exc
    except Exception:
        _close_quietly(raw)
        raise
    _OPENED.add(raw)
    return raw


# Every connection this process opened, so a forked child can disown them.
_OPENED: "weakref.WeakSet" = weakref.WeakSet()


def _after_fork_in_child() -> None:
    """A child process shares its parent's sockets. Point each inherited
    connection's descriptor at /dev/null in the child, so neither a query nor
    the Terminate sent when the object is freed can reach the parent's
    session. The pools see the pid change and start afresh."""
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
    except OSError:
        return
    try:
        for raw in list(_OPENED):
            try:
                if not raw.closed:
                    os.dup2(devnull, raw.fileno())
            except Exception:
                pass
    finally:
        os.close(devnull)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


def _close_quietly(raw) -> None:
    try:
        raw.close()
    except Exception:
        pass


class _Lease:
    __slots__ = ("raw", "created", "last_used")

    def __init__(self, raw, created: float):
        self.raw = raw
        self.created = created
        self.last_used = created


class Pool:
    """A bounded, thread-safe pool.

    Callers wait up to pool_timeout for a free connection and then get
    DatabaseUnavailable rather than queueing forever. Connections are checked
    before reuse (after being idle a while), retired after max_lifetime, and
    thrown away rather than returned if anything about them looks wrong.
    Never reuses a socket inherited across fork().
    """

    def __init__(self, max_size: int, wait_seconds: float, factory: Callable[[], Any]):
        self.max_size = max_size
        self.wait_seconds = wait_seconds
        self._factory = factory
        self._cv = threading.Condition()
        self._idle: List[_Lease] = []
        self._open = 0
        self._pid = os.getpid()
        self._closed = False
        self.waits = 0
        self.timeouts = 0

    def _check_fork_locked(self) -> None:
        if self._pid != os.getpid():
            # Inherited across fork; _after_fork_in_child has already cut
            # them off from the parent's sessions, so just let them go.
            self._idle = []
            self._open = 0
            self._pid = os.getpid()

    def acquire(self) -> _Lease:
        s = settings()
        deadline = time.monotonic() + self.wait_seconds
        while True:
            lease = None
            with self._cv:
                self._check_fork_locked()
                while True:
                    if self._closed:
                        raise DatabaseUnavailable("Database pool is shut down")
                    if self._idle:
                        lease = self._idle.pop()   # most recently used first
                        break
                    if self._open < self.max_size:
                        self._open += 1
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self.timeouts += 1
                        raise DatabaseUnavailable("All database connections are busy; try again shortly")
                    self.waits += 1
                    self._cv.wait(remaining)

            if lease is None:
                try:
                    return _Lease(self._factory(), time.monotonic())
                except BaseException:
                    with self._cv:
                        self._open -= 1
                        self._cv.notify()
                    raise

            now = time.monotonic()
            stale = (lease.raw.closed
                     or now - lease.created > s.max_lifetime
                     or now - lease.last_used > s.idle_timeout)
            if not stale and s.ping_after and now - lease.last_used > s.ping_after:
                stale = not _ping(lease.raw)
            if not stale:
                return lease
            self._discard(lease)
            # loop: take another idle one or open a fresh one

    def _discard(self, lease: _Lease) -> None:
        _close_quietly(lease.raw)
        with self._cv:
            if self._pid == os.getpid():
                self._open -= 1
            self._cv.notify()

    def release(self, lease: _Lease, broken: bool = False) -> None:
        raw = lease.raw
        if self._pid != os.getpid():
            # Checked out before a fork and handed back in the child.
            return
        if not broken and not raw.closed:
            status = raw.get_transaction_status()
            if status != psycopg2.extensions.TRANSACTION_STATUS_IDLE:
                try:
                    raw.rollback()
                except Exception:
                    broken = True
        now = time.monotonic()
        s = settings()
        expired: List[_Lease] = []
        with self._cv:
            if self._pid != os.getpid():
                return
            # Close connections that have sat unused past idle_timeout. They
            # collect at the bottom of the stack, which a quiet worker never
            # reaches, so they are swept here rather than on checkout.
            while self._idle and now - self._idle[0].last_used > s.idle_timeout:
                expired.append(self._idle.pop(0))
            keep = (not broken and not raw.closed and not self._closed
                    and now - lease.created <= s.max_lifetime)
            if keep:
                lease.last_used = now
                self._idle.append(lease)
                self._cv.notify()
        for old in expired:
            self._discard(old)
        if not keep:
            self._discard(lease)

    def close(self) -> None:
        with self._cv:
            self._closed = True
            idle, self._idle = self._idle, []
            if self._pid != os.getpid():
                return
            self._open -= len(idle)
            self._cv.notify_all()
        for lease in idle:
            _close_quietly(lease.raw)

    def snapshot(self) -> dict:
        with self._cv:
            return {"open": self._open, "idle": len(self._idle), "max": self.max_size,
                    "waits": self.waits, "timeouts": self.timeouts}


def _ping(raw) -> bool:
    # Autocommit for the check itself: one round trip, not BEGIN/SELECT/ROLLBACK.
    try:
        raw.autocommit = True
        with raw.cursor() as cur:
            cur.execute("SELECT 1")
        raw.autocommit = False
        return True
    except Exception:
        return False


_pool: Optional[Pool] = None
_pool_lock = threading.Lock()


def pool() -> Pool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                s = settings()
                _pool = Pool(s.pool_max, s.pool_timeout, open_connection)
    return _pool


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None


# ============================================================================
# QUERIES
# ============================================================================

@lru_cache(maxsize=4096)
def translate(statement: str) -> str:
    """Rewrite ? and :name placeholders to psycopg2's %s / %(name)s.

    Quoted strings, quoted identifiers, comments and ::casts are left alone,
    and a literal % is doubled so psycopg2 doesn't read it as a placeholder.
    Mixing the two placeholder styles in one statement is refused.
    """
    out: List[str] = []
    i, n = 0, len(statement)
    styles = set()
    while i < n:
        c = statement[i]
        if c in ("'", '"'):
            j = i + 1
            while True:
                j = statement.find(c, j)
                if j < 0:
                    raise ValueError("Unterminated quote in SQL")
                if j + 1 < n and statement[j + 1] == c:
                    j += 2
                    continue
                break
            chunk = statement[i:j + 1]
            out.append(chunk.replace("%", "%%"))
            i = j + 1
        elif c == "-" and statement.startswith("--", i):
            j = statement.find("\n", i)
            j = n if j < 0 else j
            out.append(statement[i:j].replace("%", "%%"))
            i = j
        elif c == "/" and statement.startswith("/*", i):
            j = statement.find("*/", i + 2)
            if j < 0:
                raise ValueError("Unterminated comment in SQL")
            out.append(statement[i:j + 2].replace("%", "%%"))
            i = j + 2
        elif c == "$" and re.match(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$", statement[i:]):
            raise ValueError("Dollar-quoted SQL is not supported by the query adapter")
        elif c == "%":
            out.append("%%")
            i += 1
        elif c == "?":
            styles.add("qmark")
            out.append("%s")
            i += 1
        elif c == ":":
            if statement.startswith("::", i):
                out.append("::")
                i += 2
                continue
            m = re.match(r":([A-Za-z_][A-Za-z_0-9]*)", statement[i:])
            if m:
                styles.add("named")
                out.append(f"%({m.group(1)})s")
                i += len(m.group(0))
            else:
                out.append(c)
                i += 1
        else:
            out.append(c)
            i += 1
    if len(styles) > 1:
        raise ValueError("Use either ? or :name placeholders in one statement, not both")
    return "".join(out)


def eastern_iso(value: datetime) -> str:
    """How every module except the Snowflake runner has always written time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(EASTERN).isoformat()


def utc_naive_iso(value: datetime) -> str:
    """The Snowflake runner's older convention: naive UTC, no offset."""
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.isoformat()


class Row(dict):
    """A result row: row["name"] and row[0] both work, like sqlite3.Row,
    and dict(row) / json encoding behave like a plain dict."""
    __slots__ = ("_values",)

    def __init__(self, names, values):
        super().__init__(zip(names, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return dict.__getitem__(self, key)


class Result:
    __slots__ = ("_rows", "_pos", "rowcount")

    def __init__(self, rows: List[Row], rowcount: int):
        self._rows = rows
        self._pos = 0
        self.rowcount = rowcount

    def fetchone(self) -> Optional[Row]:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> List[Row]:
        rows = self._rows[self._pos:]
        self._pos = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


def _converter(fmt: Callable[[datetime], str]):
    def convert(value):
        if isinstance(value, datetime):
            return fmt(value)
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            # Only aggregates (SUM of integers, AVG) produce numerics in these
            # tables; hand them back as plain numbers the way SQLite did.
            return int(value) if value == value.to_integral_value() else float(value)
        return value
    return convert


_NEEDS_CONVERSION = (datetime, date, Decimal)


def _no_nul(value):
    # PostgreSQL text can't hold NUL; SQLite could, and request bodies can
    # carry \u0000. Dropped rather than failing the whole write.
    return value.replace("\x00", "") if isinstance(value, str) and "\x00" in value else value


def _clean_params(params):
    if isinstance(params, dict):
        if any(isinstance(v, str) and "\x00" in v for v in params.values()):
            return {k: _no_nul(v) for k, v in params.items()}
        return params
    if any(isinstance(v, str) and "\x00" in v for v in params):
        return tuple(_no_nul(v) for v in params)
    return params


class Connection:
    """One pooled connection inside one transaction."""

    def __init__(self, raw, datetime_format: Callable[[datetime], str] = eastern_iso):
        self.raw = raw
        self.broken = False
        self._convert = _converter(datetime_format)

    def execute(self, statement: str, params: Any = None) -> Result:
        if params is None:
            params = ()
        elif not isinstance(params, dict):
            params = tuple(params)
        params = _clean_params(params)
        cur = self.raw.cursor()
        try:
            cur.execute(translate(statement), params)
            if cur.description is None:
                return Result([], cur.rowcount)
            names = [d[0] for d in cur.description]
            convert = self._convert
            rows = []
            for values in cur.fetchall():
                if any(isinstance(v, _NEEDS_CONVERSION) for v in values):
                    values = tuple(convert(v) for v in values)
                rows.append(Row(names, values))
            return Result(rows, cur.rowcount)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            # A cancelled statement (timeout) leaves a healthy socket; anything
            # else in this family means the session can't be trusted.
            if self.raw.closed or not isinstance(exc, psycopg2.errors.QueryCanceled):
                self.broken = True
            raise _mark(exc)
        except psycopg2.Error as exc:
            raise _mark(exc)
        finally:
            cur.close()

    def executemany(self, statement: str, seq: Iterable[Any], page_size: int = 500) -> None:
        rows = [_clean_params(p if isinstance(p, dict) else tuple(p)) for p in seq]
        if not rows:
            return
        cur = self.raw.cursor()
        try:
            psycopg2.extras.execute_batch(cur, translate(statement), rows, page_size=page_size)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            self.broken = True
            raise _mark(exc)
        except psycopg2.Error as exc:
            raise _mark(exc)
        finally:
            cur.close()


_ISOLATION = {
    None: None,
    "read_committed": psycopg2.extensions.ISOLATION_LEVEL_READ_COMMITTED,
    "repeatable_read": psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ,
    "serializable": psycopg2.extensions.ISOLATION_LEVEL_SERIALIZABLE,
}


@contextmanager
def transaction(*, read_only: bool = False, isolation: Optional[str] = None,
                autocommit: Optional[bool] = None,
                datetime_format: Callable[[datetime], str] = eastern_iso):
    """A pooled connection, used for one unit of work, always given back.

    Round trips are what a request pays for, so the mode is picked to spend
    as few as possible:

    - read_only with no isolation: autocommit. Each SELECT is one round trip,
      with no BEGIN/COMMIT around it. (Separate SELECTs then see separate
      snapshots - exactly what READ COMMITTED gave them anyway.)
    - autocommit=True: the same for a write that is a single statement, which
      is atomic on its own.
    - otherwise a real transaction: committed when the block finishes, rolled
      back if it raises. isolation / read_only ride along in the BEGIN itself
      rather than costing a SET TRANSACTION round trip.
    """
    if isolation not in _ISOLATION:
        raise ValueError(f"Unknown isolation level {isolation!r}")
    auto = autocommit if autocommit is not None else (read_only and isolation is None)
    ensure_ready()
    owner = pool()
    lease = owner.acquire()
    raw = lease.raw
    conn = Connection(raw, datetime_format)
    tuned = False
    try:
        if auto:
            raw.autocommit = True
        elif read_only or isolation:
            # Client-side only while idle; psycopg2 sends them in the BEGIN.
            raw.isolation_level = _ISOLATION[isolation]
            raw.readonly = True if read_only else None
            tuned = True
        yield conn
        if not auto:
            try:
                raw.commit()
            except psycopg2.Error as exc:
                conn.broken = conn.broken or raw.closed
                raise _mark(exc)
    except BaseException:
        if not auto and not raw.closed:
            try:
                raw.rollback()
            except Exception:
                conn.broken = True
        raise
    finally:
        if not raw.closed and not conn.broken:
            try:
                if auto:
                    raw.autocommit = False
                if tuned:
                    raw.isolation_level = None
                    raw.readonly = None
            except Exception:
                conn.broken = True
        owner.release(lease, broken=conn.broken or raw.closed)


def lock_key(name: str) -> int:
    """A stable signed 64-bit key for pg_advisory_* derived from a name."""
    return int.from_bytes(hashlib.blake2b(name.encode(), digest_size=8).digest(), "big", signed=True)


def advisory_xact_lock(conn: Connection, name: str, wait: bool = True,
                       timeout_ms: Optional[int] = None) -> bool:
    """Serialise work across every worker and replica for the rest of this
    transaction. With wait=False returns False straight away if someone else
    holds it. timeout_ms overrides the session lock_timeout for this wait."""
    key = lock_key(name)
    if not wait:
        return bool(conn.execute("SELECT pg_try_advisory_xact_lock(?) AS ok", (key,)).fetchone()["ok"])
    if timeout_ms is None:
        conn.execute("SELECT pg_advisory_xact_lock(?)", (key,))
        return True
    previous = conn.execute(
        "SELECT current_setting('lock_timeout') AS old, set_config('lock_timeout', ?, true)",
        (str(int(timeout_ms)),),
    ).fetchone()["old"]
    conn.execute("SELECT pg_advisory_xact_lock(?)", (key,))
    conn.execute("SELECT set_config('lock_timeout', ?, true)", (previous,))
    return True


# ============================================================================
# MIGRATIONS
# ============================================================================

_MIGRATION_FILE = re.compile(r"^V(\d{3,})__([a-z0-9_]+)\.sql$")


class Migration:
    def __init__(self, path: Path):
        m = _MIGRATION_FILE.match(path.name)
        if not m:
            raise RuntimeError(f"Bad migration file name: {path.name} (want V001__what_it_does.sql)")
        self.version = int(m.group(1))
        self.name = m.group(2)
        self.path = path
        # Normalised so that copying the file around can't make it look edited:
        # Windows line endings, a byte-order mark and whitespace at the very end
        # (editors and downloads add or drop a final newline) don't count.
        self.sql = path.read_bytes().decode("utf-8-sig").replace("\r\n", "\n").rstrip()
        self.checksum = hashlib.sha256(self.sql.encode("utf-8")).hexdigest()
        # Builds before this normalisation hashed the file with its final
        # newline; a database migrated by one of them must still match.
        self.accepted = {self.checksum, hashlib.sha256((self.sql + "\n").encode("utf-8")).hexdigest()}


def migrations() -> List[Migration]:
    found = [Migration(p) for p in sorted(MIGRATIONS_DIR.glob("*.sql"))]
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Two migration files share a version number")
    return sorted(found, key=lambda m: m.version)


def _applied(cur) -> dict:
    cur.execute("SELECT to_regclass(%s)", (f"{schema()}.schema_migrations",))
    if cur.fetchone()[0] is None:
        return {}
    cur.execute(f'SELECT version, checksum FROM "{schema()}".schema_migrations')
    return {v: c for v, c in cur.fetchall()}


def migrate() -> List[int]:
    """Apply every pending migration, one transaction each. Safe to run from
    several workers at once: they queue on an advisory lock and the later ones
    find nothing to do. Returns the versions applied."""
    wanted = migrations()
    name = schema()
    raw = open_connection(statement_timeout_ms=0, lock_timeout_ms=0,
                          application_name=f"{settings().application_name}-migrate")
    applied_now: List[int] = []
    key = lock_key(f"migrate:{name}")
    try:
        raw.autocommit = True
        with raw.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (key,))
            try:
                cur.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (name,))
                if cur.fetchone() is None:
                    cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{name}"')
                cur.execute(f"""
                    CREATE TABLE IF NOT EXISTS "{name}".schema_migrations (
                        version    integer PRIMARY KEY,
                        name       text NOT NULL,
                        checksum   text NOT NULL,
                        applied_at timestamptz NOT NULL DEFAULT now()
                    )""")
                done = _applied(cur)
                for m in wanted:
                    if m.version in done:
                        if done[m.version] not in m.accepted:
                            raise RuntimeError(
                                f"Migration V{m.version:03d} ({m.name}) has been edited since it was "
                                "applied. Put the change in a new migration file instead.")
                        continue
                    logger.info("Applying database migration V%03d %s", m.version, m.name)
                    raw.autocommit = False
                    try:
                        # DDL waits at most 30s for a lock, so a migration can't
                        # sit behind a long query while every request queues
                        # behind it.
                        cur.execute("SELECT set_config('search_path', %s, true),"
                                    "       set_config('lock_timeout', '30s', true)", (name,))
                        cur.execute(m.sql)
                        cur.execute(f'INSERT INTO "{name}".schema_migrations (version, name, checksum) '
                                    "VALUES (%s, %s, %s)", (m.version, m.name, m.checksum))
                        raw.commit()
                    except Exception:
                        raw.rollback()
                        raise
                    finally:
                        raw.autocommit = True
                    applied_now.append(m.version)
                unknown = sorted(set(done) - {m.version for m in wanted})
                if unknown:
                    logger.warning("Database has migrations this code doesn't know about: %s "
                                   "(running an older build against a newer schema?)", unknown)
            finally:
                # Closing the session releases the lock anyway; this just
                # frees it sooner, and must not hide the real error.
                try:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
                except Exception:
                    pass
    finally:
        _close_quietly(raw)
    return applied_now


def pending() -> List[Migration]:
    raw = open_connection(application_name=f"{settings().application_name}-migrate")
    try:
        with raw.cursor() as cur:
            done = _applied(cur)
        raw.rollback()
    finally:
        _close_quietly(raw)
    return [m for m in migrations() if m.version not in done]


_ready = False
_ready_lock = threading.Lock()


def ensure_ready() -> None:
    """Make sure the schema is current, once per process. With auto-migrate
    off, refuse to run against a database that is behind the code."""
    global _ready
    if _ready:
        return
    with _ready_lock:
        if _ready:
            return
        if not is_configured():
            raise DatabaseUnavailable("SMARTHUB_DB_DSN is not set - the portal needs its PostgreSQL database")
        if settings().auto_migrate:
            applied = migrate()
            if applied:
                logger.info("Database schema %s is up to date (applied %s)", schema(), applied)
        else:
            behind = pending()
            if behind:
                raise RuntimeError(
                    "Database schema is behind this build: "
                    + ", ".join(f"V{m.version:03d}" for m in behind)
                    + ". Run `python db.py migrate` or set SMARTHUB_DB_AUTO_MIGRATE=true.")
        _ready = True


def health() -> dict:
    started = time.monotonic()
    with transaction(read_only=True) as conn:
        conn.execute("SELECT 1")
    return {"ok": True, "ms": round((time.monotonic() - started) * 1000, 1), "pool": pool().snapshot()}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "migrate":
        done = migrate()
        print(f"Applied: {done}" if done else "Nothing to apply; schema is current.")
    elif command == "status":
        left = pending()
        print("Up to date." if not left else "Pending: " + ", ".join(f"V{m.version:03d} {m.name}" for m in left))
    else:
        print("usage: python db.py [migrate|status]")
        sys.exit(2)