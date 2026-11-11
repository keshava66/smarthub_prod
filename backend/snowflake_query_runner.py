from __future__ import annotations

import csv
import difflib
import io
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time as time_, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import db
from auth import require_permission

logger = logging.getLogger(__name__)

snowflake_router = APIRouter(prefix="/api/snowflake", tags=["Data Studio - Snowflake"])

# Wired in from app.py via set_snowflake_dependencies()
_config: Optional[Dict[str, Any]] = None
_fallback_config: Optional[Dict[str, Any]] = None

# Workspace (history / saved queries) lives in the portal database:
# smarthub_core.sf_query_history and sf_saved_queries.

# ============================================================================
# Config
# ============================================================================

DEFAULT_STATEMENT_TIMEOUT_SECONDS = 300      # per query unless the caller overrides
MAX_STATEMENT_TIMEOUT_SECONDS = 3600         # hard cap regardless of what's asked for
DEFAULT_MAX_CONCURRENT_JOBS = 6              # dedicated connections held at once
DEFAULT_JOB_TTL_SECONDS = 45 * 60            # jobs get GC'd after this
JOB_IDLE_RELEASE_SECONDS = 120               # a job nobody is paging gives its connection back
ROWS_PAGE_MAX_SIZE = 1000                    # on-screen paging cap
DOWNLOAD_CHUNK_ROWS = 10_000                 # rows per fetchmany while streaming a download
XLSX_MAX_ROWS = 1_048_575                    # Excel's own sheet limit, minus the header
META_CACHE_TTL_SECONDS = 300                 # explorer cache
HISTORY_KEEP_PER_USER = 500                  # trimmed on write


def _clean_cfg(value):
    """Trim whitespace and stray wrapping quotes off a config string.

    Config values get pasted by hand, and a leftover quote or trailing space in
    something like the account identifier turns into a %22 in the login URL and
    a DNS failure that reads like a network outage. Cheap to strip, miserable
    to debug otherwise."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in ("\"", "'"):
        text = text[1:-1].strip()
    return text.strip("\"'").strip()


def _cfg_value(key: str, env_var: str, default=None):
    if _config and _config.get(key) not in (None, ""):
        return _clean_cfg(_config[key])
    if os.environ.get(env_var):
        return _clean_cfg(os.environ[env_var])
    if _fallback_config and _fallback_config.get(key) not in (None, ""):
        return _clean_cfg(_fallback_config[key])
    return default


def _raw_cfg_value(key: str, env_var: str, default=None):
    """Same lookup as _cfg_value but byte-for-byte - secrets are allowed to
    start or end with a quote."""
    if _config and _config.get(key) not in (None, ""):
        return _config[key]
    if os.environ.get(env_var):
        return os.environ[env_var]
    if _fallback_config and _fallback_config.get(key) not in (None, ""):
        return _fallback_config[key]
    return default


def _configured() -> bool:
    return bool(_config or _fallback_config or os.environ.get("SFQR_ACCOUNT"))


# ============================================================================
# Credential profiles
#
# Most deployments run on one Snowflake login. Some can't: the role that can
# read the source schemas is not the role that can write to the target one.
# A write profile - the "sqrn_write" block inside snowflake_query_runner, or
# SFQRW_* env vars - covers that. Anything it leaves out falls back to the read
# profile, so the block can be as small as a different user and role.
#
# Nothing changes for a deployment without one: every connection is the read
# profile, which is the config that was always there.
# ============================================================================

READ_PROFILE = "read"
WRITE_PROFILE = "write"


# The block is "sqrn_write" in dq_config.json; "write" is still read so an
# existing config keeps working.
WRITE_SECTION_KEYS = ("sqrn_write", "write")


def _write_section() -> Optional[Dict[str, Any]]:
    if not _config:
        return None
    for key in WRITE_SECTION_KEYS:
        section = _config.get(key)
        if isinstance(section, dict) and section:
            return section
    return None


def _write_configured() -> bool:
    """True when writes are meant to go through a second login. Env vars alone
    are enough - SFQRW_USER with no config block is a valid setup."""
    if _write_section():
        return True
    return any(os.environ.get(var) for var in ("SFQRW_ACCOUNT", "SFQRW_USER", "SFQRW_ROLE"))


def _write_cfg_value(key: str, env_var: str, raw: bool = False):
    section = _write_section()
    if section and section.get(key) not in (None, ""):
        return section[key] if raw else _clean_cfg(section[key])
    if os.environ.get(env_var):
        return os.environ[env_var] if raw else _clean_cfg(os.environ[env_var])
    return None


def _require_config() -> Dict[str, Any]:
    if not _configured():
        raise HTTPException(
            status_code=503,
            detail="Data Studio (Snowflake) is not configured - add a 'snowflake_query_runner' section to dq_config.json",
        )
    cfg = {
        "account": _cfg_value("account", "SFQR_ACCOUNT"),
        "user": _cfg_value("user", "SFQR_USER"),
        "password": _raw_cfg_value("password", "SFQR_PASSWORD"),
        "private_key_path": _key_path(_cfg_value("private_key_path", "SFQR_PRIVATE_KEY_PATH")),
        "private_key_password": _raw_cfg_value("private_key_password", "SFQR_PRIVATE_KEY_PASSWORD", ""),
        "role": _cfg_value("role", "SFQR_ROLE"),
        "warehouse": _cfg_value("warehouse", "SFQR_WAREHOUSE"),
        "database": _cfg_value("database", "SFQR_DATABASE"),
        "schema": _cfg_value("schema", "SFQR_SCHEMA"),
        "statement_timeout_seconds": int(_cfg_value("statement_timeout_seconds", "SFQR_STATEMENT_TIMEOUT", DEFAULT_STATEMENT_TIMEOUT_SECONDS)),
        "max_statement_timeout_seconds": int(_cfg_value("max_statement_timeout_seconds", "SFQR_MAX_STATEMENT_TIMEOUT", MAX_STATEMENT_TIMEOUT_SECONDS)),
        "max_concurrent_jobs": int(_cfg_value("max_concurrent_jobs", "SFQR_MAX_CONCURRENT_JOBS", DEFAULT_MAX_CONCURRENT_JOBS)),
        "job_ttl_seconds": int(_cfg_value("job_ttl_seconds", "SFQR_JOB_TTL_SECONDS", DEFAULT_JOB_TTL_SECONDS)),
        "login_timeout": int(_cfg_value("login_timeout_seconds", "SFQR_LOGIN_TIMEOUT", 30)),
        # Cloning a whole schema or moving a large table is not a 5 minute job.
        "copy_timeout_seconds": int(_cfg_value("copy_timeout_seconds", "SFQR_COPY_TIMEOUT", COPY_STATEMENT_TIMEOUT_SECONDS)),
        # Turning this off makes the whole page read-only, no matter what the
        # caller's RBAC role says. Belt and braces for prod deployments.
        "allow_writes": _cfg_value("allow_writes", "SFQR_ALLOW_WRITES", True) not in (False, "false", "False", 0, "0"),
    }
    numbered = numbered_sections(_config)
    if numbered and not cfg["user"]:
        # Only numbered users: the first one is the default login (and the copier's read login).
        first = next(iter(numbered.values()))
        for key in ("account", "user", "role", "warehouse", "database", "schema"):
            if first.get(key) not in (None, ""):
                cfg[key] = _clean_cfg(first[key])
        key = first.get("private_key") or first.get("private_key_path")
        cfg["password"] = first.get("password")
        cfg["private_key_path"] = _key_path(_clean_cfg(key)) if key else None
        cfg["private_key_password"] = first.get("private_key_password") or cfg["private_key_password"]
        if not cfg["role"] and _as_list(first.get("roles")):
            cfg["role"] = _as_list(first.get("roles"))[0]
    if not cfg["account"] or not cfg["user"]:
        raise HTTPException(status_code=503, detail="Data Studio (Snowflake) config is missing 'account' or 'user'")
    if not cfg["password"] and not cfg["private_key_path"]:
        raise HTTPException(status_code=503, detail="Data Studio (Snowflake) config needs either 'password' or 'private_key_path'")
    return cfg


# ============================================================================
# Numbered connections (user_1, private_key_1, roles_1, warehouse_1, label_1...)
#
# Each number with a user is one login in the worksheet's connection dropdown.
# Keys without a number are shared. Postgres reads its numbered keys with the
# same parser. A worksheet's login and role travel as a profile string
# "c:<n>|<role>", which keeps them in every pool and cache key for free.
# ============================================================================

_NUMBERED_KEY = re.compile(r"^(.+?)_?(\d+)$")
CONN_PREFIX = "c:"


def numbered_sections(section: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """{"1": {"user": .., "password": ..}, "2": {...}} from user_1 / user1 style
    keys, in number order. Only numbers that have a user count."""
    found: Dict[str, Dict[str, Any]] = {}
    for key, value in (section or {}).items():
        m = _NUMBERED_KEY.match(str(key))
        if m and value not in (None, ""):
            found.setdefault(str(int(m.group(2))), {})[m.group(1)] = value
    return {n: found[n] for n in sorted(found, key=int) if found[n].get("user")}


def _as_list(value) -> List[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def _key_path(path: Optional[str]) -> Optional[str]:
    """A bare file name is looked up in private_key_path_base."""
    if not path:
        return path
    base = _cfg_value("private_key_path_base", "SFQR_PRIVATE_KEY_PATH_BASE")
    return str(Path(base) / path) if base and not Path(path).is_absolute() else path


def conn_profile(conn_id: Optional[str], role: Optional[str]) -> str:
    """Profile string for a worksheet's login and role. Validates both."""
    numbered = numbered_sections(_config)
    if conn_id in (None, "", "default"):
        if not numbered:
            return f"{CONN_PREFIX}default|{role}" if role else READ_PROFILE
        conn_id = next(iter(numbered))
    if conn_id not in numbered:
        raise HTTPException(status_code=400, detail=f"Unknown connection '{conn_id}'")
    allowed = _as_list(numbered[conn_id].get("roles"))
    if role and allowed and role.upper() not in {r.upper() for r in allowed}:
        raise HTTPException(status_code=400, detail=f"Role {role} is not in the roles configured for this connection")
    return f"{CONN_PREFIX}{conn_id}|{role or ''}"


def _numbered_config(conn_id: str, role: Optional[str]) -> Dict[str, Any]:
    cfg = dict(_require_config())
    n = numbered_sections(_config).get(conn_id)
    if n is None and conn_id != "default":
        raise HTTPException(status_code=400, detail=f"Unknown connection '{conn_id}'")
    if n:
        # Start from the shared (unnumbered) keys, not from whichever user filled the defaults.
        for key, env in (("account", "SFQR_ACCOUNT"), ("user", "SFQR_USER"), ("role", "SFQR_ROLE"),
                         ("warehouse", "SFQR_WAREHOUSE"), ("database", "SFQR_DATABASE"), ("schema", "SFQR_SCHEMA")):
            cfg[key] = _clean_cfg(n[key]) if n.get(key) not in (None, "") else _cfg_value(key, env)
        for key in ("statement_timeout_seconds", "max_statement_timeout_seconds"):
            if key in n:
                cfg[key] = int(n[key])
        if "allow_writes" in n:
            cfg["allow_writes"] = n["allow_writes"] not in (False, "false", "False", 0, "0")
        # Credentials as a set, like the write profile: a user with its own key
        # or password never borrows another user's. The passphrase can be shared.
        key = n.get("private_key") or n.get("private_key_path")
        if key or n.get("password"):
            cfg["password"] = n.get("password")
            cfg["private_key_path"] = _key_path(_clean_cfg(key)) if key else None
            cfg["private_key_password"] = n.get("private_key_password") or _raw_cfg_value(
                "private_key_password", "SFQR_PRIVATE_KEY_PASSWORD", "")
        cfg["label"] = n.get("label") or cfg["user"]
        cfg["roles"] = _as_list(n.get("roles"))
    else:
        cfg["label"] = (_config or {}).get("label") or cfg["user"]
        cfg["roles"] = _as_list((_config or {}).get("roles"))
    if role:
        cfg["role"] = role
    elif cfg["roles"] and (not n or not n.get("role")):
        cfg["role"] = cfg["roles"][0]
    if not cfg["account"]:
        raise HTTPException(status_code=503, detail=f"Connection {conn_id} has no 'account'")
    cfg["conn_id"] = conn_id
    return cfg


def connections() -> List[Dict[str, Any]]:
    """What the connection dropdown lists. Never includes secrets."""
    out = []
    for cid in list(numbered_sections(_config)) or ["default"]:
        c = _numbered_config(cid, None)
        out.append({"id": cid, "label": c["label"], "user": c["user"], "account": c["account"],
                    "role": c["role"], "roles": c["roles"], "warehouse": c["warehouse"],
                    "default_database": c["database"], "default_schema": c["schema"],
                    "allow_writes": c["allow_writes"]})
    return out


def _profile_config(profile: str = READ_PROFILE) -> Dict[str, Any]:
    """The connection settings for one profile. The write profile is the read
    config with its own keys laid over the top. A "c:<n>|<role>" profile is a
    numbered login, optionally switched to another role."""
    if profile and profile.startswith(CONN_PREFIX):
        cid, _, role = profile[len(CONN_PREFIX):].partition("|")
        return _numbered_config(cid, role or None)
    cfg = _require_config()
    if profile != WRITE_PROFILE or not _write_configured():
        return cfg

    write = dict(cfg)
    for key, env_var in (
        ("account", "SFQRW_ACCOUNT"),
        ("user", "SFQRW_USER"),
        ("role", "SFQRW_ROLE"),
        ("warehouse", "SFQRW_WAREHOUSE"),
        ("database", "SFQRW_DATABASE"),
        ("schema", "SFQRW_SCHEMA"),
        ("private_key_path", "SFQRW_PRIVATE_KEY_PATH"),
    ):
        value = _write_cfg_value(key, env_var)
        if value not in (None, ""):
            write[key] = value

    # Credentials are taken as a set: a write profile that names a password or
    # a key file uses only its own, so a half-filled block can never end up
    # pairing the read user with the write password.
    password = _write_cfg_value("password", "SFQRW_PASSWORD", raw=True)
    key_path = _write_cfg_value("private_key_path", "SFQRW_PRIVATE_KEY_PATH")
    key_password = _write_cfg_value("private_key_password", "SFQRW_PRIVATE_KEY_PASSWORD", raw=True)
    if password or key_path:
        write["password"] = password
        write["private_key_path"] = key_path
        write["private_key_password"] = key_password or ""

    if not write["password"] and not write["private_key_path"]:
        raise HTTPException(
            status_code=503,
            detail="The Snowflake write profile needs either 'password' or 'private_key_path'",
        )
    return write


def _profile_label(profile: str) -> str:
    cfg = _profile_config(profile)
    return f"{cfg['user']}" + (f"/{cfg['role']}" if cfg.get("role") else "")


def set_snowflake_dependencies(config: Optional[Dict[str, Any]], fallback: Optional[Dict[str, Any]] = None):
    """Called once from app.py. `config` is the 'snowflake_query_runner' section;
    `fallback` is the main 'snowflake' section, used for any key the runner
    section leaves out so a minimal config still works."""
    global _config, _fallback_config
    _config = config
    _fallback_config = fallback
    if not _configured():
        logger.warning("⚠️ snowflake_query_runner config not found - Data Studio Snowflake endpoints will be unavailable")
    else:
        _init_db()
        _pool.warm()


# ============================================================================
# Connections
# ============================================================================

_private_key_cache: Dict[str, bytes] = {}
_private_key_lock = threading.Lock()


def _load_private_key(path: str, password: str) -> bytes:
    """DER-encoded PKCS8 bytes, cached by (path, mtime) so we're not re-reading
    and re-deriving the key on every single connection."""
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization

    key_file = Path(path)
    if not key_file.exists():
        raise HTTPException(status_code=503, detail=f"Snowflake private key not found at {path}")
    cache_key = f"{path}:{key_file.stat().st_mtime_ns}"
    with _private_key_lock:
        cached = _private_key_cache.get(cache_key)
    if cached is not None:
        return cached
    try:
        with open(key_file, "rb") as f:
            p_key = serialization.load_pem_private_key(
                f.read(),
                password=password.encode() if password else None,
                backend=default_backend(),
            )
        der = p_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not load Snowflake private key: {e}")
    with _private_key_lock:
        _private_key_cache.clear()
        _private_key_cache[cache_key] = der
    return der


def _connect(database: Optional[str] = None, schema: Optional[str] = None, warehouse: Optional[str] = None,
             profile: str = READ_PROFILE):
    """Fresh Snowflake connection. Imported lazily so the module still imports
    in environments without the connector installed."""
    import snowflake.connector

    cfg = _profile_config(profile)
    kwargs: Dict[str, Any] = {
        "account": cfg["account"],
        "user": cfg["user"],
        "login_timeout": cfg["login_timeout"],
        # No network_timeout on purpose. It is not a login setting: the
        # connector applies it to every statement and cancels the query from
        # the client side when it elapses - which is how a 30 second login
        # timeout turned into "SQL execution was cancelled by the client due to
        # a timeout" on any query that ran longer than half a minute. How long
        # a statement may run is Snowflake's business, set per session with
        # STATEMENT_TIMEOUT_IN_SECONDS below.
        "application": "SmartHub_SnowflakeQueryRunner",
        "client_session_keep_alive": True,
        # We only ever bind values, never identifiers, and Snowflake's
        # client-side binding is what makes the value/identifier split real.
        "paramstyle": "qmark",
    }
    if cfg["private_key_path"]:
        kwargs["private_key"] = _load_private_key(cfg["private_key_path"], cfg["private_key_password"] or "")
    else:
        kwargs["password"] = cfg["password"]
    for key, val in (
        ("role", cfg["role"]),
        ("warehouse", warehouse or cfg["warehouse"]),
        ("database", database or cfg["database"]),
        ("schema", schema or cfg["schema"]),
    ):
        if val:
            kwargs[key] = val
    try:
        return snowflake.connector.connect(**kwargs)
    except HTTPException:
        raise
    except Exception as e:
        text = str(e)
        if "getaddrinfo" in text or "NameResolutionError" in text or "Failed to resolve" in text:
            raise HTTPException(
                status_code=502,
                detail=(
                    f"Snowflake host {cfg['account']}.snowflakecomputing.com could not be resolved. "
                    "That is almost always a malformed 'account' in the config (a stray quote, a full "
                    "URL instead of the identifier, or the wrong region suffix) rather than a network fault."
                ),
            )
        raise HTTPException(status_code=502, detail=f"Could not connect to Snowflake: {e}")


def _apply_statement_timeout(conn, cur, seconds: int):
    """Set STATEMENT_TIMEOUT_IN_SECONDS for this session.

    Pooled sessions remember what they were last set to, so the extra round
    trip only happens when the value actually changes."""
    wanted = max(1, int(seconds))
    if getattr(conn, "_sfqr_timeout", None) == wanted:
        return
    try:
        cur.execute(f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {wanted}")
        try:
            conn._sfqr_timeout = wanted
        except Exception:
            pass
    except Exception as e:
        logger.info("Could not set STATEMENT_TIMEOUT_IN_SECONDS: %s", e)


def _quiet_close(conn):
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


_STALE_MARKERS = (
    "connection is closed", "not connected", "connection was closed", "broken pipe",
    "closed database", "closed connection", "already closed",
    "connection reset", "connection aborted", "eof occurred", "session no longer exists",
    "authentication token has expired", "session expired", "socket", "timed out",
)


def _looks_stale(e: Exception) -> bool:
    text = str(e).lower()
    return any(marker in text for marker in _STALE_MARKERS)


class _ConnectionPool:
    """Warm Snowflake connections, reused across requests.

    A Snowflake login is the single most expensive thing this module does -
    roughly a second of TLS plus authentication, which used to be paid on every
    query, every page of a table and every explorer click. Handing out an
    already-authenticated session instead is what makes the page feel instant.

    Connections are keyed by (database, schema, warehouse) because those are set
    at login. Idle ones are retired well before Snowflake's own session timeout
    so a checkout never hands back a dead socket.
    """

    MAX_AGE = 55 * 60          # retire before Snowflake's default session timeout
    MAX_IDLE_PER_KEY = 4       # per (database, schema, warehouse)

    def __init__(self):
        self._idle: Dict[tuple, List[tuple]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(database, schema, warehouse, profile=READ_PROFILE):
        # The profile is part of the key: two logins are two different sets of
        # sessions, and handing a write session to a read caller (or the other
        # way round) would silently run it as the wrong role.
        return (profile or READ_PROFILE, database or "", schema or "", warehouse or "")

    def acquire(self, database=None, schema=None, warehouse=None, profile=READ_PROFILE):
        key = self._key(database, schema, warehouse, profile)
        while True:
            with self._lock:
                bucket = self._idle.get(key)
                if not bucket:
                    break
                conn, created = bucket.pop()
            if time.time() - created > self.MAX_AGE:
                _quiet_close(conn)
                continue
            return conn, key, created
        return _connect(database, schema, warehouse, profile), key, time.time()

    def release(self, conn, key, created):
        if conn is None:
            return
        if time.time() - created > self.MAX_AGE:
            _quiet_close(conn)
            return
        with self._lock:
            bucket = self._idle.setdefault(key, [])
            if len(bucket) >= self.MAX_IDLE_PER_KEY:
                spare = True
            else:
                bucket.append((conn, created))
                spare = False
        if spare:
            _quiet_close(conn)

    def warm(self, count=2):
        """Open a couple of connections in the background at startup so the
        first person to hit the page doesn't pay for the login."""
        def _open():
            try:
                conn, key, created = self.acquire()
                self.release(conn, key, created)
            except Exception as e:
                logger.info("Snowflake pool warm-up skipped: %s", e)
        for _ in range(count):
            threading.Thread(target=_open, daemon=True).start()

    def close_all(self):
        with self._lock:
            buckets = list(self._idle.values())
            self._idle.clear()
        for bucket in buckets:
            for conn, _created in bucket:
                _quiet_close(conn)


_pool = _ConnectionPool()


@contextmanager
def _pooled(database=None, schema=None, warehouse=None, profile=READ_PROFILE):
    """Check a connection out of the pool for the duration of the block. A
    connection that errors is dropped rather than handed to the next caller."""
    conn, key, created = _pool.acquire(database, schema, warehouse, profile)
    ok = True
    try:
        yield conn
    except Exception as e:
        ok = not _looks_stale(e)
        raise
    finally:
        if ok:
            _pool.release(conn, key, created)
        else:
            _quiet_close(conn)


def _run_meta(fn, profile: str = READ_PROFILE):
    """Run a catalog query on a pooled connection, retrying once on a stale
    session. Metadata calls no longer share one serialized connection, so the
    explorer tree can expand several nodes in parallel."""
    for attempt in (0, 1):
        conn, key, created = _pool.acquire(profile=profile)
        cur = None
        try:
            cur = conn.cursor()
            result = fn(cur)
            cur.close()
            _pool.release(conn, key, created)
            return result
        except Exception as e:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            if attempt == 0 and _looks_stale(e):
                _quiet_close(conn)
                continue
            if _looks_stale(e):
                _quiet_close(conn)
            else:
                _pool.release(conn, key, created)
            raise


# ============================================================================
# Tiny in-process TTL cache for explorer metadata.
# ============================================================================

_cache: Dict[str, tuple] = {}
_cache_lock = threading.Lock()


def _cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ts, payload = entry
        if time.time() - ts > META_CACHE_TTL_SECONDS:
            return None
        return payload


def _cache_set(key: str, payload: Any):
    with _cache_lock:
        _cache[key] = (time.time(), payload)
        if len(_cache) > 2000:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)


def _cache_drop_prefix(prefix: str):
    with _cache_lock:
        for key in [k for k in _cache if k.startswith(prefix)]:
            _cache.pop(key, None)


def _drop_table_cache(database: str, schema: str):
    """A schema that just gained a table is stale under every profile that can
    see it, not only the one that wrote it."""
    for profile in (READ_PROFILE, WRITE_PROFILE):
        _cache_drop_prefix(f"sf-tables:{profile}:{database}:{schema}")


# ============================================================================
# Identifiers and values
# ============================================================================

def quote_ident(name: str) -> str:
    """Double-quote a Snowflake identifier, escaping any embedded quotes. Every
    identifier that reaches SQL in this module goes through here - identifiers
    can't be bound as parameters, so this is the only thing standing between a
    table name and an injection."""
    if name is None:
        raise HTTPException(status_code=400, detail="Empty identifier")
    text = str(name)
    if not text or "\x00" in text:
        raise HTTPException(status_code=400, detail="Invalid identifier")
    if len(text) > 255:
        raise HTTPException(status_code=400, detail="Identifier is too long")
    return '"' + text.replace('"', '""') + '"'


def qualified(database: Optional[str], schema: Optional[str], table: str) -> str:
    parts = []
    if database:
        parts.append(quote_ident(database))
    if schema:
        parts.append(quote_ident(schema))
    parts.append(quote_ident(table))
    return ".".join(parts)


def _to_json_safe(val):
    """Coerce Snowflake result values into something json-encodable. Numeric and
    temporal precision is preserved as text rather than float - a browser
    round-tripping a NUMBER(38,10) through a JS number would silently corrupt
    it."""
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    if isinstance(val, Decimal):
        return str(val)
    if isinstance(val, (datetime, date, time_)):
        return val.isoformat()
    if isinstance(val, timedelta):
        return str(val)
    if isinstance(val, (bytes, bytearray, memoryview)):
        return bytes(val).hex().upper()
    if isinstance(val, float):
        if val != val or val in (float("inf"), float("-inf")):
            return None
        return val
    if isinstance(val, (list, tuple)):
        return [_to_json_safe(v) for v in val]
    if isinstance(val, dict):
        return {k: _to_json_safe(v) for k, v in val.items()}
    if isinstance(val, (int, str)):
        return val
    try:
        return str(val)
    except Exception:
        return None


def _flatten_for_text(val) -> str:
    """One-line text form used by the CSV/Excel writers."""
    safe = _to_json_safe(val)
    if safe is None:
        return ""
    if isinstance(safe, (dict, list)):
        return json.dumps(safe, separators=(",", ":"))
    return str(safe)


# ============================================================================
# SQL validation / classification
# ----------------------------------------------------------------------------
# Full Snowflake SQL is allowed - this is NOT a read-only runner. Two rules are
# enforced instead: one statement per run (so stacked-statement injection is
# never possible), and anything that mutates data or schema needs an explicit
# `confirm: true` from the caller, with a louder warning when an UPDATE/DELETE
# has no WHERE clause.
# ============================================================================

_ALWAYS_BLOCKED = [
    # Credential and account surface that has no business being driven from a
    # web query box, confirmed or not.
    (r"\bALTER\s+ACCOUNT\b", "ALTER ACCOUNT is not allowed from Data Studio"),
    (r"\bCREATE\s+(OR\s+REPLACE\s+)?(SECURITY\s+INTEGRATION|STORAGE\s+INTEGRATION|API\s+INTEGRATION|NOTIFICATION\s+INTEGRATION)\b",
     "Integration objects cannot be created from Data Studio"),
    (r"\bALTER\s+(SECURITY|STORAGE|API|NOTIFICATION)\s+INTEGRATION\b",
     "Integration objects cannot be altered from Data Studio"),
    (r"\bCREATE\s+(OR\s+REPLACE\s+)?USER\b", "User management is not allowed from Data Studio"),
    (r"\b(ALTER|DROP)\s+USER\b", "User management is not allowed from Data Studio"),
    (r"\bSYSTEM\$GET_PRIVATELINK", "This system function is not allowed from Data Studio"),
]

_DESTRUCTIVE_LEAD_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER",
    "CREATE", "GRANT", "REVOKE", "CALL", "COMMENT", "COPY", "PUT", "GET",
    "REMOVE", "UNDROP", "EXECUTE", "BEGIN", "COMMIT", "ROLLBACK",
}
# USE / SET / UNSET / ALTER SESSION only change this run's session, so they run
# without a confirm. That's what lets a script start with USE WAREHOUSE.

_DQL_LEAD_KEYWORDS = {"SELECT", "WITH", "SHOW", "DESC", "DESCRIBE", "LIST", "LS", "VALUES", "TABLE"}


def _strip_sql_comments(sql: str) -> str:
    """Removes '--' and /* */ comments without touching string literals,
    quoted identifiers, or $$-quoted bodies (used by procedures and UDFs).
    Newlines inside comments are preserved so error line numbers still line up."""
    out = []
    i, n = 0, len(sql)
    in_sq = in_dq = in_dollar = False
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_dollar:
            if ch == "$" and nxt == "$":
                out.append("$$")
                i += 2
                in_dollar = False
                continue
            out.append(ch)
            i += 1
            continue
        if in_sq:
            out.append(ch)
            if ch == "\\" and nxt:
                out.append(nxt)
                i += 2
                continue
            if ch == "'":
                if nxt == "'":
                    out.append(nxt)
                    i += 2
                    continue
                in_sq = False
            i += 1
            continue
        if in_dq:
            out.append(ch)
            if ch == '"':
                if nxt == '"':
                    out.append(nxt)
                    i += 2
                    continue
                in_dq = False
            i += 1
            continue
        if ch == "$" and nxt == "$":
            in_dollar = True
            out.append("$$")
            i += 2
            continue
        if ch == "'":
            in_sq = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dq = True
            out.append(ch)
            i += 1
            continue
        if ch == "-" and nxt == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            i += 2
            depth = 1
            while i < n and depth > 0:
                if sql[i] == "/" and sql[i + 1: i + 2] == "*":
                    depth += 1
                    i += 2
                    continue
                if sql[i] == "*" and sql[i + 1: i + 2] == "/":
                    depth -= 1
                    i += 2
                    continue
                if sql[i] == "\n":
                    out.append("\n")
                i += 1
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_statements(sql: str) -> List[str]:
    """Splits on ';' that sit outside strings, quoted identifiers and $$ bodies.
    Expects text that has already been through _strip_sql_comments."""
    stmts, cur = [], []
    in_sq = in_dq = in_dollar = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_dollar:
            cur.append(ch)
            if ch == "$" and nxt == "$":
                cur.append(nxt)
                i += 2
                in_dollar = False
                continue
            i += 1
            continue
        if in_sq:
            cur.append(ch)
            if ch == "\\" and nxt:
                cur.append(nxt)
                i += 2
                continue
            if ch == "'":
                if nxt == "'":
                    cur.append(nxt)
                    i += 2
                    continue
                in_sq = False
            i += 1
            continue
        if in_dq:
            cur.append(ch)
            if ch == '"':
                if nxt == '"':
                    cur.append(nxt)
                    i += 2
                    continue
                in_dq = False
            i += 1
            continue
        if ch == "$" and nxt == "$":
            in_dollar = True
            cur.append("$$")
            i += 2
            continue
        if ch == "'":
            in_sq = True
            cur.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dq = True
            cur.append(ch)
            i += 1
            continue
        if ch == ";":
            stmts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    stmts.append("".join(cur))
    return [s.strip() for s in stmts if s.strip()]


def _depth_zero_has(sql: str, pattern: str) -> bool:
    """True if `pattern` matches at parenthesis depth 0 - i.e. in the outer
    statement rather than inside a subquery."""
    depth = 0
    in_sq = in_dq = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_sq:
            if ch == "\\" and nxt:
                i += 2
                continue
            if ch == "'":
                if nxt == "'":
                    i += 2
                    continue
                in_sq = False
            i += 1
            continue
        if in_dq:
            if ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == "'":
            in_sq = True
            i += 1
            continue
        if ch == '"':
            in_dq = True
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and re.match(pattern, sql[i:], re.IGNORECASE):
            return True
        i += 1
    return False


def _lead_after_cte(sql: str) -> str:
    """For `WITH x AS (...) DELETE ...`, find the real command after the CTE
    list so the statement is classified on what it actually does."""
    depth = 0
    in_sq = in_dq = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if in_sq:
            if ch == "\\" and nxt:
                i += 2
                continue
            if ch == "'":
                if nxt == "'":
                    i += 2
                    continue
                in_sq = False
            i += 1
            continue
        if in_dq:
            if ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == "'":
            in_sq = True
            i += 1
            continue
        if ch == '"':
            in_dq = True
            i += 1
            continue
        if ch == "(":
            depth += 1
            i += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0 and i > 0:
            m = re.match(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE)\b", sql[i:], re.IGNORECASE)
            if m and not re.match(r"[A-Za-z0-9_$]", sql[i - 1]):
                return m.group(1).upper()
        i += 1
    return "SELECT"


@dataclass
class SqlClassification:
    cleaned_sql: str
    lead_keyword: str
    is_destructive: bool
    returns_rows: bool
    warning: Optional[str] = None


def classify_and_validate_sql(sql: str) -> SqlClassification:
    """Validates a single well-formed statement and classifies it. Raises 400 for
    anything structurally invalid (empty, multiple statements, blocked)."""
    if sql is None:
        raise HTTPException(status_code=400, detail="Query is empty")
    stripped = _strip_sql_comments(sql).strip()
    if not stripped:
        raise HTTPException(status_code=400, detail="Query is empty")

    statements = split_statements(stripped)
    if not statements:
        raise HTTPException(status_code=400, detail="Query is empty")
    if len(statements) > 1:
        raise HTTPException(status_code=400, detail="Only a single statement is allowed per run - highlight one and run the selection")

    for pattern, message in _ALWAYS_BLOCKED:
        if re.search(pattern, stripped, re.IGNORECASE):
            raise HTTPException(status_code=400, detail=message)

    # Execute the user's original text (comments and formatting intact); only
    # the trailing semicolon comes off.
    cleaned = sql.strip()
    while cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()

    m = re.match(r"^\s*([A-Za-z_]+)", stripped)
    lead = m.group(1).upper() if m else ""
    effective = lead

    if lead == "WITH":
        effective = _lead_after_cte(stripped)
    elif lead == "EXPLAIN":
        # Snowflake's EXPLAIN never runs the statement, so it's always safe.
        effective = "EXPLAIN"
    elif lead == "ALTER" and re.match(r"^\s*ALTER\s+SESSION\b", stripped, re.IGNORECASE):
        effective = "ALTERSESSION"

    is_destructive = effective in _DESTRUCTIVE_LEAD_KEYWORDS
    returns_rows = effective in _DQL_LEAD_KEYWORDS or effective == "EXPLAIN"

    warning = None
    if effective in ("UPDATE", "DELETE"):
        if not _depth_zero_has(stripped, r"\bWHERE\b"):
            warning = f"This {effective} has no WHERE clause and will affect every row in the target table."
    elif effective == "TRUNCATE":
        warning = "TRUNCATE removes every row in the target table and cannot be filtered."
    elif effective == "DROP":
        warning = "DROP permanently removes the object. Snowflake's UNDROP can recover it only within the object's Time Travel window."
    elif effective == "ALTER" and re.search(r"\bDROP\s+COLUMN\b", stripped, re.IGNORECASE):
        warning = "This ALTER drops a column, which permanently discards its data."
    elif effective == "CREATE" and re.search(r"\bCREATE\s+OR\s+REPLACE\b", stripped, re.IGNORECASE):
        warning = "CREATE OR REPLACE overwrites the existing object and drops its data."
    elif effective in ("USE", "SET", "UNSET", "ALTERSESSION", "BEGIN", "COMMIT", "ROLLBACK"):
        # Every run gets its own Snowflake session, so session-scoped state set
        # here is gone by the next statement. Worth saying out loud - it's a
        # classic "why didn't my USE take effect" moment.
        warning = "Each run uses its own Snowflake session, so this lasts until the end of this run only. Put it in the same run as the statements that need it, or fully qualify your objects."

    return SqlClassification(
        cleaned_sql=cleaned,
        lead_keyword=effective,
        is_destructive=is_destructive,
        returns_rows=returns_rows,
        warning=warning,
    )


MAX_STATEMENTS_PER_RUN = 100
_DOLLAR_TAG = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$")


def split_script(sql: str, backslash_escapes: bool = True) -> List[tuple]:
    """A worksheet split into statements, as (text, lines before it). Semicolons
    inside strings, quoted names, comments and $$ / $tag$ bodies don't split.
    Comment-only pieces are dropped."""
    spans, start, i, n = [], 0, 0, len(sql)
    while i < n:
        ch, nxt = sql[i], sql[i + 1: i + 2]
        if ch == "'":
            i += 1
            while i < n:
                if backslash_escapes and sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    if sql[i + 1: i + 2] == "'":
                        i += 2
                        continue
                    break
                i += 1
            i += 1
        elif ch == '"':
            j = sql.find('"', i + 1)
            i = n if j < 0 else j + 1
        elif ch == "-" and nxt == "-":
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif ch == "/" and nxt == "*":
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
        elif ch == "$" and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] in "_$")) and (m := _DOLLAR_TAG.match(sql, i)):
            j = sql.find(m.group(0), m.end())
            i = n if j < 0 else j + len(m.group(0))
        elif ch == ";":
            spans.append((start, i))
            start = i = i + 1
        else:
            i += 1
    spans.append((start, n))
    out = []
    for a, b in spans:
        text = sql[a:b]
        if not _strip_sql_comments(text).strip():
            continue
        first = a + len(text) - len(text.lstrip())
        out.append((text.strip(), sql.count("\n", 0, first)))
    return out


def plan_statements(sql: str, classify=None, backslash_escapes: bool = True) -> List[Dict[str, Any]]:
    """Split a run into statements and classify each one. Shared with the
    Postgres pane, which passes its own classifier."""
    classify = classify or classify_and_validate_sql
    parts = split_script(sql or "", backslash_escapes)
    if not parts:
        raise HTTPException(status_code=400, detail="Query is empty")
    if len(parts) > MAX_STATEMENTS_PER_RUN:
        raise HTTPException(status_code=400, detail=f"A run can have at most {MAX_STATEMENTS_PER_RUN} statements")
    planned = []
    for k, (text, line) in enumerate(parts):
        try:
            c = classify(text)
        except HTTPException as e:
            if len(parts) == 1:
                raise
            raise HTTPException(status_code=e.status_code, detail=f"Statement {k + 1} (line {line + 1}): {e.detail}")
        planned.append({"sql": c.cleaned_sql, "cls": c, "line": line})
    return planned


def confirm_detail(planned: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The 409 body asking the user to confirm every statement that writes."""
    risky = [p for p in planned if p["cls"].is_destructive]
    multi = len(planned) > 1
    warnings = [
        (f"Statement {planned.index(p) + 1}: " if multi else "") + p["cls"].warning
        for p in risky if p["cls"].warning
    ]
    if len(risky) == 1:
        kind = risky[0]["cls"].lead_keyword
        a = "an" if kind[:1] in "AEIOU" else "a"
        message = f"This run includes {a} {kind} statement that will modify data or schema." if multi else \
            f"This is {a} {kind} statement and will modify data or schema."
    else:
        kind = ", ".join(dict.fromkeys(p["cls"].lead_keyword for p in risky))
        message = f"{len(risky)} statements in this run will modify data or schema ({kind})."
    return {
        "message": message,
        "requires_confirmation": True,
        "statement_type": kind,
        "count": len(risky),
        "warning": "\n".join(warnings) or None,
    }


def failed_at(message: str, planned: List[Dict[str, Any]], k: int) -> str:
    """Error text for statement k of a multi-statement run. The footer is what
    the editor reads to jump to the right line."""
    if len(planned) <= 1:
        return message
    return f"{message}\n\nStatement {k + 1} of {len(planned)}, starting on line {planned[k]['line'] + 1}."


def _statement_result(st: Dict[str, Any], k: int, started: float, status: str, **extra) -> Dict[str, Any]:
    """One line of the per-statement summary shown in the Details tab."""
    return {
        "index": k + 1,
        "line": st["line"] + 1,
        "statement_type": st["cls"].lead_keyword if st.get("cls") else None,
        "status": status,
        "elapsed_ms": int((time.time() - started) * 1000),
        "sql": st["sql"][:300],
        **extra,
    }


def _guard_writes_allowed(classification: SqlClassification):
    cfg = _require_config()
    if classification.is_destructive and not cfg["allow_writes"]:
        raise HTTPException(
            status_code=403,
            detail="This deployment's Data Studio (Snowflake) is configured read-only (allow_writes = false)",
        )


# ============================================================================
# Result-set filtering, shared by query results and the table data editor
# ============================================================================

TEXT_OPS = {"contains", "not_contains", "starts", "ends"}
NULL_OPS = {"is_null", "is_not_null"}
COMPARE_OPS = {"eq", "neq", "gt", "gte", "lt", "lte"}
SET_OPS = {"in", "not_in"}
ALL_OPS = TEXT_OPS | NULL_OPS | COMPARE_OPS | SET_OPS


class ColumnFilter(BaseModel):
    col: str
    op: str
    value: Optional[Any] = None
    values: Optional[List[Any]] = None


def build_where(filters: Optional[List[ColumnFilter]], valid_columns: Optional[List[str]] = None):
    """Turns UI filters into a WHERE fragment plus its bound parameters. Column
    names are validated against the result's own columns and then quoted;
    values are always bound, never interpolated."""
    if not filters:
        return "", []
    valid_set = set(valid_columns) if valid_columns else None
    clauses: List[str] = []
    params: List[Any] = []
    for f in filters:
        op = (f.op or "").lower()
        if op not in ALL_OPS:
            raise HTTPException(status_code=400, detail=f"Unsupported filter operator '{f.op}'")
        if valid_set is not None and f.col not in valid_set:
            raise HTTPException(status_code=400, detail=f"'{f.col}' is not a column of this result")
        col = quote_ident(f.col)
        if op == "is_null":
            clauses.append(f"{col} IS NULL")
        elif op == "is_not_null":
            clauses.append(f"{col} IS NOT NULL")
        elif op in TEXT_OPS:
            text = "" if f.value is None else str(f.value)
            # ESCAPE '\' so a literal % or _ typed in the filter box stays literal.
            pattern = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            if op == "contains":
                clauses.append(f"TO_VARCHAR({col}) ILIKE ? ESCAPE '\\\\'")
                params.append(f"%{pattern}%")
            elif op == "not_contains":
                clauses.append(f"(TO_VARCHAR({col}) NOT ILIKE ? ESCAPE '\\\\' OR {col} IS NULL)")
                params.append(f"%{pattern}%")
            elif op == "starts":
                clauses.append(f"TO_VARCHAR({col}) ILIKE ? ESCAPE '\\\\'")
                params.append(f"{pattern}%")
            else:
                clauses.append(f"TO_VARCHAR({col}) ILIKE ? ESCAPE '\\\\'")
                params.append(f"%{pattern}")
        elif op in SET_OPS:
            values = f.values if f.values is not None else ([f.value] if f.value is not None else [])
            if not values:
                raise HTTPException(status_code=400, detail=f"Filter on '{f.col}' needs at least one value")
            if len(values) > 1000:
                raise HTTPException(status_code=400, detail="An IN filter is limited to 1000 values")
            placeholders = ", ".join("?" for _ in values)
            negate = "NOT " if op == "not_in" else ""
            clauses.append(f"TO_VARCHAR({col}) {negate}IN ({placeholders})")
            params.extend("" if v is None else str(v) for v in values)
        else:
            sql_op = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[op]
            if op == "eq" and f.value is None:
                clauses.append(f"{col} IS NULL")
                continue
            if op == "neq" and f.value is None:
                clauses.append(f"{col} IS NOT NULL")
                continue
            # Compared as text so the same filter works against any column type
            # without the caller having to know the column's declared type.
            clauses.append(f"TO_VARCHAR({col}) {sql_op} ?")
            params.append("" if f.value is None else str(f.value))
    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


def build_order_by(sort: Optional[List[Dict[str, str]]], valid_columns: Optional[List[str]] = None) -> str:
    if not sort:
        return ""
    valid_set = set(valid_columns) if valid_columns else None
    parts = []
    for entry in sort:
        col = entry.get("col")
        direction = (entry.get("dir") or "asc").lower()
        if direction not in ("asc", "desc"):
            raise HTTPException(status_code=400, detail="Sort direction must be 'asc' or 'desc'")
        if not col:
            continue
        if valid_set is not None and col not in valid_set:
            raise HTTPException(status_code=400, detail=f"'{col}' is not a column of this result")
        parts.append(f"{quote_ident(col)} {'ASC' if direction == 'asc' else 'DESC'} NULLS LAST")
    return (" ORDER BY " + ", ".join(parts)) if parts else ""


# ============================================================================
# Job registry
# ============================================================================

_executor = ThreadPoolExecutor(max_workers=32, thread_name_prefix="sfqr")
_job_slots = threading.Semaphore(DEFAULT_MAX_CONCURRENT_JOBS)
_job_slots_size = DEFAULT_MAX_CONCURRENT_JOBS
_job_slots_lock = threading.Lock()

_jobs: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


class _Cancelled(Exception):
    pass

COPY_CHUNK_TARGET_ROWS = 2_000_000      # aim for roughly this many rows per INSERT chunk
COPY_INSERT_CHUNK_THRESHOLD = 750_000   # below this, one INSERT beats splitting it up
MAX_CHUNKS_PER_TABLE = 8                # cap on how many chunks one table gets split into
MAX_PARALLEL_COPY_UNITS = 12            # global cap on concurrent chunks/clones for one job
MAX_TABLES_PER_SCHEMA_COPY = 200        # a schema copy bigger than this needs to be split by hand
COPY_JOB_TTL_SECONDS = 2 * 60 * 60      # copy jobs stick around longer than query jobs
COPY_STATEMENT_TIMEOUT_SECONDS = 4 * 60 * 60   # a copy statement may run this long before Snowflake stops it

# Copy units run in phases. Everything that reads from the source is phase 1 and
# runs in parallel; anything that reads from the target - a recreated view -
# waits for phase 1 to finish, whatever kind of copy job it belongs to.
COPY_PHASE_DATA = 1
COPY_PHASE_VIEWS = 2
TRANSFER_BATCH_ROWS = 10_000            # rows per round trip when a copy has to go through the API
TRANSFER_MAX_BINDS = 16_000             # Snowflake's own ceiling on bind variables in one statement
TRANSFER_MAX_ROWS = 5_000_000           # refuse a through-the-API copy bigger than this

_copy_jobs: Dict[str, Dict[str, Any]] = {}
_copy_jobs_lock = threading.Lock()
_copy_chunk_slots = threading.Semaphore(MAX_PARALLEL_COPY_UNITS)


def _resize_job_slots(new_size: int):
    global _job_slots, _job_slots_size
    with _job_slots_lock:
        if new_size != _job_slots_size:
            _job_slots = threading.Semaphore(new_size)
            _job_slots_size = new_size


def _new_job(classification: SqlClassification, database: Optional[str], schema: Optional[str],
             warehouse: Optional[str], timeout_seconds: int, owner: str) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "owner": (owner or "").strip().lower(),
        "sql": classification.cleaned_sql,
        "database": database,
        "schema": schema,
        "warehouse": warehouse,
        "lead_keyword": classification.lead_keyword,
        "is_destructive": classification.is_destructive,
        "returns_rows": classification.returns_rows,
        "timeout_seconds": timeout_seconds,
        "status": "running",           # running | success | failed | cancelled
        "conn": None,
        "pool_key": None,             # so the connection goes back to the pool, not the bin
        "pool_created": 0.0,
        "done": threading.Event(),    # lets a caller wait for a fast query inline
        "cursor": None,               # live cursor while running, for cancel()
        "query_id": None,
        "columns": None,               # [{name, type}]
        "row_count": None,
        "rows_affected": None,
        "error": None,
        "created_at": time.time(),
        "last_used": time.time(),
        "started_at": time.time(),
        "finished_at": None,
        "job_lock": threading.Lock(),
        "cancel_requested": False,
        # Materialized results, used when RESULT_SCAN can't be (duplicate
        # column names, SHOW commands, expired result cache).
        "inline_rows": None,
        "profile": READ_PROFILE,
    }
    with _jobs_lock:
        _jobs[job_id] = job
    return job


def _touch(job: Dict[str, Any]) -> Dict[str, Any]:
    job["last_used"] = time.time()
    return job


def _get_job(job_id: str, owner: Optional[str] = None) -> Dict[str, Any]:
    with _jobs_lock:
        job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown or expired query job")
    # Jobs hold a live Snowflake connection and a result set; only the person
    # who started one can read it back.
    if owner is not None and job["owner"] and job["owner"] != (owner or "").strip().lower():
        raise HTTPException(status_code=404, detail="Unknown or expired query job")
    return _touch(job)


def _close_job(job: Dict[str, Any]):
    """Hand the job's connection back to the pool so the next query reuses the
    warm session instead of logging in again."""
    conn = job.get("conn")
    if conn is not None:
        _pool.release(conn, job.get("pool_key") or _ConnectionPool._key(job["database"], job["schema"], job["warehouse"], job["profile"]),
                      job.get("pool_created") or time.time())
    job["conn"] = None


def cleanup_jobs():
    ttl = DEFAULT_JOB_TTL_SECONDS
    if _configured():
        try:
            ttl = _require_config()["job_ttl_seconds"]
        except HTTPException:
            pass
    now = time.time()
    cutoff = now - ttl
    with _jobs_lock:
        stale = [jid for jid, j in _jobs.items() if j["created_at"] < cutoff]
        # A finished query nobody is paging through has no reason to sit on a
        # Snowflake session. Give it back; the next page reacquires one and
        # reads the same result via RESULT_SCAN.
        idle = [j for j in _jobs.values()
                if j.get("conn") is not None
                and j["status"] != "running"
                and now - j.get("last_used", 0) > JOB_IDLE_RELEASE_SECONDS]
    for job in idle:
        _close_job(job)
    for jid in stale:
        with _jobs_lock:
            job = _jobs.pop(jid, None)
        if job:
            _close_job(job)


def _describe_columns(cur) -> List[Dict[str, str]]:
    """Column names plus a readable Snowflake type, straight off the cursor
    description so it costs nothing extra."""
    out = []
    for d in (cur.description or []):
        name = d[0]
        type_code = d[1]
        try:
            from snowflake.connector.constants import FIELD_ID_TO_NAME
            type_name = FIELD_ID_TO_NAME.get(type_code, str(type_code))
        except Exception:
            type_name = str(type_code)
        internal_size = d[3] if len(d) > 3 else None
        precision = d[4] if len(d) > 4 else None
        scale = d[5] if len(d) > 5 else None
        out.append({
            "name": name,
            "type": type_name,
            "display_type": _display_type(type_name, internal_size, precision, scale),
        })
    return out


def _display_type(type_name, internal_size, precision, scale) -> str:
    """Connector names (FIXED, TEXT, REAL) turned into what Snowsight shows:
    NUMBER(38,0), VARCHAR(16777216), FLOAT, TIMESTAMP_NTZ(9)."""
    t = (type_name or "").upper()
    if t == "FIXED":
        return f"NUMBER({precision},{scale or 0})" if precision is not None else "NUMBER"
    if t == "REAL":
        return "FLOAT"
    if t == "TEXT":
        return f"VARCHAR({internal_size})" if internal_size else "VARCHAR"
    if t == "BINARY":
        return f"BINARY({internal_size})" if internal_size else "BINARY"
    if t in ("TIME", "TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ") and scale is not None:
        return f"{t}({scale})"
    return t


def _has_duplicate_columns(columns: List[Dict[str, str]]) -> bool:
    names = [c["name"] for c in columns]
    return len(names) != len(set(names))


def _run_job(job_id: str):
    job = _get_job(job_id)
    try:
        conn, pool_key, pool_created = _pool.acquire(job["database"], job["schema"], job["warehouse"], job["profile"])
    except HTTPException as e:
        job["status"] = "failed"
        job["error"] = e.detail if isinstance(e.detail, str) else str(e.detail)
        job["finished_at"] = time.time()
        job["done"].set()
        _job_slots.release()
        return

    job["conn"] = conn
    job["pool_key"] = pool_key
    job["pool_created"] = pool_created
    cur = None
    try:
        cur = conn.cursor()
        job["cursor"] = cur
        _apply_statement_timeout(conn, cur, job["timeout_seconds"])

        if job["cancel_requested"]:
            raise _Cancelled()

        planned = job.get("statements") or [{"sql": job["sql"], "line": 0, "cls": None}]
        for k, st in enumerate(planned):
            job["current_statement"] = k
            job["statement_started"] = started = time.time()
            cur.execute(st["sql"])
            job["query_id"] = cur.sfqid
            if job["cancel_requested"]:
                raise _Cancelled()
            if k < len(planned) - 1:
                job.setdefault("statement_results", []).append(_statement_result(st, k, started, "success", query_id=cur.sfqid))

        columns = _describe_columns(cur)
        job["columns"] = columns

        if job["returns_rows"]:
            row_count = cur.rowcount
            # SHOW/DESC and anything with duplicate output column names can't
            # be re-read through RESULT_SCAN by name, so those get materialized
            # once here and paged from memory instead.
            needs_inline = (
                job["lead_keyword"] in ("SHOW", "DESC", "DESCRIBE", "LIST", "LS")
                or _has_duplicate_columns(columns)
            )
            if needs_inline:
                rows = cur.fetchall()
                job["inline_rows"] = [[_to_json_safe(v) for v in r] for r in rows]
                job["columns"] = _dedupe_columns(columns)
                job["row_count"] = len(job["inline_rows"])
            else:
                if row_count is None or row_count < 0:
                    row_count = _result_scan_count(conn, job["query_id"])
                job["row_count"] = row_count
            job["rows_affected"] = None
        else:
            # DML/DDL: Snowflake reports what it touched in the result row.
            affected = 0
            try:
                rows = cur.fetchall()
                for row in rows:
                    for value in row:
                        if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
                            affected += int(value)
                if not rows and cur.rowcount and cur.rowcount > 0:
                    affected = cur.rowcount
            except Exception:
                affected = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            job["rows_affected"] = affected
            job["columns"] = []
            job["row_count"] = 0

        job["status"] = "success"
        job["finished_at"] = time.time()
        if len(planned) > 1:
            job["statement_results"].append(_statement_result(
                planned[-1], len(planned) - 1, job["statement_started"],
                "success", query_id=job["query_id"], row_count=job["row_count"], rows_affected=job["rows_affected"]))
    except _Cancelled:
        job["status"] = "cancelled"
        job["finished_at"] = time.time()
    except Exception as e:
        message = failed_at(str(e), job.get("statements") or [], job.get("current_statement", 0))
        if job["cancel_requested"] or "SQL execution canceled" in message:
            job["status"] = "cancelled"
        elif "STATEMENT_TIMEOUT" in message.upper() or "statement timed out" in message.lower():
            job["status"] = "failed"
            job["error"] = f"Query timed out after {job['timeout_seconds']}s"
        else:
            job["status"] = "failed"
            job["error"] = message
        job["finished_at"] = time.time()
    finally:
        job["cursor"] = None
        if cur is not None:
            try:
                cur.close()
            except Exception:
                pass
        # A finished non-SELECT (or a failed run) has nothing left to page
        # through, so its connection goes back immediately.
        if job["status"] != "success" or not job["returns_rows"] or job["inline_rows"] is not None:
            _close_job(job)
        job["done"].set()
        _job_slots.release()


def _dedupe_columns(columns: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """SHOW output can repeat a column name; suffix the repeats so the grid can
    key on them safely."""
    seen: Dict[str, int] = {}
    out = []
    for col in columns:
        name = col["name"]
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[col['name']]}"
        else:
            seen[name] = 0
        out.append({**col, "name": name})
    return out


def _result_scan_sql(query_id: str) -> str:
    """RESULT_SCAN takes the query id as a *bound value*, so this never
    interpolates it - callers pass query_id as a parameter."""
    return "SELECT * FROM TABLE(RESULT_SCAN(?))"


def _result_scan_count(conn, query_id: str) -> Optional[int]:
    cur = conn.cursor()
    try:
        cur.execute(f"SELECT COUNT(*) FROM ({_result_scan_sql(query_id)})", (query_id,))
        row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        logger.info("RESULT_SCAN count failed for %s: %s", query_id, e)
        return None
    finally:
        try:
            cur.close()
        except Exception:
            pass


def _job_connection(job: Dict[str, Any]):
    """The job's connection. No liveness ping - that was a full round trip on
    every page turn to guard against something that almost never happens.
    Callers retry once instead, via _drop_job_connection."""
    conn = job.get("conn")
    if conn is not None:
        return conn
    conn, pool_key, pool_created = _pool.acquire(job["database"], job["schema"], job["warehouse"], job["profile"])
    job["conn"] = conn
    job["pool_key"] = pool_key
    job["pool_created"] = pool_created
    return conn


def _drop_job_connection(job: Dict[str, Any]):
    """Throw away a connection that failed mid-page so the retry gets a fresh
    one. RESULT_SCAN only needs the same user and role, both of which come from
    this module's config, so a new session can still read the result."""
    _quiet_close(job.get("conn"))
    job["conn"] = None


# ============================================================================
# Workspace storage - query history and saved queries
# ============================================================================

def _db(read_only: bool = False):
    # This page has always sent naive UTC ISO strings; the browser appends the
    # Z itself (studioHelpers.formatWhen), so keep handing it that shape.
    return db.transaction(read_only=read_only, datetime_format=db.utc_naive_iso)


_db_ready = False


def _init_db():
    """Nothing to create any more (migrations own the tables). Confirms the
    schema once; until that works, history simply isn't recorded and the
    workspace endpoints answer 503, but the rest of Data Studio still runs."""
    global _db_ready
    if _db_ready:
        return
    try:
        db.ensure_ready()
        _db_ready = True
    except Exception as e:
        logger.warning("Data Studio workspace storage not ready yet: %s", e)


def _record_history(user_email: str, job: Dict[str, Any]):
    if not _db_ready:
        return
    try:
        elapsed = int(((job.get("finished_at") or time.time()) - job["started_at"]) * 1000)
        email = (user_email or "").strip().lower()
        with _db() as conn:
            conn.execute(
                """INSERT INTO sf_query_history (id, user_email, sql, database, schema, status, elapsed_ms, row_count, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    uuid.uuid4().hex,
                    email,
                    job["sql"][:20000],
                    job.get("database") or "",
                    job.get("schema") or "",
                    job.get("status") or "",
                    elapsed,
                    job.get("row_count"),
                    (job.get("error") or "")[:2000],
                    datetime.now(timezone.utc),
                ),
            )
            # Keep the newest N per person: find the (N+1)th newest and delete
            # it and everything older. The (user_email, created_at DESC, id)
            # index finds that row without sorting the lot.
            conn.execute(
                """DELETE FROM sf_query_history h
                   USING (SELECT created_at, id FROM sf_query_history
                           WHERE user_email = ?
                           ORDER BY created_at DESC, id DESC
                           OFFSET ? LIMIT 1) AS edge
                   WHERE h.user_email = ?
                     AND (h.created_at, h.id) <= (edge.created_at, edge.id)""",
                (email, HISTORY_KEEP_PER_USER, email),
            )
    except Exception as e:
        logger.info("Could not record Snowflake query history: %s", e)


# ============================================================================
# Request models
# ============================================================================

class SnowflakeQueryRequest(BaseModel):
    sql: str
    database: Optional[str] = None
    schema_: Optional[str] = Field(None, alias="schema")
    warehouse: Optional[str] = None
    confirm: bool = False
    timeout_seconds: Optional[int] = None
    # How long POST /query will hold the response open waiting for the query to
    # finish. Most queries beat this, which turns the whole run into a single
    # request instead of submit + poll + fetch.
    wait_ms: int = 2500
    page_size: int = 100

    model_config = {"populate_by_name": True}


class RowsRequest(BaseModel):
    page: int = 1
    page_size: int = 100
    sort: Optional[List[Dict[str, str]]] = None
    filters: Optional[List[ColumnFilter]] = None


class TableRowsRequest(BaseModel):
    database: str
    schema_: str = Field(..., alias="schema")
    table: str
    page: int = 1
    page_size: int = 100
    sort: Optional[List[Dict[str, str]]] = None
    filters: Optional[List[ColumnFilter]] = None
    exact_count: bool = True

    model_config = {"populate_by_name": True}


class RowChange(BaseModel):
    kind: str                                  # insert | update | delete
    values: Optional[Dict[str, Any]] = None    # insert/update: the new values
    original: Optional[Dict[str, Any]] = None  # update/delete: the row as read


class MutateRequest(BaseModel):
    database: str
    schema_: str = Field(..., alias="schema")
    table: str
    changes: List[RowChange]
    key_columns: Optional[List[str]] = None
    confirm: bool = False

    model_config = {"populate_by_name": True}


class SavedQueryRequest(BaseModel):
    name: str
    sql: str
    database: Optional[str] = None
    schema_: Optional[str] = Field(None, alias="schema")
    shared: bool = False

    model_config = {"populate_by_name": True}


class CopyRequest(BaseModel):
    copy_type: str                      # "table" | "tables" | "schema"
    mode: str                           # "clone" | "insert"
    if_exists: str = "fail"             # "fail" | "overwrite" | "append"
    source_database: str
    source_schema: str
    source_table: Optional[str] = None  # required when copy_type == "table"
    source_tables: Optional[List[str]] = None   # required when copy_type == "tables"
    target_database: str
    target_schema: str
    target_table: Optional[str] = None  # table copy only; defaults to source_table
    parallelism: Optional[int] = None   # caller-requested chunk count cap, insert mode only
    confirm: bool = False


# ============================================================================
# Endpoints - explorer
# ============================================================================

PERM = "runSnowflakeQueryRunner"
PERM_ALL_ROLES = "useAllSnowflakeRoles"   # switch to any role, not just the login's default
PERM_WRITE = "writeSnowflake"             # writes from the worksheet and grid edits
PERM_COPY = "copySnowflakeTables"         # Table Copier

_USE_ROLE = re.compile(r"\bUSE\s+(SECONDARY\s+)?ROLES?\b", re.IGNORECASE)


def _has(auth: dict, perm: str) -> bool:
    return (auth.get("permissions") or {}).get(perm) is True


def _default_role(conn_id: Optional[str]) -> str:
    try:
        return (_profile_config(conn_profile(conn_id, None)).get("role") or "").strip('"')
    except HTTPException:
        raise
    except Exception:
        return ""


def _default_conn_id() -> str:
    numbered = numbered_sections(_config)
    return next(iter(numbered)) if numbered else "default"


def _check_role(auth: dict, conn_id: Optional[str], role: Optional[str]):
    """Without All Roles a user gets one login (the first one) and its
    default role. Each login has its own role, so picking another login is
    switching role too."""
    if _has(auth, PERM_ALL_ROLES):
        return
    if conn_id not in (None, "", "default", _default_conn_id()):
        raise HTTPException(status_code=403, detail="You can only use the default Snowflake role. Ask an admin for 'All Roles' access.")
    if role and role.strip('"').upper() != _default_role(conn_id).upper():
        raise HTTPException(status_code=403, detail="You can only use the default Snowflake role. Ask an admin for 'All Roles' access.")


def _visible_connections(auth: dict) -> List[Dict[str, Any]]:
    conns = connections()
    if _has(auth, PERM_ALL_ROLES):
        return conns
    first = _default_conn_id()
    return [c for c in conns if c["id"] == first][:1] or conns[:1]


def sf_auth(request: Request, auth: dict = Depends(require_permission(PERM))) -> dict:
    """Snowflake access plus the role check. Every endpoint takes the role as
    a query param, so checking it here covers all of them."""
    _check_role(auth, request.query_params.get("conn_id"), request.query_params.get("role"))
    return auth


def _require(auth: dict, perm: str, what: str):
    if not _has(auth, perm):
        raise HTTPException(status_code=403, detail=f"You don't have permission to {what} in Snowflake")


@snowflake_router.get("/health")
def health(conn_id: Optional[str] = Query(None), role: Optional[str] = Query(None),
           auth: dict = Depends(require_permission(PERM))):
    # No role check here: an old tab saved on another login should land back
    # on the default one, not fail to connect.
    if not _has(auth, PERM_ALL_ROLES):
        conn_id, role = None, None
    cfg = _profile_config(conn_profile(conn_id, role))
    return {
        "configured": True,
        "connections": _visible_connections(auth),
        "conn_id": cfg.get("conn_id") or "default",
        "account": cfg["account"],
        "user": cfg["user"],
        "role": cfg["role"],
        "warehouse": cfg["warehouse"],
        "default_database": cfg["database"],
        "default_schema": cfg["schema"],
        "allow_writes": cfg["allow_writes"] and _has(auth, PERM_WRITE),
        "can_write": _has(auth, PERM_WRITE),
        "can_copy": _has(auth, PERM_COPY),
        "can_use_all_roles": _has(auth, PERM_ALL_ROLES),
        "max_page_size": ROWS_PAGE_MAX_SIZE,
        # Present only when writes go through a second login - the copier uses
        # it to browse target databases with the credentials that own them.
        "write_profile": (
            {
                "configured": True,
                "account": _profile_config(WRITE_PROFILE)["account"],
                "user": _profile_config(WRITE_PROFILE)["user"],
                "role": _profile_config(WRITE_PROFILE)["role"],
                "warehouse": _profile_config(WRITE_PROFILE)["warehouse"],
            }
            if _write_configured() else {"configured": False}
        ),
    }


@snowflake_router.get("/roles")
def list_roles(conn_id: Optional[str] = Query(None), auth: dict = Depends(sf_auth)):
    """Roles a worksheet can switch to: roles_N from the config, or every role
    granted to that login when none are listed."""
    profile = conn_profile(conn_id, None)
    cfg = _profile_config(profile)
    if not _has(auth, PERM_ALL_ROLES):
        return {"roles": [cfg["role"]] if cfg["role"] else [], "default": cfg["role"], "configured": True, "locked": True}
    if cfg.get("roles"):
        return {"roles": cfg["roles"], "default": cfg["role"], "configured": True}
    cache_key = f"sf-roles:{profile}"
    cached = _cache_get(cache_key)
    if cached is None:
        def read(cur):
            cur.execute("SELECT CURRENT_AVAILABLE_ROLES()")
            return sorted(json.loads(cur.fetchone()[0] or "[]"))
        try:
            cached = _run_meta(read, profile)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Could not list roles: {e}")
        _cache_set(cache_key, cached)
    return {"roles": cached, "default": cfg["role"], "configured": False}


@snowflake_router.get("/databases")
def list_databases(
    bypass_cache: bool = Query(False),
    profile: str = Query(READ_PROFILE, description="Which credentials to browse with: read or write"),
    conn_id: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    auth: dict = Depends(sf_auth),
):
    _require_config()
    which = _use_profile(profile, conn_id, role)
    cache_key = f"sf-databases:{which}"
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}
    home = _profile_config(which)["database"]

    def read(cur):
        # INFORMATION_SCHEMA.DATABASES only lists databases the current role
        # holds privileges on, whereas SHOW DATABASES lists everything in the
        # account - including the ones that blow up with "insufficient
        # privileges" the moment you expand them.
        if home:
            try:
                cur.execute(f"SELECT DATABASE_NAME FROM {quote_ident(home)}.INFORMATION_SCHEMA.DATABASES")
                names = {r[0] for r in cur.fetchall() if r[0]}
                if names:
                    return sorted(names)
            except Exception as e:
                logger.info("INFORMATION_SCHEMA.DATABASES unavailable, falling back to SHOW: %s", e)
        cur.execute("SHOW TERSE DATABASES")
        rows = cur.fetchall()
        name_idx = _column_index(cur, "name", default=1)
        return sorted({r[name_idx] for r in rows if r[name_idx]})

    try:
        databases = _run_meta(read, which)
    except HTTPException:
        raise
    except Exception as e:
        raise _meta_http_error(e, "databases")
    payload = {"databases": databases, "cached": False}
    _cache_set(cache_key, payload)
    return payload


def _use_profile(profile: Optional[str], conn_id: Optional[str] = None, role: Optional[str] = None) -> str:
    """Catalog calls name the profile they want to browse with. Asking for the
    write profile on a deployment that has none is not an error - there is only
    one login, and it answers for both. A worksheet's login and role win."""
    if conn_id or role:
        return conn_profile(conn_id, role)
    if (profile or "").strip().lower() == WRITE_PROFILE and _write_configured():
        return WRITE_PROFILE
    return READ_PROFILE


def _column_index(cur, name: str, default: int = 0) -> int:
    """SHOW output column order isn't contractual across Snowflake releases, so
    look the column up by name and only fall back to a position."""
    for i, d in enumerate(cur.description or []):
        if d[0].lower() == name.lower():
            return i
    return default


@snowflake_router.get("/schemas")
def list_schemas(
    database: str = Query(...),
    bypass_cache: bool = Query(False),
    profile: str = Query(READ_PROFILE, description="Which credentials to browse with: read or write"),
    conn_id: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    auth: dict = Depends(sf_auth),
):
    _require_config()
    which = _use_profile(profile, conn_id, role)
    cache_key = f"sf-schemas:{which}:{database}"
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}
    def read(cur):
        # SCHEMATA is privilege-filtered the same way DATABASES is.
        cur.execute(f"SELECT SCHEMA_NAME FROM {quote_ident(database)}.INFORMATION_SCHEMA.SCHEMATA")
        return sorted({r[0] for r in cur.fetchall() if r[0]})

    try:
        schemas = _run_meta(read, which)
    except HTTPException:
        raise
    except Exception as e:
        raise _meta_http_error(e, f"schemas in {database}")
    payload = {"schemas": schemas, "cached": False}
    _cache_set(cache_key, payload)
    return payload


@snowflake_router.get("/tables")
def list_tables(
    database: str = Query(...),
    schema: str = Query(...),
    bypass_cache: bool = Query(False),
    profile: str = Query(READ_PROFILE, description="Which credentials to browse with: read or write"),
    conn_id: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    auth: dict = Depends(sf_auth),
):
    """Tables and views with the row count and byte size Snowflake already keeps
    in INFORMATION_SCHEMA - never a live COUNT(*), so this stays instant even
    against schemas full of billion-row tables."""
    _require_config()
    which = _use_profile(profile, conn_id, role)
    cache_key = f"sf-tables:{which}:{database}:{schema}"
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}
    def read(cur):
        cur.execute(
            f"""
            SELECT TABLE_NAME, TABLE_TYPE, ROW_COUNT, BYTES, IS_TRANSIENT
            FROM {quote_ident(database)}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = ?
            ORDER BY TABLE_NAME
            """,
            (schema,),
        )
        tables = []
        for name, table_type, row_count, byte_size, transient in cur.fetchall():
            kind = "table"
            upper = (table_type or "").upper()
            if "VIEW" in upper:
                kind = "materialized_view" if "MATERIALIZED" in upper else "view"
            elif "EXTERNAL" in upper:
                kind = "external_table"
            tables.append({
                "name": name,
                "kind": kind,
                "est_rows": int(row_count) if row_count is not None else None,
                "size_bytes": int(byte_size) if byte_size is not None else None,
                "transient": (transient or "NO").upper() == "YES",
            })
        return tables

    try:
        tables = _run_meta(read, which)
    except HTTPException:
        raise
    except Exception as e:
        raise _meta_http_error(e, f"tables in {database}.{schema}")
    payload = {"tables": tables, "cached": False}
    _cache_set(cache_key, payload)
    return payload


# ---------------------------------------------------------------------------
# Table name search. One in-memory index of every table/view name the role can
# see, built with a single SHOW call (metadata only, no warehouse), served
# stale while it refreshes in the background so a search never waits on
# Snowflake after the first build.
# ---------------------------------------------------------------------------

NAME_INDEX_TTL = 10 * 60
SHOW_CAP = 10000            # SHOW never returns more than this many rows
_name_index: Dict[str, Dict[str, Any]] = {}
_name_index_lock = threading.Lock()
_SPLIT = re.compile(r"[^a-z0-9]+")


def _kind_of(raw: str) -> str:
    up = (raw or "").upper()
    if "MATERIALIZED" in up:
        return "materialized_view"
    if "VIEW" in up:
        return "view"
    if "EXTERNAL" in up:
        return "external_table"
    return "table"


def _show_objects(cur, scope: str) -> List[tuple]:
    cur.execute(f"SHOW TERSE OBJECTS IN {scope}")
    rows = cur.fetchall()
    di = _column_index(cur, "database_name", 3)
    si = _column_index(cur, "schema_name", 4)
    ni = _column_index(cur, "name", 1)
    ki = _column_index(cur, "kind", 2)
    return [(r[di], r[si], r[ni], r[ki]) for r in rows if r[si] != "INFORMATION_SCHEMA"]


def _info_schema_objects(cur, database: str) -> List[tuple]:
    cur.execute(
        f"""
        SELECT TABLE_CATALOG, TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
        FROM {quote_ident(database)}.INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA <> 'INFORMATION_SCHEMA'
        """
    )
    return [tuple(r) for r in cur.fetchall()]


def _build_name_index(which: str, dbs: List[str]) -> List[tuple]:
    # fastest path: one call for the whole account
    rows = _run_meta(lambda cur: _show_objects(cur, "ACCOUNT"), which)
    if len(rows) < SHOW_CAP:
        raw = rows
    else:
        # too many objects for one SHOW, go database by database
        def one(db_name):
            got = _run_meta(lambda cur: _show_objects(cur, f"DATABASE {quote_ident(db_name)}"), which)
            if len(got) >= SHOW_CAP:
                got = _run_meta(lambda cur: _info_schema_objects(cur, db_name), which)
            return got

        raw = []
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(dbs)))) as pool:
            for fut in [pool.submit(one, d) for d in dbs]:
                try:
                    raw.extend(fut.result())
                except Exception as e:
                    logger.info("name index skipped a database: %s", e)

    items = []
    for db_name, schema, name, kind in raw:
        if not name:
            continue
        low = name.lower()
        items.append((db_name, schema, name, _kind_of(kind), low, tuple(p for p in _SPLIT.split(low) if p)))
    return items


def _refresh_index(which: str, dbs: List[str]):
    try:
        items = _build_name_index(which, dbs)
        parts = sorted({p for it in items for p in it[5]})
        with _name_index_lock:
            _name_index[which] = {"items": items, "parts": parts, "built": time.time(), "building": False}
        logger.info("table name index for %s: %d objects", which, len(items))
    except Exception:
        with _name_index_lock:
            entry = _name_index.get(which)
            if entry:
                entry["building"] = False
        raise


def _get_index(which: str, dbs_fn, force: bool = False) -> Dict[str, Any]:
    # warm-up already running and nothing built yet, wait for it instead of
    # starting a second build
    for _ in range(300):
        with _name_index_lock:
            entry = _name_index.get(which)
            waiting = entry and entry.get("items") is None and entry.get("building")
        if not waiting:
            break
        time.sleep(0.1)
    with _name_index_lock:
        entry = _name_index.get(which)
        fresh = entry and entry.get("items") is not None and time.time() - entry["built"] < NAME_INDEX_TTL
        if entry and entry.get("items") is not None and not force:
            if not fresh and not entry["building"]:
                entry["building"] = True
                threading.Thread(target=_bg_refresh, args=(which, dbs_fn), daemon=True).start()
            return entry
    _refresh_index(which, dbs_fn())
    return _name_index[which]


def _bg_refresh(which, dbs_fn):
    try:
        _refresh_index(which, dbs_fn())
    except Exception as e:
        logger.info("background name index refresh failed: %s", e)


def _token_part_scores(token: str, parts: List[str]) -> Dict[str, float]:
    # score each distinct name part once, not once per table
    out = {}
    n = len(token)
    for p in parts:
        if p == token:
            out[p] = 1.0
        elif p.startswith(token):
            out[p] = 0.92
        elif token in p:
            out[p] = 0.85
        elif n >= 4 and abs(len(p) - n) <= 2:
            sm = difflib.SequenceMatcher(None, token, p)
            if sm.real_quick_ratio() >= 0.75 and sm.quick_ratio() >= 0.75:
                r = sm.ratio()
                if r >= 0.75:
                    out[p] = r * 0.8
    return out


def _search_index(entry: Dict[str, Any], q: str, database: Optional[str]) -> List[Dict[str, Any]]:
    tokens = [t for t in _SPLIT.split(q.lower()) if t]
    if not tokens:
        return []
    whole = "_".join(tokens)
    tables = [_token_part_scores(t, entry["parts"]) for t in tokens]
    want_db = database.upper() if database else None
    hits = []
    for db_name, schema, name, kind, low, parts in entry["items"]:
        if want_db and db_name.upper() != want_db:
            continue
        if low == whole:
            score = 3.0
        else:
            total = 0.0
            for t, table in zip(tokens, tables):
                best = 0.0
                for p in parts:
                    v = table.get(p)
                    if v and v > best:
                        best = v
                if not best and t in low:
                    best = 0.85
                if not best:
                    break
                total += best
            else:
                score = total / len(tokens) + (0.3 if whole in low else 0)
                hits.append((score, db_name, schema, name, kind))
            continue
        hits.append((score, db_name, schema, name, kind))
    hits.sort(key=lambda h: (-h[0], len(h[3]), h[3]))
    return hits


@snowflake_router.get("/search-tables")
def search_tables(
    q: str = Query(..., min_length=2),
    database: Optional[str] = Query(None, description="Only search this database"),
    limit: int = Query(100, ge=1, le=500),
    bypass_cache: bool = Query(False),
    profile: str = Query(READ_PROFILE),
    conn_id: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    auth: dict = Depends(sf_auth),
):
    """Find tables and views by name across every database the role can see.
    Each word in q can hit any part of the name, small typos are ok."""
    _require_config()
    which = _use_profile(profile, conn_id, role)
    dbs_fn = lambda: list_databases(bypass_cache=False, profile=profile, conn_id=conn_id, role=role, auth=auth)["databases"]
    try:
        entry = _get_index(which, dbs_fn, force=bypass_cache)
    except HTTPException:
        raise
    except Exception as e:
        raise _meta_http_error(e, "table names")
    hits = _search_index(entry, q, database)
    return {
        "results": [
            {"database": d, "schema": s, "name": n, "kind": k, "score": round(sc, 3)}
            for sc, d, s, n, k in hits[:limit]
        ],
        "total": len(hits),
        "indexed": len(entry["items"]),
        "age_seconds": int(time.time() - entry["built"]),
    }


@snowflake_router.post("/search-tables/warm")
def warm_search_index(
    force: bool = Query(False, description="Rebuild even if the index is fresh"),
    profile: str = Query(READ_PROFILE),
    conn_id: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    auth: dict = Depends(sf_auth),
):
    """Called when the explorer opens, so the index is ready before anyone types."""
    _require_config()
    which = _use_profile(profile, conn_id, role)
    with _name_index_lock:
        entry = _name_index.get(which)
        if entry and (entry.get("building") or (not force and time.time() - entry["built"] < NAME_INDEX_TTL)):
            return {"ok": True, "ready": entry.get("items") is not None}
        if entry:
            entry["building"] = True
        else:
            _name_index[which] = {"items": None, "parts": [], "built": 0, "building": True}
    dbs_fn = lambda: list_databases(bypass_cache=False, profile=profile, conn_id=conn_id, role=role, auth=auth)["databases"]
    threading.Thread(target=_bg_refresh, args=(which, dbs_fn), daemon=True).start()
    return {"ok": True, "ready": False}


def _fetch_columns(database: str, schema: str, table: str, bypass_cache: bool = False,
                    profile: str = READ_PROFILE) -> List[Dict[str, Any]]:
    """Column metadata for a table. Cached, because the data grid needs it on
    every page turn and every save, and a schema doesn't change between two
    clicks of the Next button."""
    cache_key = f"sf-colmeta:{profile}:{database}:{schema}:{table}"
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

    def read(cur):
        cur.execute(
            f"""
            SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, ORDINAL_POSITION,
                   CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, COMMENT
            FROM {quote_ident(database)}.INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            ORDER BY ORDINAL_POSITION
            """,
            (schema, table),
        )
        raw = cur.fetchall()
        if not raw:
            raise HTTPException(status_code=404, detail=f"{schema}.{table} was not found in {database}")

        pk_cols: List[str] = []
        try:
            cur.execute(f"SHOW PRIMARY KEYS IN TABLE {qualified(database, schema, table)}")
            pk_rows = cur.fetchall()
            col_idx = _column_index(cur, "column_name", default=4)
            seq_idx = _column_index(cur, "key_sequence", default=5)
            ordered = sorted(pk_rows, key=lambda r: r[seq_idx] if isinstance(r[seq_idx], int) else 0)
            pk_cols = [r[col_idx] for r in ordered]
        except Exception as e:
            # Views and most Snowflake tables have no declared PK. Not an error.
            logger.debug("No primary keys for %s.%s.%s: %s", database, schema, table, e)
        return raw, pk_cols

    raw, pk_cols = _run_meta(read, profile)

    columns = [_column_row(*row, pk_cols=pk_cols) for row in raw]
    _cache_set(cache_key, columns)
    return columns


def _column_row(name, data_type, nullable, default, position, char_len, precision, scale, comment,
                 pk_cols=()) -> Dict[str, Any]:
    return {
        "name": name,
        "data_type": (data_type or "").upper(),
        "nullable": (nullable or "YES").upper() == "YES",
        "default": default,
        "position": position,
        "char_length": char_len,
        "precision": precision,
        "scale": scale,
        "comment": comment,
        "is_primary_key": name in pk_cols,
        "editable": is_editable_type(data_type or ""),
        "comparable": is_comparable_type(data_type or ""),
    }


def _fetch_schema_columns(database: str, schema: str, profile: str = READ_PROFILE) -> Dict[str, List[Dict[str, Any]]]:
    """Every column in the schema, keyed by table, in one query.

    A copy that has to build its own CREATE TABLE needs the columns of each
    object it is copying. Asking per table means one round trip each - plus
    another for its primary keys - which is 170 of them for an 85 table schema
    and about half a minute of staring at "Checking...". Primary keys are left
    out on purpose: a copy does not use them."""
    cache_key = f"sf-schemacols:{profile}:{database}:{schema}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    def read(cur):
        cur.execute(
            f"""
            SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT, ORDINAL_POSITION,
                   CHARACTER_MAXIMUM_LENGTH, NUMERIC_PRECISION, NUMERIC_SCALE, COMMENT
            FROM {quote_ident(database)}.INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = ?
            ORDER BY TABLE_NAME, ORDINAL_POSITION
            """,
            (schema,),
        )
        return cur.fetchall()

    try:
        rows = _run_meta(read, profile)
    except Exception as e:
        # Not fatal - the caller falls back to asking table by table.
        logger.info("Bulk column read failed for %s.%s: %s", database, schema, e)
        return {}

    by_table: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        by_table.setdefault(row[0], []).append(_column_row(*row[1:]))
    _cache_set(cache_key, by_table)
    return by_table


@snowflake_router.get("/columns")
def list_columns(
    database: str = Query(...),
    schema: str = Query(...),
    table: str = Query(...),
    bypass_cache: bool = Query(False),
    conn_id: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    auth: dict = Depends(sf_auth),
):
    _require_config()
    which = _use_profile(None, conn_id, role)
    cache_key = f"sf-columns:{which}:{database}:{schema}:{table}"
    if not bypass_cache:
        cached = _cache_get(cache_key)
        if cached is not None:
            return {**cached, "cached": True}
    try:
        columns = _fetch_columns(database, schema, table, bypass_cache=bypass_cache, profile=which)
    except HTTPException:
        raise
    except Exception as e:
        raise _meta_http_error(e, f"columns of {schema}.{table}")
    payload = {
        "columns": columns,
        "primary_key": [c["name"] for c in columns if c["is_primary_key"]],
        "cached": False,
    }
    _cache_set(cache_key, payload)
    return payload


DDL_KINDS = {"TABLE", "VIEW", "MATERIALIZED_VIEW", "FUNCTION", "PROCEDURE", "SCHEMA"}


def _object_ddl(database: str, schema: str, name: str, kind: str = "TABLE",
                 profile: str = READ_PROFILE) -> str:
    """GET_DDL for one object, straight from Snowflake."""
    kind_upper = (kind or "TABLE").upper().replace(" ", "_")
    if kind_upper not in DDL_KINDS:
        raise HTTPException(status_code=400, detail=f"Unsupported object kind '{kind}'")
    # GET_DDL has no MATERIALIZED_VIEW argument - VIEW returns it either way.
    sf_kind = "VIEW" if kind_upper == "MATERIALIZED_VIEW" else kind_upper

    # Cached: a schema copy reads the DDL of every view it finds, and it builds
    # the same plan twice - once to preview it, once to run it.
    cache_key = f"sf-ddl:{profile}:{sf_kind}:{database}:{schema}:{name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    def read(cur):
        cur.execute("SELECT GET_DDL(?, ?, TRUE)", (sf_kind, f"{database}.{schema}.{name}"))
        return cur.fetchone()

    row = _run_meta(read, profile)
    ddl = row[0] if row else ""
    _cache_set(cache_key, ddl)
    return ddl


@snowflake_router.get("/ddl")
def get_ddl(
    database: str = Query(...),
    schema: str = Query(...),
    name: str = Query(...),
    kind: str = Query("TABLE"),
    profile: str = Query(READ_PROFILE, description="Which credentials to read the DDL with"),
    conn_id: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    auth: dict = Depends(sf_auth),
):
    """GET_DDL for a table or view - the thing every DBeaver user reaches for
    right after opening an object."""
    try:
        return {"ddl": _object_ddl(database, schema, name, kind, _use_profile(profile, conn_id, role))}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read DDL: {e}")


# ============================================================================
# Endpoints - query execution
# ============================================================================

@snowflake_router.post("/query")
def start_query(body: SnowflakeQueryRequest, conn_id: Optional[str] = Query(None), role: Optional[str] = Query(None),
                auth: dict = Depends(sf_auth)):
    cleanup_jobs()
    profile = _use_profile(None, conn_id, role)
    cfg = _profile_config(profile)
    _resize_job_slots(cfg["max_concurrent_jobs"])

    planned = plan_statements(body.sql)
    risky = [p["cls"] for p in planned if p["cls"].is_destructive]
    if risky:
        _require(auth, PERM_WRITE, "run write statements")
    if not _has(auth, PERM_ALL_ROLES) and _USE_ROLE.search(_strip_sql_comments(body.sql or "")):
        raise HTTPException(status_code=403, detail="USE ROLE needs 'All roles' access in Snowflake")
    if risky and not cfg["allow_writes"]:
        raise HTTPException(status_code=403, detail="This connection is configured read-only (allow_writes = false)")
    if risky and not body.confirm:
        raise HTTPException(status_code=409, detail=confirm_detail(planned))
    # The last statement's result is the one shown, like a Snowsight "Run all".
    classification = planned[-1]["cls"]

    timeout_seconds = body.timeout_seconds or cfg["statement_timeout_seconds"]
    timeout_seconds = max(1, min(int(timeout_seconds), cfg["max_statement_timeout_seconds"]))

    if not _job_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail=f"Too many queries already running (max {cfg['max_concurrent_jobs']} concurrent). Cancel one or wait for it to finish.",
        )

    job = _new_job(
        classification,
        body.database or cfg["database"],
        body.schema_ or cfg["schema"],
        body.warehouse,
        timeout_seconds,
        auth["user"]["email"],
    )
    job["statements"] = planned
    job["statement_results"] = []
    job["profile"] = profile
    job["is_destructive"] = bool(risky)
    if len(planned) > 1:
        job["sql"] = body.sql.strip()
    try:
        _executor.submit(_run_job, job["job_id"])
    except Exception as e:
        with _jobs_lock:
            _jobs.pop(job["job_id"], None)
        _job_slots.release()
        raise HTTPException(status_code=503, detail=f"Could not schedule the query: {e}")

    response = {
        "job_id": job["job_id"],
        "status": "running",
        "statement_type": classification.lead_keyword,
        "is_destructive": bool(risky),
        "returns_rows": classification.returns_rows,
        "warning": classification.warning if len(planned) == 1 else None,
    }

    wait_seconds = max(0.0, min(int(body.wait_ms or 0), 15000) / 1000.0)
    if wait_seconds and job["done"].wait(wait_seconds):
        response.update(_job_status_payload(job))
        _record_history_once(auth["user"]["email"], job)
        if job["status"] == "success" and job["returns_rows"]:
            page_size = max(1, min(int(body.page_size or 100), ROWS_PAGE_MAX_SIZE))
            try:
                response["first_page"] = get_query_rows(
                    job["job_id"], RowsRequest(page=1, page_size=page_size), auth=auth,
                )
            except HTTPException:
                # Not fatal - the client just falls back to asking for page 1.
                pass
    return response


def _job_status_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "statement_type": job["lead_keyword"],
        "returns_rows": job["returns_rows"],
        "is_destructive": job["is_destructive"],
        "columns": job["columns"],
        "row_count": job["row_count"],
        "rows_affected": job["rows_affected"],
        "query_id": job["query_id"],
        "error": job["error"],
        "elapsed_ms": int(((job["finished_at"] or time.time()) - job["started_at"]) * 1000),
        "statement_count": len(job.get("statements") or []) or 1,
        "statements": job.get("statement_results") or [],
    }


def _record_history_once(user_email: str, job: Dict[str, Any]):
    if job["status"] != "running" and not job.get("_history_written"):
        job["_history_written"] = True
        _record_history(user_email, job)


@snowflake_router.get("/query/{job_id}/status")
def get_query_status(job_id: str, wait_ms: int = Query(0, ge=0, le=15000),
                     auth: dict = Depends(sf_auth)):
    """Long-polls when wait_ms is set: the request parks on the job's completion
    event instead of the client burning round trips on a fixed timer."""
    job = _get_job(job_id, auth["user"]["email"])
    if wait_ms and job["status"] == "running":
        job["done"].wait(wait_ms / 1000.0)
        _touch(job)
    _record_history_once(auth["user"]["email"], job)
    return _job_status_payload(job)


def _paged_inline(job: Dict[str, Any], body: RowsRequest):
    """Paging, sorting and filtering for the small materialized results (SHOW,
    duplicate column names) that RESULT_SCAN can't serve."""
    names = [c["name"] for c in job["columns"]]
    rows = job["inline_rows"] or []

    if body.filters:
        idx = {n: i for i, n in enumerate(names)}
        for f in body.filters:
            if f.col not in idx:
                raise HTTPException(status_code=400, detail=f"'{f.col}' is not a column of this result")
            i = idx[f.col]
            op = (f.op or "").lower()
            needle = "" if f.value is None else str(f.value).lower()
            if op == "is_null":
                rows = [r for r in rows if r[i] is None]
            elif op == "is_not_null":
                rows = [r for r in rows if r[i] is not None]
            elif op == "contains":
                rows = [r for r in rows if needle in _flatten_for_text(r[i]).lower()]
            elif op == "not_contains":
                rows = [r for r in rows if needle not in _flatten_for_text(r[i]).lower()]
            elif op == "starts":
                rows = [r for r in rows if _flatten_for_text(r[i]).lower().startswith(needle)]
            elif op == "ends":
                rows = [r for r in rows if _flatten_for_text(r[i]).lower().endswith(needle)]
            elif op == "eq":
                rows = [r for r in rows if _flatten_for_text(r[i]) == ("" if f.value is None else str(f.value))]
            elif op == "neq":
                rows = [r for r in rows if _flatten_for_text(r[i]) != ("" if f.value is None else str(f.value))]
            elif op in ("in", "not_in"):
                wanted = {str(v) for v in (f.values or ([f.value] if f.value is not None else []))}
                if op == "in":
                    rows = [r for r in rows if _flatten_for_text(r[i]) in wanted]
                else:
                    rows = [r for r in rows if _flatten_for_text(r[i]) not in wanted]
            else:
                # gt/gte/lt/lte: numeric when both sides parse, text otherwise.
                target = "" if f.value is None else str(f.value)

                def key_of(value):
                    text = _flatten_for_text(value)
                    try:
                        return (0, float(text))
                    except (TypeError, ValueError):
                        return (1, text)

                try:
                    target_key = (0, float(target))
                except ValueError:
                    target_key = (1, target)
                compare = {
                    "gt": lambda a: a > target_key,
                    "gte": lambda a: a >= target_key,
                    "lt": lambda a: a < target_key,
                    "lte": lambda a: a <= target_key,
                }[op]
                rows = [r for r in rows if key_of(r[i])[0] == target_key[0] and compare(key_of(r[i]))]

    if body.sort:
        idx = {n: i for i, n in enumerate(names)}
        for entry in reversed(body.sort):
            col = entry.get("col")
            if col not in idx:
                raise HTTPException(status_code=400, detail=f"'{col}' is not a column of this result")
            i = idx[col]
            reverse = (entry.get("dir") or "asc").lower() == "desc"

            def sort_key(row, i=i):
                value = row[i]
                if value is None:
                    return (2, "")
                text = _flatten_for_text(value)
                try:
                    return (0, float(text))
                except (TypeError, ValueError):
                    return (1, text)

            rows = sorted(rows, key=sort_key, reverse=reverse)

    total = len(rows)
    start = (body.page - 1) * body.page_size
    return rows[start:start + body.page_size], total


@snowflake_router.post("/query/{job_id}/rows")
def get_query_rows(job_id: str, body: RowsRequest, auth: dict = Depends(sf_auth)):
    job = _get_job(job_id, auth["user"]["email"])
    if job["status"] != "success":
        raise HTTPException(status_code=409, detail=f"Query is not ready (status: {job['status']})")

    page = max(1, int(body.page))
    page_size = max(1, min(int(body.page_size), ROWS_PAGE_MAX_SIZE))
    body = body.model_copy(update={"page": page, "page_size": page_size})

    if not job["returns_rows"]:
        return {"columns": [], "rows": [], "page": 1, "page_size": page_size, "total_rows": 0, "total_pages": 1}

    if job["inline_rows"] is not None:
        rows, total = _paged_inline(job, body)
        return {
            "columns": job["columns"],
            "rows": rows,
            "page": page,
            "page_size": page_size,
            "total_rows": total,
            "total_pages": max(1, -(-total // page_size)),
        }

    names = [c["name"] for c in job["columns"]]
    where_sql, where_params = build_where(body.filters, names)
    order_sql = build_order_by(body.sort, names)
    offset = (page - 1) * page_size

    def scan(conn):
        cur = conn.cursor()
        try:
            base = f"({_result_scan_sql(job['query_id'])})"
            total = job["row_count"] or 0
            if where_sql:
                cur.execute(f"SELECT COUNT(*) FROM {base}{where_sql}", (job["query_id"], *where_params))
                total = int(cur.fetchone()[0])
            cur.execute(
                f"SELECT * FROM {base}{where_sql}{order_sql} LIMIT {page_size} OFFSET {offset}",
                (job["query_id"], *where_params),
            )
            return [[_to_json_safe(v) for v in r] for r in cur.fetchall()], total
        finally:
            try:
                cur.close()
            except Exception:
                pass

    with job["job_lock"]:
        try:
            rows, total = scan(_job_connection(job))
        except Exception as first:
            if not _looks_stale(first):
                raise HTTPException(status_code=500, detail=_friendly_result_scan_error(first))
            _drop_job_connection(job)
            try:
                rows, total = scan(_job_connection(job))
            except Exception as second:
                raise HTTPException(status_code=500, detail=_friendly_result_scan_error(second))

    return {
        "columns": job["columns"],
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "total_rows": total,
        "total_pages": max(1, -(-total // page_size)) if total else 1,
    }


_PRIVILEGE_MARKERS = ("insufficient privileges", "does not exist or not authorized",
                      "not authorized", "003001", "002003")


def _is_privilege_error(e: Exception) -> bool:
    text = str(e).lower()
    return any(marker in text for marker in _PRIVILEGE_MARKERS)


def _meta_http_error(e: Exception, what: str) -> HTTPException:
    """A missing grant isn't a server fault - it's a 403 the tree can render as
    a quiet "no access" line instead of a wall of red."""
    if _is_privilege_error(e):
        return HTTPException(status_code=403, detail=f"This role has no access to {what}.")
    return HTTPException(status_code=500, detail=f"Failed to list {what}: {e}")


def _friendly_result_scan_error(e: Exception) -> str:
    text = str(e)
    if "does not exist" in text.lower() and "result" in text.lower():
        return "This result set has expired in Snowflake (results are kept for 24 hours). Re-run the query."
    return f"Failed to fetch rows: {text}"


@snowflake_router.post("/query/{job_id}/cancel")
def cancel_query(job_id: str, auth: dict = Depends(sf_auth)):
    job = _get_job(job_id, auth["user"]["email"])
    if job["status"] != "running":
        return {"job_id": job_id, "status": job["status"]}
    job["cancel_requested"] = True
    cur = job.get("cursor")
    if cur is not None:
        try:
            cur.cancel()
        except Exception as e:
            logger.info("Snowflake cursor cancel failed for job %s: %s", job_id, e)
    query_id = job.get("query_id")
    if query_id:
        # Belt and braces: the cursor-level cancel can miss if the request is
        # mid-flight, SYSTEM$CANCEL_QUERY never does.
        try:
            with _pooled(job["database"], job["schema"], job["warehouse"], job["profile"]) as side:
                side_cur = side.cursor()
                try:
                    side_cur.execute("SELECT SYSTEM$CANCEL_QUERY(?)", (query_id,))
                finally:
                    side_cur.close()
        except Exception as e:
            logger.info("SYSTEM$CANCEL_QUERY failed for job %s: %s", job_id, e)
    return {"job_id": job_id, "status": "cancelling"}


@snowflake_router.delete("/query/{job_id}")
def close_query(job_id: str, auth: dict = Depends(sf_auth)):
    job = _get_job(job_id, auth["user"]["email"])
    with _jobs_lock:
        _jobs.pop(job_id, None)
    _close_job(job)
    return {"job_id": job_id, "status": "closed"}


# ============================================================================
# Endpoints - export
# ============================================================================

EXPORT_FORMATS = {"csv", "tsv", "json", "xlsx", "sql"}


@snowflake_router.get("/query/{job_id}/download")
def download_query(
    job_id: str,
    fmt: str = Query("csv"),
    sort_json: Optional[str] = Query(None),
    filters_json: Optional[str] = Query(None),
    auth: dict = Depends(sf_auth),
):
    """Streams the FULL result set. Rows are pulled in DOWNLOAD_CHUNK_ROWS
    batches from a dedicated cursor, so memory stays flat no matter how big the
    result is - nothing is ever materialized in one go."""
    job = _get_job(job_id, auth["user"]["email"])
    fmt = (fmt or "csv").lower()
    if fmt not in EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported export format '{fmt}'")
    if job["status"] != "success":
        raise HTTPException(status_code=409, detail=f"Query is not ready (status: {job['status']})")
    if not job["returns_rows"]:
        raise HTTPException(status_code=409, detail="This statement did not return rows")

    sort = _parse_json_param(sort_json, "sort")
    filters = [ColumnFilter(**f) for f in (_parse_json_param(filters_json, "filters") or [])]
    names = [c["name"] for c in job["columns"]]

    if job["inline_rows"] is not None:
        rows, _ = _paged_inline(job, RowsRequest(page=1, page_size=len(job["inline_rows"]) or 1, sort=sort, filters=filters))
        return _stream_rows(fmt, names, iter([rows]), f"snowflake_query_{job_id[:8]}", job["columns"])

    where_sql, where_params = build_where(filters, names)
    order_sql = build_order_by(sort, names)
    query_id = job["query_id"]
    database, schema, warehouse, profile = job["database"], job["schema"], job["warehouse"], job["profile"]

    def batches():
        with _pooled(database, schema, warehouse, profile) as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    f"SELECT * FROM ({_result_scan_sql(query_id)}){where_sql}{order_sql}",
                    (query_id, *where_params),
                )
                while True:
                    batch = cur.fetchmany(DOWNLOAD_CHUNK_ROWS)
                    if not batch:
                        break
                    yield batch
            finally:
                try:
                    cur.close()
                except Exception:
                    pass

    return _stream_rows(fmt, names, batches(), f"snowflake_query_{job_id[:8]}", job["columns"])


def _parse_json_param(raw: Optional[str], label: str):
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail=f"'{label}' is not valid JSON")
    if not isinstance(parsed, list):
        raise HTTPException(status_code=400, detail=f"'{label}' must be a JSON array")
    return parsed


def _stream_rows(fmt: str, names: List[str], batches, base_filename: str, columns: List[Dict[str, str]]):
    if fmt in ("csv", "tsv"):
        delimiter = "\t" if fmt == "tsv" else ","

        def generate():
            buf = io.StringIO()
            writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
            writer.writerow(names)
            yield buf.getvalue()
            for batch in batches:
                buf = io.StringIO()
                writer = csv.writer(buf, delimiter=delimiter, lineterminator="\n")
                for row in batch:
                    writer.writerow([_flatten_for_text(v) for v in row])
                yield buf.getvalue()

        media = "text/tab-separated-values" if fmt == "tsv" else "text/csv"
        return _attachment(generate(), media, f"{base_filename}.{fmt}")

    if fmt == "json":
        def generate():
            yield "[\n"
            first = True
            for batch in batches:
                chunk = []
                for row in batch:
                    obj = {name: _to_json_safe(value) for name, value in zip(names, row)}
                    chunk.append(("" if first else ",\n") + json.dumps(obj, default=str))
                    first = False
                if chunk:
                    yield "".join(chunk)
            yield "\n]\n"

        return _attachment(generate(), "application/json", f"{base_filename}.json")

    if fmt == "sql":
        def generate():
            for batch in batches:
                lines = []
                for row in batch:
                    values = ", ".join(_sql_literal(v) for v in row)
                    cols = ", ".join(quote_ident(n) for n in names)
                    lines.append(f"INSERT INTO {quote_ident('TARGET_TABLE')} ({cols}) VALUES ({values});\n")
                yield "".join(lines)

        return _attachment(generate(), "application/sql", f"{base_filename}.sql")

    # xlsx: xlsxwriter's constant_memory mode keeps one row in RAM at a time.
    import tempfile

    import xlsxwriter

    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.close()
    written = 0
    truncated = False
    try:
        workbook = xlsxwriter.Workbook(tmp.name, {"constant_memory": True, "default_date_format": "yyyy-mm-dd hh:mm:ss"})
        sheet = workbook.add_worksheet("Results")
        header_fmt = workbook.add_format({"bold": True, "bg_color": "#F2F2F2", "border": 1})
        for i, name in enumerate(names):
            sheet.write_string(0, i, str(name), header_fmt)
        sheet.freeze_panes(1, 0)
        for batch in batches:
            for row in batch:
                if written >= XLSX_MAX_ROWS:
                    truncated = True
                    break
                written += 1
                for i, value in enumerate(row):
                    safe = _to_json_safe(value)
                    if safe is None:
                        continue
                    if isinstance(safe, bool):
                        sheet.write_boolean(written, i, safe)
                    elif isinstance(safe, (int, float)):
                        sheet.write_number(written, i, safe)
                    else:
                        text = _flatten_for_text(value)
                        # Excel's own hard limit on a single cell.
                        sheet.write_string(written, i, text[:32767])
            if truncated:
                break
        if truncated:
            sheet.write_string(min(written + 1, XLSX_MAX_ROWS), 0,
                               f"Truncated at {XLSX_MAX_ROWS:,} rows - Excel's sheet limit. Use CSV for the full result set.")
        workbook.close()
    except Exception as e:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to build the Excel file: {e}")

    def generate_file():
        try:
            with open(tmp.name, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 256)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    return _attachment(
        generate_file(),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        f"{base_filename}.xlsx",
    )


def _attachment(generator, media_type: str, filename: str):
    return StreamingResponse(
        generator,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _sql_literal(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    text = _flatten_for_text(value).replace("\\", "\\\\").replace("'", "''")
    return "'" + text + "'"


# ============================================================================
# Table data editor
# ----------------------------------------------------------------------------
# The grid-editing half of the page: read a table's rows directly (with server
# side paging, sorting and filtering), then push cell edits, new rows and
# deletes back as generated SQL. Every mutation runs inside one transaction and
# is verified to touch exactly the rows it claimed to, or the whole batch rolls
# back.
# ============================================================================

# Types whose values a text input can round-trip safely.
_UNEDITABLE_TYPES = {"GEOGRAPHY", "GEOMETRY", "BINARY", "VARBINARY", "VECTOR", "FILE"}
_SEMI_STRUCTURED = {"VARIANT", "OBJECT", "ARRAY", "MAP"}
_NUMERIC_TYPES = {"NUMBER", "DECIMAL", "NUMERIC", "INT", "INTEGER", "BIGINT", "SMALLINT",
                  "TINYINT", "BYTEINT", "FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "REAL"}


def _base_type(data_type: str) -> str:
    return re.sub(r"\(.*\)", "", (data_type or "")).strip().upper()


def is_editable_type(data_type: str) -> bool:
    return _base_type(data_type) not in _UNEDITABLE_TYPES


def is_comparable_type(data_type: str) -> bool:
    """Semi-structured and geo columns can't sit in a WHERE key match directly;
    they get compared as text instead."""
    base = _base_type(data_type)
    return base not in _SEMI_STRUCTURED and base not in _UNEDITABLE_TYPES


def _cast_placeholder(data_type: str) -> str:
    """SQL fragment that turns a bound string into the column's real type.
    Values always arrive as text from the browser, so the cast happens in
    Snowflake where the type rules actually live."""
    base = _base_type(data_type)
    if base in _SEMI_STRUCTURED:
        return "TO_VARIANT(PARSE_JSON(?))"
    if base in _NUMERIC_TYPES:
        return f"CAST(? AS {data_type})"
    if base in ("BOOLEAN",):
        return "TO_BOOLEAN(?)"
    if base in ("DATE",):
        return "TO_DATE(?)"
    if base in ("TIME",):
        return "TO_TIME(?)"
    if base.startswith("TIMESTAMP"):
        return f"TO_{base}(?)" if base in ("TIMESTAMP_NTZ", "TIMESTAMP_LTZ", "TIMESTAMP_TZ") else "TO_TIMESTAMP(?)"
    return "CAST(? AS VARCHAR)" if base in ("VARCHAR", "STRING", "TEXT", "CHAR", "CHARACTER") else "?"


def _bind_value(value: Any, data_type: str):
    """Everything binds as a string (or None) and gets cast in SQL. Keeping one
    representation end to end is what stops '007' from silently becoming 7 or a
    timestamp from losing its offset."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _key_columns_for(columns: List[Dict[str, Any]], requested: Optional[List[str]]) -> List[str]:
    by_name = {c["name"]: c for c in columns}
    if requested:
        unknown = [c for c in requested if c not in by_name]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unknown key column(s): {', '.join(unknown)}")
        return list(requested)
    pk = [c["name"] for c in columns if c["is_primary_key"]]
    if pk:
        return pk
    # No declared PK - Snowflake rarely has one. Match on every comparable
    # column's original value instead, and let the affected-row check below be
    # the thing that guarantees we only touched one row.
    fallback = [c["name"] for c in columns if c["comparable"]]
    if not fallback:
        raise HTTPException(
            status_code=400,
            detail="This table has no primary key and no comparable columns, so rows cannot be matched safely. Add a primary key or edit via SQL.",
        )
    return fallback


def _where_for_row(original: Dict[str, Any], key_columns: List[str], columns_by_name: Dict[str, Dict[str, Any]]):
    """WHERE that matches exactly the row the user was looking at. EQUAL_NULL is
    used throughout so a NULL in a key column matches a NULL rather than
    matching nothing."""
    clauses, params = [], []
    for name in key_columns:
        col = columns_by_name.get(name)
        if col is None:
            raise HTTPException(status_code=400, detail=f"Unknown key column '{name}'")
        if name not in original:
            raise HTTPException(status_code=400, detail=f"The row is missing key column '{name}' - refresh and try again")
        value = original[name]
        ident = quote_ident(name)
        if not col["comparable"]:
            clauses.append(f"EQUAL_NULL(TO_VARCHAR({ident}), ?)")
            params.append(_bind_value(value, "VARCHAR"))
        elif _base_type(col["data_type"]) in _SEMI_STRUCTURED:
            clauses.append(f"EQUAL_NULL(TO_VARCHAR({ident}), ?)")
            params.append(_bind_value(value, "VARCHAR"))
        else:
            clauses.append(f"EQUAL_NULL({ident}, {_cast_placeholder(col['data_type'])})")
            params.append(_bind_value(value, col["data_type"]))
    if not clauses:
        raise HTTPException(status_code=400, detail="No key columns available to match this row")
    return " AND ".join(clauses), params


@snowflake_router.post("/table/rows")
def table_rows(body: TableRowsRequest, conn_id: Optional[str] = Query(None), role: Optional[str] = Query(None),
               auth: dict = Depends(sf_auth)):
    """Paged, sorted, filtered read straight off a table - the data-editor grid,
    as opposed to the result of an ad-hoc query."""
    profile = _use_profile(None, conn_id, role)
    cfg = _profile_config(profile)
    page = max(1, int(body.page))
    page_size = max(1, min(int(body.page_size), ROWS_PAGE_MAX_SIZE))
    columns = _fetch_columns(body.database, body.schema_, body.table, profile=profile)
    names = [c["name"] for c in columns]

    where_sql, where_params = build_where(body.filters, names)
    order_sql = build_order_by(body.sort, names)
    target = qualified(body.database, body.schema_, body.table)
    offset = (page - 1) * page_size

    # The row count and the page itself are independent queries, so they run at
    # the same time on two pooled connections rather than one after the other.
    count_future = None
    if body.exact_count:
        def count_rows():
            with _pooled(body.database, body.schema_, profile=profile) as conn:
                cur = conn.cursor()
                try:
                    cur.execute(f"SELECT COUNT(*) FROM {target}{where_sql}", tuple(where_params))
                    return int(cur.fetchone()[0])
                finally:
                    cur.close()

        count_future = _executor.submit(count_rows)

    try:
        with _pooled(body.database, body.schema_, profile=profile) as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    f"SELECT * FROM {target}{where_sql}{order_sql} LIMIT {page_size} OFFSET {offset}",
                    tuple(where_params),
                )
                result_columns = _describe_columns(cur)
                rows = [[_to_json_safe(v) for v in r] for r in cur.fetchall()]
            finally:
                cur.close()
    except HTTPException:
        raise
    except Exception as e:
        if count_future is not None:
            count_future.cancel()
        raise HTTPException(status_code=500, detail=f"Failed to read {body.schema_}.{body.table}: {e}")

    total = None
    if count_future is not None:
        try:
            total = count_future.result()
        except Exception as e:
            # A slow or failed count shouldn't cost the user their rows.
            logger.info("Row count for %s failed: %s", target, e)

    key_columns = [c["name"] for c in columns if c["is_primary_key"]]
    return {
        "columns": columns,
        "result_columns": result_columns,
        "rows": rows,
        "page": page,
        "page_size": page_size,
        "total_rows": total,
        "total_pages": max(1, -(-total // page_size)) if total else None,
        "primary_key": key_columns,
        "editable": cfg["allow_writes"] and _has(auth, PERM_WRITE),
    }


@snowflake_router.post("/table/mutate")
def table_mutate(body: MutateRequest, conn_id: Optional[str] = Query(None), role: Optional[str] = Query(None),
                 auth: dict = Depends(sf_auth)):
    """Applies grid edits as one transaction. Each statement is checked to have
    affected exactly one row; anything else rolls the whole batch back, so a
    stale row version can never quietly update a hundred rows."""
    _require(auth, PERM_WRITE, "edit table data")
    profile = _use_profile(None, conn_id, role)
    cfg = _profile_config(profile)
    if not cfg["allow_writes"]:
        raise HTTPException(status_code=403, detail="This connection is configured read-only")
    if not body.changes:
        raise HTTPException(status_code=400, detail="No changes to apply")
    if len(body.changes) > 500:
        raise HTTPException(status_code=400, detail="Apply at most 500 row changes at a time")

    columns = _fetch_columns(body.database, body.schema_, body.table, profile=profile)
    by_name = {c["name"]: c for c in columns}
    key_columns = _key_columns_for(columns, body.key_columns)
    target = qualified(body.database, body.schema_, body.table)

    # Build every statement up front so a bad change fails before anything runs.
    plan: List[Dict[str, Any]] = []
    for index, change in enumerate(body.changes):
        kind = (change.kind or "").lower()
        if kind == "insert":
            values = change.values or {}
            if not values:
                raise HTTPException(status_code=400, detail=f"Change {index + 1}: an insert needs at least one value")
            unknown = [c for c in values if c not in by_name]
            if unknown:
                raise HTTPException(status_code=400, detail=f"Change {index + 1}: unknown column(s) {', '.join(unknown)}")
            cols, placeholders, params = [], [], []
            for name, value in values.items():
                col = by_name[name]
                if not col["editable"]:
                    raise HTTPException(status_code=400, detail=f"Column '{name}' ({col['data_type']}) cannot be edited from the grid")
                cols.append(quote_ident(name))
                placeholders.append("NULL" if value is None else _cast_placeholder(col["data_type"]))
                if value is not None:
                    params.append(_bind_value(value, col["data_type"]))
            sql = f"INSERT INTO {target} ({', '.join(cols)}) SELECT {', '.join(placeholders)}"
            plan.append({"kind": "insert", "sql": sql, "params": params, "expect": 1, "index": index,
                         "dup_check": _dup_check(target, {k: values.get(k) for k in key_columns}, key_columns, by_name)})

        elif kind == "update":
            values = change.values or {}
            original = change.original or {}
            if not values:
                raise HTTPException(status_code=400, detail=f"Change {index + 1}: an update needs at least one changed value")
            unknown = [c for c in values if c not in by_name]
            if unknown:
                raise HTTPException(status_code=400, detail=f"Change {index + 1}: unknown column(s) {', '.join(unknown)}")
            sets, params = [], []
            for name, value in values.items():
                col = by_name[name]
                if not col["editable"]:
                    raise HTTPException(status_code=400, detail=f"Column '{name}' ({col['data_type']}) cannot be edited from the grid")
                if value is None:
                    sets.append(f"{quote_ident(name)} = NULL")
                else:
                    sets.append(f"{quote_ident(name)} = {_cast_placeholder(col['data_type'])}")
                    params.append(_bind_value(value, col["data_type"]))
            where_sql, where_params = _where_for_row(original, key_columns, by_name)
            sql = f"UPDATE {target} SET {', '.join(sets)} WHERE {where_sql}"
            after = {k: (values[k] if k in values else original.get(k)) for k in key_columns}
            plan.append({"kind": "update", "sql": sql, "params": params + where_params, "expect": 1, "index": index,
                         "dup_check": _dup_check(target, after, key_columns, by_name)})

        elif kind == "delete":
            original = change.original or {}
            where_sql, where_params = _where_for_row(original, key_columns, by_name)
            sql = f"DELETE FROM {target} WHERE {where_sql}"
            plan.append({"kind": "delete", "sql": sql, "params": where_params, "expect": 1, "index": index})

        else:
            raise HTTPException(status_code=400, detail=f"Change {index + 1}: unknown change kind '{change.kind}'")

    if not body.confirm:
        return {
            "requires_confirmation": True,
            "key_columns": key_columns,
            # params let the UI show the actual values in the preview; the
            # confirmed call rebuilds and binds them the same way.
            "statements": [{"kind": p["kind"], "sql": p["sql"], "params": p["params"]} for p in plan],
            "summary": _change_summary(plan),
        }

    conn, pool_key, pool_created = _pool.acquire(body.database, body.schema_, profile=profile)
    applied = []
    healthy = True
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        try:
            for step in plan:
                cur.execute(step["sql"], tuple(step["params"]))
                affected = _affected_rows(cur)
                if affected != step["expect"]:
                    raise _MutationMismatch(step, affected)
                # The grid matches rows on the key columns, so two rows with the
                # same key values could never be edited safely again. Checked
                # after the statement so earlier rows in this batch count too.
                if step.get("dup_check"):
                    check_sql, check_params = step["dup_check"]
                    cur.execute(check_sql, tuple(check_params))
                    found = cur.fetchone()
                    if found and found[0] and int(found[0]) > 1:
                        raise _DuplicateRow(step, key_columns)
                applied.append({"kind": step["kind"], "rows": affected})
            cur.execute("COMMIT")
        except Exception:
            try:
                cur.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            cur.close()
    except _DuplicateRow as dr:
        step = dr.step
        what = "the same primary key as" if any(c["is_primary_key"] for c in columns) else "exactly the same values as"
        raise HTTPException(
            status_code=409,
            detail=(
                f"Change {step['index'] + 1} ({step['kind']}) would leave a row with {what} another row "
                f"(compared on {', '.join(dr.key_columns[:6])}{'...' if len(dr.key_columns) > 6 else ''}). "
                "Nothing was applied. Change at least one value and try again."
            ),
        )
    except _MutationMismatch as mm:
        step, affected = mm.step, mm.affected
        if affected == 0:
            detail = (
                f"Change {step['index'] + 1} ({step['kind']}) matched no rows - the row was changed or deleted by "
                "someone else since you loaded it. Nothing was applied; refresh and try again."
            )
        else:
            detail = (
                f"Change {step['index'] + 1} ({step['kind']}) would have affected {affected} rows instead of 1. "
                "Nothing was applied. This table has no unique key for these values."
            )
        raise HTTPException(status_code=409, detail=detail)
    except HTTPException:
        raise
    except Exception as e:
        healthy = not _looks_stale(e)
        raise HTTPException(status_code=500, detail=f"Changes were rolled back: {e}")
    finally:
        if healthy:
            _pool.release(conn, pool_key, pool_created)
        else:
            _quiet_close(conn)

    _drop_table_cache(body.database, body.schema_)
    _cache_drop_prefix(f"sf-tables:{profile}:{body.database}:{body.schema_}")
    try:
        from audit import write_audit_event
        write_audit_event(
            "sfqr_table_edit",
            auth["user"]["email"],
            f"{body.database}.{body.schema_}.{body.table}",
            _change_summary(plan),
        )
    except Exception:
        pass

    return {"applied": applied, "total": len(applied), "summary": _change_summary(plan)}


class _DuplicateRow(Exception):
    def __init__(self, step: Dict[str, Any], key_columns: List[str]):
        super().__init__(f"{step['kind']} would create a duplicate row")
        self.step = step
        self.key_columns = key_columns


def _dup_check(target: str, row: Dict[str, Any], key_columns: List[str], by_name: Dict[str, Dict[str, Any]]):
    """COUNT(*) of rows that look the same as `row` on the key columns. Run
    after an insert or update, more than one means we just made a duplicate."""
    where_sql, where_params = _where_for_row(row, key_columns, by_name)
    return f"SELECT COUNT(*) FROM {target} WHERE {where_sql}", where_params


class _MutationMismatch(Exception):
    def __init__(self, step: Dict[str, Any], affected: int):
        super().__init__(f"{step['kind']} affected {affected} rows")
        self.step = step
        self.affected = affected


def _affected_rows(cur) -> int:
    """Snowflake returns the touched-row counts as a result row ('number of rows
    updated', 'number of rows inserted', ...). Sum them; fall back to rowcount."""
    try:
        rows = cur.fetchall()
    except Exception:
        rows = None
    if rows:
        total = 0
        for row in rows:
            for value in row:
                if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
                    total += int(value)
        return total
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def _change_summary(plan: List[Dict[str, Any]]) -> str:
    counts = {"insert": 0, "update": 0, "delete": 0}
    for step in plan:
        counts[step["kind"]] += 1
    parts = [f"{n} {kind}{'s' if n != 1 else ''}" for kind, n in counts.items() if n]
    return ", ".join(parts) or "no changes"


# ============================================================================
# Workspace endpoints - history and saved queries
# ============================================================================

@snowflake_router.get("/history")
def get_history(limit: int = Query(50, ge=1, le=200), auth: dict = Depends(sf_auth)):
    _init_db()
    email = (auth["user"]["email"] or "").strip().lower()
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, sql, database, schema, status, elapsed_ms, row_count, error, created_at "
            "FROM sf_query_history WHERE user_email = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (email, limit),
        ).fetchall()
    return {"history": [dict(r) for r in rows]}


@snowflake_router.delete("/history")
def clear_history(auth: dict = Depends(sf_auth)):
    _init_db()
    email = (auth["user"]["email"] or "").strip().lower()
    with _db() as conn:
        conn.execute("DELETE FROM sf_query_history WHERE user_email = ?", (email,))
    return {"cleared": True}


@snowflake_router.get("/saved")
def list_saved(auth: dict = Depends(sf_auth)):
    _init_db()
    email = (auth["user"]["email"] or "").strip().lower()
    with _db() as conn:
        rows = conn.execute(
            "SELECT id, user_email, name, sql, database, schema, shared, created_at, updated_at "
            "FROM sf_saved_queries WHERE user_email = ? OR shared ORDER BY updated_at DESC, id",
            (email,),
        ).fetchall()
    return {
        "saved": [
            {**dict(r), "shared": bool(r["shared"]), "owned": r["user_email"] == email}
            for r in rows
        ]
    }


@snowflake_router.post("/saved")
def create_saved(body: SavedQueryRequest, auth: dict = Depends(sf_auth)):
    _init_db()
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the saved query a name")
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="That name is too long (200 characters max)")
    if not (body.sql or "").strip():
        raise HTTPException(status_code=400, detail="There's no SQL to save")
    email = (auth["user"]["email"] or "").strip().lower()
    now = datetime.now(timezone.utc)
    saved_id = uuid.uuid4().hex
    with _db() as conn:
        conn.execute(
            "INSERT INTO sf_saved_queries (id, user_email, name, sql, database, schema, shared, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (saved_id, email, name, body.sql[:200000], body.database or "", body.schema_ or "", bool(body.shared), now, now),
        )
    return {"id": saved_id, "name": name, "created_at": db.utc_naive_iso(now)}


@snowflake_router.put("/saved/{saved_id}")
def update_saved(saved_id: str, body: SavedQueryRequest, auth: dict = Depends(sf_auth)):
    _init_db()
    email = (auth["user"]["email"] or "").strip().lower()
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Give the saved query a name")
    if len(name) > 200:
        raise HTTPException(status_code=400, detail="That name is too long (200 characters max)")
    if not (body.sql or "").strip():
        raise HTTPException(status_code=400, detail="There's no SQL to save")
    now = datetime.now(timezone.utc)
    with _db() as conn:
        # Owner check and write in one statement, so the row can't change
        # hands (or vanish) between checking and updating.
        updated = conn.execute(
            "UPDATE sf_saved_queries SET name = ?, sql = ?, database = ?, schema = ?, shared = ?, updated_at = ? "
            "WHERE id = ? AND user_email = ? RETURNING id",
            (name, body.sql[:200000], body.database or "", body.schema_ or "", bool(body.shared), now, saved_id, email),
        ).fetchone()
        if not updated:
            row = conn.execute("SELECT user_email FROM sf_saved_queries WHERE id = ?", (saved_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Saved query not found")
            raise HTTPException(status_code=403, detail="Only the owner can edit this saved query")
    return {"id": saved_id, "updated_at": db.utc_naive_iso(now)}


@snowflake_router.delete("/saved/{saved_id}")
def delete_saved(saved_id: str, auth: dict = Depends(sf_auth)):
    _init_db()
    email = (auth["user"]["email"] or "").strip().lower()
    with _db() as conn:
        deleted = conn.execute(
            "DELETE FROM sf_saved_queries WHERE id = ? AND user_email = ? RETURNING id", (saved_id, email)
        ).fetchone()
        if not deleted:
            row = conn.execute("SELECT user_email FROM sf_saved_queries WHERE id = ?", (saved_id,)).fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Saved query not found")
            raise HTTPException(status_code=403, detail="Only the owner can delete this saved query")
    return {"deleted": True}


def _copy_touch(job: Dict[str, Any]):
    """Wake anyone watching this job. The stream sleeps on this rather than on
    a clock, so a log line reaches the screen the moment it is written."""
    job["updated_at"] = time.time()
    pulse = job.get("pulse")
    if pulse is not None:
        pulse.set()


def _copy_log(job: Dict[str, Any], level: str, message: str):
    with job["lock"]:
        job["logs"].append({"ts": time.time(), "level": level, "message": message})
        if len(job["logs"]) > 1000:
            job["logs"] = job["logs"][-1000:]
    _copy_touch(job)


def _plan_chunks(est_rows: Optional[int], requested: Optional[int]) -> int:
    if not est_rows or est_rows <= COPY_INSERT_CHUNK_THRESHOLD:
        return 1
    n = -(-est_rows // COPY_CHUNK_TARGET_ROWS)  # ceil division
    n = max(2, min(n, MAX_CHUNKS_PER_TABLE))
    if requested:
        n = max(1, min(n, max(1, int(requested))))
    return n


def _enumerate_source_tables(database: str, schema: str) -> List[Dict[str, Any]]:
    """Everything in the schema that can be copied - tables and views alike.

    Tables come first so that a view recreated in the target has something to
    point at; external tables are left out, there being nothing to copy."""
    def read(cur):
        cur.execute(
            f"""
            SELECT TABLE_NAME, TABLE_TYPE, ROW_COUNT
            FROM {quote_ident(database)}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = ? AND TABLE_TYPE IN ('BASE TABLE', 'VIEW', 'MATERIALIZED VIEW')
            ORDER BY TABLE_NAME
            """,
            (schema,),
        )
        rows = []
        for name, table_type, row_count in cur.fetchall():
            upper = (table_type or "").upper()
            if "VIEW" in upper:
                kind = "materialized_view" if "MATERIALIZED" in upper else "view"
            else:
                kind = "table"
            rows.append({
                "name": name, "kind": kind,
                "est_rows": int(row_count) if row_count is not None else 0,
            })
        rows.sort(key=lambda r: (r["kind"] != "table", r["name"]))
        return rows

    try:
        return _run_meta(read)
    except HTTPException:
        raise
    except Exception as e:
        raise _meta_http_error(e, f"tables in {database}.{schema}")


def _source_object(database: str, schema: str, name: str) -> Dict[str, Any]:
    """What the copy source actually is, and how big it is.

    A view has no ROW_COUNT and cannot be built with CREATE TABLE ... LIKE, so
    the plan needs to know which it is dealing with before it writes any SQL.

    Cached, because a hand-picked copy of 70 objects looks each one up, and the
    preview and the confirmed start both build the same plan."""
    cache_key = f"sf-srcobj:{database}:{schema}:{name}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    def read(cur):
        cur.execute(
            f"""
            SELECT TABLE_TYPE, ROW_COUNT FROM {quote_ident(database)}.INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = ? AND TABLE_NAME = ?
            """,
            (schema, name),
        )
        return cur.fetchone()

    try:
        row = _run_meta(read)
    except Exception as e:
        logger.info("Source lookup skipped for %s.%s.%s: %s", database, schema, name, e)
        return {"kind": "table", "est_rows": None}

    if not row:
        return {"kind": "table", "est_rows": None}
    table_type = (row[0] or "").upper()
    kind = "table"
    if "VIEW" in table_type:
        kind = "materialized_view" if "MATERIALIZED" in table_type else "view"
    elif "EXTERNAL" in table_type:
        kind = "external_table"
    payload = {"kind": kind, "est_rows": int(row[1]) if row[1] is not None else None}
    _cache_set(cache_key, payload)
    return payload


def _table_row_estimate(database: str, schema: str, table: str) -> Optional[int]:
    return _source_object(database, schema, table)["est_rows"]


# ============================================================================
# Cross-profile copies
#
# The normal copy is one statement - INSERT INTO target SELECT FROM source -
# run by a single login that can see both sides. When reads and writes are two
# different logins, that statement cannot exist: whichever session runs it is
# blind to one half. Those copies go row by row through this process instead,
# read with one connection and inserted with the other.
#
# It is slower than anything Snowflake does internally, so it is only used when
# the write login genuinely cannot see the source - which is checked, not
# assumed.
# ============================================================================

_JSON_TYPES = {"VARIANT", "OBJECT", "ARRAY", "MAP"}
_BINARY_TYPES = {"BINARY", "VARBINARY"}
_GEO_TYPES = {"GEOGRAPHY", "GEOMETRY"}


def _writer_profile() -> str:
    """Whichever login owns the target. With no write profile configured that
    is the only login there is, which is how this always worked."""
    return WRITE_PROFILE if _write_configured() else READ_PROFILE


def _write_can_read(database: str, schema: str, table: Optional[str]) -> bool:
    """Can the write login see the source? A cross-profile copy is expensive,
    so it is worth one cheap DESCRIBE to find out whether it is needed."""
    if not _write_configured():
        return True
    target = (f"{quote_ident(database)}.{quote_ident(schema)}.{quote_ident(table)}" if table
              else f"{quote_ident(database)}.{quote_ident(schema)}")
    what = "TABLE" if table else "SCHEMA"

    # The preview and the confirmed start both plan the same copy, so without
    # this the probe runs twice for every job.
    cache_key = f"sf-writeread:{database}:{schema}:{table or ''}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return bool(cached)

    def probe(cur):
        cur.execute(f"DESCRIBE {what} {target}")
        cur.fetchone()
        return True

    try:
        allowed = bool(_run_meta(probe, WRITE_PROFILE))
    except Exception as e:
        logger.info("Write profile cannot read %s %s (%s) - copying through the API", what.lower(), target, e)
        allowed = False
    _cache_set(cache_key, allowed)
    return allowed


def _column_type_sql(col: Dict[str, Any]) -> str:
    """The column's type as it has to be written into a CREATE TABLE.

    CREATE TABLE ... LIKE would do this for us, but it needs one role that can
    see both tables, which is exactly what we do not have here."""
    data_type = (col.get("data_type") or "").upper()
    char_len = col.get("char_length")
    precision = col.get("precision")
    scale = col.get("scale")

    if data_type in ("TEXT", "VARCHAR", "STRING", "CHAR", "CHARACTER"):
        return f"VARCHAR({int(char_len)})" if char_len else "VARCHAR"
    if data_type in _BINARY_TYPES:
        return f"BINARY({int(char_len)})" if char_len else "BINARY"
    if data_type in ("NUMBER", "DECIMAL", "NUMERIC"):
        if precision is not None:
            return f"NUMBER({int(precision)},{int(scale or 0)})"
        return "NUMBER"
    if data_type in ("FLOAT", "FLOAT4", "FLOAT8", "DOUBLE", "REAL"):
        return "FLOAT"
    return data_type or "VARCHAR"


def _create_table_sql(tgt_q: str, columns: List[Dict[str, Any]], if_exists: str) -> str:
    verb = "CREATE OR REPLACE TABLE" if if_exists == "overwrite" else "CREATE TABLE"
    body = ",\n  ".join(
        f"{quote_ident(c['name'])} {_column_type_sql(c)}" + ("" if c.get("nullable", True) else " NOT NULL")
        for c in columns
    )
    return f"{verb} {tgt_q} (\n  {body}\n)"


def _transfer_expressions(columns: List[Dict[str, Any]]):
    """How each column leaves the source and how it goes back in.

    Most types survive the round trip as plain Python values. The ones that do
    not - semi-structured, binary and geospatial - travel as text and are
    rebuilt on the way in, which needs a function call around the bind.

    Returns (select_parts, rebuild_parts, needs_rebuild). `rebuild_parts` are
    written against the pseudo-columns of a VALUES clause (column1, column2...),
    because Snowflake does not accept function calls inside VALUES itself - the
    documented way round it is INSERT ... SELECT ... FROM VALUES."""
    select_parts, rebuild_parts = [], []
    needs_rebuild = False
    for i, col in enumerate(columns, start=1):
        name = quote_ident(col["name"])
        slot = f"column{i}"
        data_type = (col.get("data_type") or "").upper()
        if data_type in _JSON_TYPES:
            select_parts.append(f"TO_JSON({name})")
            rebuild_parts.append(f"PARSE_JSON({slot})")
            needs_rebuild = True
        elif data_type in _BINARY_TYPES:
            select_parts.append(f"HEX_ENCODE({name})")
            rebuild_parts.append(f"TO_BINARY({slot}, 'HEX')")
            needs_rebuild = True
        elif data_type == "GEOGRAPHY":
            select_parts.append(f"ST_ASWKT({name})")
            rebuild_parts.append(f"TO_GEOGRAPHY({slot})")
            needs_rebuild = True
        elif data_type == "GEOMETRY":
            select_parts.append(f"ST_ASWKT({name})")
            rebuild_parts.append(f"TO_GEOMETRY({slot})")
            needs_rebuild = True
        else:
            select_parts.append(name)
            rebuild_parts.append(slot)
    return select_parts, rebuild_parts, needs_rebuild


def _transfer_batch_rows(column_count: int, needs_rebuild: bool) -> int:
    """How many rows can go in one INSERT.

    Every value in the batch is a bind variable and Snowflake refuses a
    statement with more than ~16k of them, so a wide table takes fewer rows per
    round trip. A batch that has to be rebuilt column by column also makes a
    much longer statement, so it goes in smaller mouthfuls again."""
    ceiling = TRANSFER_BATCH_ROWS // 5 if needs_rebuild else TRANSFER_BATCH_ROWS
    return max(1, min(ceiling, TRANSFER_MAX_BINDS // max(1, column_count)))


def _transfer_unit(src_db, src_schema, src_table, tgt_db, tgt_schema, tgt_table,
                    est_rows, if_exists, columns=None) -> tuple:
    """Prep steps plus the single streaming unit for one cross-profile table."""
    columns = columns or _fetch_columns(src_db, src_schema, src_table, profile=READ_PROFILE)
    if not columns:
        raise HTTPException(status_code=404, detail=f"{src_schema}.{src_table} has no columns to copy")
    if est_rows and est_rows > TRANSFER_MAX_ROWS:
        raise HTTPException(
            status_code=400,
            detail=(f"{src_schema}.{src_table} has about {est_rows:,} rows. A copy between two different "
                    f"Snowflake logins goes through this API row by row, and that is capped at "
                    f"{TRANSFER_MAX_ROWS:,}. Either grant the write role read access on the source so "
                    f"Snowflake can do it internally, or copy the table in parts."),
        )

    src_q = qualified(src_db, src_schema, src_table)
    tgt_q = qualified(tgt_db, tgt_schema, tgt_table)
    select_parts, rebuild_parts, needs_rebuild = _transfer_expressions(columns)
    column_list = ", ".join(quote_ident(c["name"]) for c in columns)

    prep_steps: List[tuple] = []
    if if_exists in ("fail", "overwrite"):
        prep_steps.append((_create_table_sql(tgt_q, columns, if_exists), None,
                            f"Create structure for {tgt_schema}.{tgt_table} from the source columns"))

    unit = {
        "table": src_table, "target": tgt_table, "kind": "transfer_table",
        "sql": None, "params": None,
        "label": f"Transfer {src_schema}.{src_table} \u2192 {tgt_schema}.{tgt_table}",
        "source_database": src_db, "source_schema": src_schema,
        "target_database": tgt_db, "target_schema": tgt_schema,
        "select_sql": f"SELECT {', '.join(select_parts)} FROM {src_q}",
        "insert_prefix": (
            f"INSERT INTO {tgt_q} ({column_list}) SELECT {', '.join(rebuild_parts)} FROM VALUES "
            if needs_rebuild else
            f"INSERT INTO {tgt_q} ({column_list}) VALUES "
        ),
        "column_count": len(columns),
        "batch_rows": _transfer_batch_rows(len(columns), needs_rebuild),
        "est_rows": est_rows or 0,
    }
    return prep_steps, [unit]


# ============================================================================
# Recreating a view in another schema
#
# The view is taken as it is written - GET_DDL - and every reference to the
# source schema is pointed at the target one before it is run there. What it
# selects from has to exist in the target schema; if it does not, Snowflake
# says so and that error is what the job reports.
# ============================================================================

_IDENT = r'(?:"[^"]*"|[A-Za-z_][\w$]*)'
_QUALIFIED = rf'(?:{_IDENT}\s*\.\s*){{0,2}}{_IDENT}'

_VIEW_HEADER = re.compile(
    rf'''^\s*create\s+(?:or\s+replace\s+)?
         (?P<props>(?:secure\s+|recursive\s+|temporary\s+|temp\s+|volatile\s+)*)
         view\s+(?:if\s+not\s+exists\s+)?
         (?P<name>{_QUALIFIED})''',
    re.I | re.X,
)


# Snowflake stores a view's text as written, so whatever quoting goes in is
# what everyone reads back out of GET_DDL forever. A plain uppercase name needs
# no quotes and looks like the original did.
_BARE_IDENT = re.compile(r"^[A-Z_][A-Z0-9_$]*$")

# Quoting these would change nothing, but leaving them bare would break the
# statement, so they keep their quotes.
_RESERVED_IDENTS = {
    "ALL", "ALTER", "AND", "ANY", "AS", "BETWEEN", "BY", "CASE", "CAST", "CHECK", "COLUMN",
    "CONNECT", "CREATE", "CROSS", "CURRENT", "DELETE", "DISTINCT", "DROP", "ELSE", "EXISTS",
    "FALSE", "FOLLOWING", "FOR", "FROM", "FULL", "GRANT", "GROUP", "HAVING", "IN", "INNER",
    "INSERT", "INTERSECT", "INTO", "IS", "JOIN", "LATERAL", "LEFT", "LIKE", "NATURAL", "NOT",
    "NULL", "OF", "ON", "OR", "ORDER", "QUALIFY", "REVOKE", "RIGHT", "ROW", "ROWS", "SAMPLE",
    "SELECT", "SET", "SOME", "START", "TABLE", "TABLESAMPLE", "THEN", "TO", "TRIGGER", "TRUE",
    "UNION", "UNIQUE", "UPDATE", "USING", "VALUES", "WHEN", "WHENEVER", "WHERE", "WITH",
}


def _ddl_ident(name: str) -> str:
    """An identifier for text that gets stored and read back.

    Bare when it is a plain uppercase name - which is how Snowflake writes its
    own DDL - and quoted when the quotes actually carry meaning."""
    text = str(name or "")
    if _BARE_IDENT.match(text) and text not in _RESERVED_IDENTS:
        return text
    return quote_ident(text)


def _ddl_qualified(database: str, schema: str, name: str) -> str:
    return f"{_ddl_ident(database)}.{_ddl_ident(schema)}.{_ddl_ident(name)}"


def _ident_pattern(name: str) -> str:
    """Matches an identifier however the DDL happens to spell it - bare or
    double-quoted. Snowflake writes them unquoted and upper-cased, but a view
    created from quoted names keeps the quotes."""
    return f'(?:{re.escape(chr(34) + name + chr(34))}|{re.escape(name)})'


def _retarget_view_ddl(ddl: str, src_db: str, src_schema: str, src_name: str,
                        tgt_db: str, tgt_schema: str, tgt_name: str, if_exists: str):
    """Point a view's DDL at the target schema. Returns (sql, references_rewritten)."""
    body = (ddl or "").strip().rstrip(";").strip()
    if not body:
        raise HTTPException(status_code=404, detail=f"Snowflake returned no DDL for {src_schema}.{src_name}")

    header = _VIEW_HEADER.match(body)
    if not header:
        raise HTTPException(
            status_code=400,
            detail=f"The DDL for {src_schema}.{src_name} does not read as a CREATE VIEW - copy it by hand.",
        )

    verb = "CREATE OR REPLACE VIEW" if if_exists == "overwrite" else "CREATE VIEW"
    props = " ".join(header.group("props").split())
    if props:
        verb = verb.replace("VIEW", f"{props.upper()} VIEW")
    body = f"{verb} {_ddl_qualified(tgt_db, tgt_schema, tgt_name)}" + body[header.end("name"):]

    # Anything the view selects from moves schema with it. Fully qualified
    # names go first, so the two-part rule never chews half of a three-part one.
    rewritten = 0
    db_schema = re.compile(
        rf'(?<![\w$."]){_ident_pattern(src_db)}\s*\.\s*{_ident_pattern(src_schema)}\s*\.',
        re.I,
    )
    body, count = db_schema.subn(f"{_ddl_ident(tgt_db)}.{_ddl_ident(tgt_schema)}.", body)
    rewritten += count

    # A two-part reference is only rewritten where it can only be an object -
    # straight after FROM, JOIN and friends. Anywhere else "DQ_CORE." is just
    # as likely to be a table alias in front of a column, and renaming that
    # would quietly break the view.
    schema_only = re.compile(
        rf'(\b(?:from|join|into|update|table)\s+){_ident_pattern(src_schema)}\s*\.',
        re.I,
    )
    body, count = schema_only.subn(lambda m: f"{m.group(1)}{_ddl_ident(tgt_schema)}.", body)
    rewritten += count

    return body, rewritten


def _view_recreate_unit(src_db, src_schema, src_name, tgt_db, tgt_schema, tgt_name, if_exists):
    """A view copied as a view: its own DDL, re-pointed and run in the target."""
    if if_exists == "append":
        raise HTTPException(
            status_code=400,
            detail="'append' means adding rows, and a view holds none. Use 'Stop' or 'Overwrite'.",
        )
    ddl = _object_ddl(src_db, src_schema, src_name, "VIEW", READ_PROFILE)
    sql, rewritten = _retarget_view_ddl(ddl, src_db, src_schema, src_name,
                                         tgt_db, tgt_schema, tgt_name, if_exists)
    return [], [{
        "table": src_name, "target": tgt_name, "kind": "recreate_view",
        "sql": sql, "params": None,
        "label": f"Recreate view {src_schema}.{src_name} → {tgt_schema}.{tgt_name}",
        "target_database": tgt_db, "target_schema": tgt_schema,
        "references_rewritten": rewritten,
        # Phase 2: a recreated view reads from the target schema, so it can only
        # compile once everything else in this job has landed there.
        "phase": COPY_PHASE_VIEWS,
        "est_rows": 0,
    }]


def _copy_table_units(src_db, src_schema, src_table, tgt_db, tgt_schema, tgt_table,
                       est_rows, mode, if_exists, parallelism, source_kind="table", strategy=None):
    """Prep statements (run once, sequentially) plus the parallel units for one
    table. Returns (prep_steps, units)."""
    src_q = qualified(src_db, src_schema, src_table)
    tgt_q = qualified(tgt_db, tgt_schema, tgt_table)

    # A view is a query, not storage: it has no structure to copy with LIKE and
    # no micro-partitions to clone. It lands as a table built from its own
    # result, in one statement - splitting that by hash would re-run the view
    # once per chunk, which is the opposite of faster.
    if source_kind in ("view", "materialized_view"):
        # Clone has no meaning for a view - there is nothing stored to clone -
        # so that choice recreates the view itself instead, which is what
        # "copy this object as it is" means for a view.
        if strategy == "view_ddl" or (strategy is None and mode == "clone"):
            return _view_recreate_unit(src_db, src_schema, src_table, tgt_db, tgt_schema, tgt_table, if_exists)
        if if_exists == "append":
            unit_sql = f"INSERT INTO {tgt_q} SELECT * FROM {src_q}"
            label = f"Append {src_schema}.{src_table} \u2192 {tgt_schema}.{tgt_table}"
        else:
            verb = "CREATE OR REPLACE TABLE" if if_exists == "overwrite" else "CREATE TABLE"
            unit_sql = f"{verb} {tgt_q} AS SELECT * FROM {src_q}"
            label = f"Materialise view {src_schema}.{src_table} \u2192 {tgt_schema}.{tgt_table}"
        return [], [{
            "table": src_table, "target": tgt_table, "kind": "view_to_table",
            "sql": unit_sql, "params": None, "label": label,
            "target_database": tgt_db, "target_schema": tgt_schema,
            "est_rows": est_rows or 0,
        }]

    if mode == "clone":
        verb = "CREATE OR REPLACE TABLE" if if_exists == "overwrite" else "CREATE TABLE"
        unit = {
            "table": src_table, "target": tgt_table, "kind": "clone_table",
            "sql": f"{verb} {tgt_q} CLONE {src_q}", "params": None,
            "label": f"Clone {src_schema}.{src_table} \u2192 {tgt_schema}.{tgt_table}",
            "target_database": tgt_db, "target_schema": tgt_schema,
            "est_rows": est_rows or 0,
        }
        return [], [unit]

    prep_steps: List[tuple] = []
    if if_exists == "overwrite":
        prep_steps.append((f"CREATE OR REPLACE TABLE {tgt_q} LIKE {src_q}", None,
                            f"Recreate structure for {tgt_schema}.{tgt_table}"))
    elif if_exists == "fail":
        prep_steps.append((f"CREATE TABLE {tgt_q} LIKE {src_q}", None,
                            f"Create structure for {tgt_schema}.{tgt_table}"))
    # if_exists == "append": the target table is expected to already exist.

    chunk_count = _plan_chunks(est_rows, parallelism)
    units = []
    for i in range(chunk_count):
        if chunk_count == 1:
            sql, params = f"INSERT INTO {tgt_q} SELECT * FROM {src_q}", None
        else:
            sql = f"INSERT INTO {tgt_q} SELECT * FROM {src_q} WHERE ABS(HASH(*)) % ? = ?"
            params = (chunk_count, i)
        units.append({
            "table": src_table, "target": tgt_table, "kind": "insert_chunk",
            "sql": sql, "params": params,
            "label": f"Copy {src_schema}.{src_table} chunk {i + 1}/{chunk_count} \u2192 {tgt_schema}.{tgt_table}",
            "target_database": tgt_db, "target_schema": tgt_schema,
            "est_rows": (est_rows or 0) // chunk_count,
        })
    return prep_steps, units


def _schema_exists(database: str, schema: str) -> Optional[bool]:
    """True, False, or None when the write login can't tell us."""
    def read(cur):
        cur.execute(
            f"SELECT 1 FROM {quote_ident(database)}.INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = ?",
            (schema,),
        )
        return cur.fetchone() is not None

    try:
        return _run_meta(read, _writer_profile())
    except Exception as e:
        logger.info("Schema check skipped for %s.%s: %s", database, schema, e)
        return None


def _ensure_schema_steps(database: str, schema: str) -> List[tuple]:
    """A CREATE SCHEMA step, but only when the schema is really missing.

    The target schema can be typed by hand, so a copy has to be able to create
    it - and firing CREATE SCHEMA IF NOT EXISTS at one that already exists is
    not free: the role still needs CREATE SCHEMA on the database, which a
    write role scoped to one schema usually does not have."""
    # Unknown counts as "leave it alone" - better a clear "schema does not
    # exist" from Snowflake than a statement that trips on privileges.
    if _schema_exists(database, schema) is not False:
        return []
    return [(
        f"CREATE SCHEMA IF NOT EXISTS {quote_ident(database)}.{quote_ident(schema)}",
        None, f"Create schema {database}.{schema}",
    )]


def _copy_route(body: CopyRequest, first_table: Optional[str]) -> str:
    """"server" when one login can run the copy inside Snowflake, "transfer"
    when the data has to come through this process because the write login
    cannot see the source."""
    if not _write_configured():
        return "server"
    probe_table = body.source_table if body.copy_type == "table" else first_table
    if probe_table and _write_can_read(body.source_database, body.source_schema, probe_table):
        return "server"
    if not probe_table:
        return "server"                     # nothing to probe - nothing to copy either
    return "transfer"


def _settle_view_strategies(items: List[Dict[str, Any]], mode: str):
    """Decide, per object, whether a view stays a view.

    When everything picked is a view, the mode pills are about views and the
    choice is the user's: recreate it, or write out what it returns. When the
    selection also holds tables, those pills are about the tables - so a view
    keeps its nature rather than quietly arriving as a table."""
    views = [i for i in items if i.get("kind") in ("view", "materialized_view")]
    if not views:
        return
    views_only = len(views) == len(items)
    for item in views:
        item["strategy"] = "view" if (views_only and mode == "insert") else "view_ddl"


def _build_copy_plan(body: CopyRequest, target_table: Optional[str]) -> Dict[str, Any]:
    if body.copy_type == "table":
        source = _source_object(body.source_database, body.source_schema, body.source_table)
        items = [{"name": body.source_table, "target": target_table, "est_rows": source["est_rows"] or 0,
                  "strategy": body.mode, "kind": source["kind"]}]
    elif body.copy_type == "tables":
        # A hand-picked set. Each one is looked up on its own because the
        # selection can mix tables and views, and they are copied differently.
        names = [n.strip() for n in (body.source_tables or []) if (n or "").strip()]
        if len(names) > MAX_TABLES_PER_SCHEMA_COPY:
            raise HTTPException(
                status_code=400,
                detail=(f"{len(names)} objects selected - that's more than one copy job can take on "
                         f"({MAX_TABLES_PER_SCHEMA_COPY} max). Copy in smaller batches."),
            )
        # Past a handful, one listing of the schema is cheaper than a lookup
        # per name - the same reason the column read is done in bulk below.
        catalogue = {}
        if len(names) > 5:
            try:
                catalogue = {t["name"]: t for t in
                             _enumerate_source_tables(body.source_database, body.source_schema)}
            except HTTPException as e:
                logger.info("Bulk object listing unavailable (%s) - falling back per object", e.detail)

        items = []
        for name in names:
            listed = catalogue.get(name)
            source = ({"kind": listed["kind"], "est_rows": listed["est_rows"]} if listed
                      else _source_object(body.source_database, body.source_schema, name))
            items.append({"name": name, "target": name, "est_rows": source["est_rows"] or 0,
                          "strategy": body.mode, "kind": source["kind"]})
    else:
        tables = _enumerate_source_tables(body.source_database, body.source_schema)
        if len(tables) > MAX_TABLES_PER_SCHEMA_COPY:
            raise HTTPException(
                status_code=400,
                detail=(f"{len(tables)} tables in {body.source_schema} - that's more than one copy job can "
                         f"take on ({MAX_TABLES_PER_SCHEMA_COPY} max). Copy in smaller batches."),
            )
        items = [{"name": t["name"], "target": t["name"], "est_rows": t["est_rows"],
                  "strategy": body.mode, "kind": t["kind"]} for t in tables]

    _settle_view_strategies(items, body.mode)

    prep_steps: List[tuple] = []
    units: List[Dict[str, Any]] = []
    warnings: List[str] = []

    # Recreating a view moves no rows: the read login supplies the DDL and the
    # write login runs it, whatever either can or cannot see of the other side.
    # Only a selection that is nothing but views can skip the route check.
    recreating_views = bool(items) and all(i["strategy"] == "view_ddl" for i in items)

    route = "view_ddl" if recreating_views else _copy_route(body, items[0]["name"] if items else None)
    if route == "transfer":
        # A view is recreated from its DDL either way: that needs no access to
        # the other side's data, so the two-login problem never applies to it.
        # Only a table actually has to be cloned, and that is what cannot work.
        if body.mode == "clone" and any(i["strategy"] != "view_ddl" for i in items):
            raise HTTPException(
                status_code=400,
                # A code as well as the sentence: the screen offers to make this
                # exact change rather than asking someone to go and find it.
                detail={
                    "code": "clone_needs_one_role",
                    "message": (
                        f"Clone needs one Snowflake role that can see both sides, and the write login "
                        f"({_profile_label(WRITE_PROFILE)}) cannot read "
                        f"{body.source_database}.{body.source_schema}. Copy rows instead and the data is "
                        f"carried across by the portal - or grant the write role read access on the source."
                    ),
                },
            )
        writer = _profile_label(WRITE_PROFILE)
        reader = _profile_label(READ_PROFILE)
        if any(i["strategy"] != "view_ddl" for i in items):
            warnings.append(
                f"The write login ({writer}) cannot read {body.source_database}.{body.source_schema}, so the "
                f"rows are read as {reader} and inserted as {writer} through this API. That is slower than a "
                f"copy inside Snowflake and it holds both connections for the whole job."
            )
        # One read for every column in the schema beats one read per object.
        column_map = (_fetch_schema_columns(body.source_database, body.source_schema)
                      if len(items) > 1 else {})
        for item in items:
            if item["strategy"] == "view_ddl":
                table_prep, table_units = _view_recreate_unit(
                    body.source_database, body.source_schema, item["name"],
                    body.target_database, body.target_schema, item["target"],
                    body.if_exists if body.if_exists != "append" else "overwrite",
                )
            else:
                table_prep, table_units = _transfer_unit(
                    body.source_database, body.source_schema, item["name"],
                    body.target_database, body.target_schema, item["target"],
                    item["est_rows"], body.if_exists,
                    columns=column_map.get(item["name"]),
                )
                item["strategy"] = "transfer"
            prep_steps.extend(table_prep)
            units.extend(table_units)
            item["est_chunks"] = len(table_units)

        for step in reversed(_ensure_schema_steps(body.target_database, body.target_schema)):
            prep_steps.insert(0, step)
        return {
            "items": items, "prep_steps": prep_steps, "units": units, "route": route,
            "totals": {"tables": len(items), "est_rows": sum(i["est_rows"] for i in items)},
            "warnings": warnings,
        }

    # CREATE SCHEMA ... CLONE builds the schema itself, so it can only run when
    # there is no schema there yet - or when the caller has asked to replace the
    # whole thing. Cloning into a schema that already exists means cloning its
    # tables into it one by one, which is what falling through does.
    target_schema_exists = (
        _schema_exists(body.target_database, body.target_schema)
        if body.copy_type == "schema" and body.mode == "clone" else None
    )
    if body.copy_type == "schema" and body.mode == "clone" and target_schema_exists and body.if_exists == "overwrite":
        warnings.append(
            f"{body.target_database}.{body.target_schema} already exists and will be dropped and replaced "
            "by the clone - everything currently in it goes with it."
        )

    whole_schema_clone = (
        body.copy_type == "schema" and body.mode == "clone" and items
        and (body.if_exists == "overwrite" or target_schema_exists is False)
    )
    if whole_schema_clone:
        verb = "CREATE OR REPLACE SCHEMA" if body.if_exists == "overwrite" else "CREATE SCHEMA"
        src_q = f"{quote_ident(body.source_database)}.{quote_ident(body.source_schema)}"
        tgt_q = f"{quote_ident(body.target_database)}.{quote_ident(body.target_schema)}"
        units.append({
            "table": "__schema__", "target": body.target_schema, "kind": "clone_schema",
            "sql": f"{verb} {tgt_q} CLONE {src_q}", "params": None,
            "label": f"Clone schema {body.source_database}.{body.source_schema} \u2192 "
                     f"{body.target_database}.{body.target_schema} - one statement covering "
                     f"{len(items)} table(s) and everything else in the schema",
            "target_database": body.target_database, "target_schema": body.target_schema,
            "est_rows": sum(i["est_rows"] for i in items),
        })
        # CLONE copies a view's definition as it is written, so a cloned view
        # still selects from the schema it came from. Recreating each one
        # afterwards with its references re-pointed is what makes the copy
        # stand on its own - and phase 2 means it happens after the clone.
        for item in items:
            item["est_chunks"] = 1
            if item["kind"] not in ("view", "materialized_view"):
                continue
            try:
                _, view_units = _view_recreate_unit(
                    body.source_database, body.source_schema, item["name"],
                    body.target_database, body.target_schema, item["name"], "overwrite",
                )
            except HTTPException as exc:
                # A view whose DDL cannot be read (a secure view someone else
                # owns) still gets cloned; it just keeps pointing at the source.
                warnings.append(f"{item['name']}: {exc.detail}")
                item["strategy"] = "clone"
                continue
            units.extend(view_units)
            item["strategy"] = "view_ddl"

        return {
            "items": items, "prep_steps": [], "units": units, "route": route,
            "single_statement": True,
            "totals": {"tables": len(items), "est_rows": sum(i["est_rows"] for i in items)},
            "warnings": warnings,
        }

    prep_steps.extend(_ensure_schema_steps(body.target_database, body.target_schema))

    for item in items:
        table_prep, table_units = _copy_table_units(
            body.source_database, body.source_schema, item["name"],
            body.target_database, body.target_schema, item["target"],
            item["est_rows"], body.mode,
            body.if_exists if not (item["strategy"] == "view_ddl" and body.if_exists == "append") else "overwrite",
            body.parallelism,
            source_kind=item.get("kind", "table"), strategy=item["strategy"],
        )
        prep_steps.extend(table_prep)
        units.extend(table_units)
        item["est_chunks"] = len(table_units)

    return {
        "items": items,
        "prep_steps": prep_steps,
        "units": units,
        "route": route,
        "totals": {"tables": len(items), "est_rows": sum(i["est_rows"] for i in items)},
        "warnings": warnings,
    }


def _copy_endpoint_label(database: str, schema: str, name: Optional[str]) -> str:
    """db.schema for a whole-schema or multi-object job, db.schema.name for one."""
    return f"{database}.{schema}" + (f".{name}" if name else "")


def _new_copy_job(body: CopyRequest, plan: Dict[str, Any], owner: str) -> Dict[str, Any]:
    job_id = uuid.uuid4().hex
    objects_total = len(plan["items"])
    tables = {}
    for unit in plan["units"]:
        if unit["table"] == "__schema__":
            # One statement covering the whole schema. Naming it after the first
            # table in the list - which is what it used to do - reads like that
            # one table is being copied and the other 84 are missing.
            row_name = f"Whole schema · {objects_total} object{'' if objects_total == 1 else 's'}"
        else:
            row_name = unit["table"]
        t = tables.setdefault(unit["table"], {
            "name": row_name,
            "target": unit["target"], "status": "pending",
            "est_rows": 0, "rows_copied": 0, "chunks_total": 0, "chunks_done": 0, "error": None,
        })
        t["est_rows"] += unit.get("est_rows", 0)
        t["chunks_total"] += 1

    job = {
        "job_id": job_id,
        "owner": (owner or "").strip().lower(),
        "copy_type": body.copy_type,
        "mode": body.mode,
        "route": plan.get("route", "server"),
        "if_exists": body.if_exists,
        "source": _copy_endpoint_label(body.source_database, body.source_schema, body.source_table
                                       if body.copy_type == "table" else None),
        "target": _copy_endpoint_label(body.target_database, body.target_schema, body.target_table
                                       if body.copy_type == "table" else None),
        "target_database": body.target_database,
        "target_schema": body.target_schema,
        "status": "running",   # running | success | failed | cancelled | partial
        "tables": tables,
        # Steps are what the progress list shows: a whole-schema clone is one
        # step covering many objects, so the two counts are not the same number
        # and the payload carries both.
        "tables_total": len(tables),
        "objects_total": objects_total,
        "tables_done": 0,
        "tables_failed": 0,
        "logs": [],
        "lock": threading.Lock(),
        "cancel_requested": False,
        "active_cursors": [],
        "created_at": time.time(),
        "started_at": time.time(),
        "finished_at": None,
        "updated_at": time.time(),
        "done": threading.Event(),
        # Set whenever anything about the job changes, so a watcher can wait on
        # it instead of polling.
        "pulse": threading.Event(),
    }
    with _copy_jobs_lock:
        _copy_jobs[job_id] = job
    return job


def _get_copy_job(job_id: str, owner: Optional[str] = None) -> Dict[str, Any]:
    with _copy_jobs_lock:
        job = _copy_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Unknown or expired copy job")
    if owner is not None and job["owner"] and job["owner"] != (owner or "").strip().lower():
        raise HTTPException(status_code=404, detail="Unknown or expired copy job")
    return job


def cleanup_copy_jobs():
    cutoff = time.time() - COPY_JOB_TTL_SECONDS
    with _copy_jobs_lock:
        stale = [jid for jid, j in _copy_jobs.items() if j["status"] != "running" and j["created_at"] < cutoff]
        for jid in stale:
            _copy_jobs.pop(jid, None)


def _stream_table(job: Dict[str, Any], unit: Dict[str, Any], table_state: Dict[str, Any]) -> int:
    """Read one table with the read login and insert it with the write one.

    Rows are counted into the job as each batch lands rather than at the end,
    so a long transfer shows a progress bar that actually moves."""
    batch_rows = unit["batch_rows"]
    row_values = "(" + ", ".join("?" * unit["column_count"]) + ")"
    copied = 0

    with _pooled(unit["source_database"], unit["source_schema"], profile=READ_PROFILE) as read_conn, \
         _pooled(unit["target_database"], unit["target_schema"], profile=WRITE_PROFILE) as write_conn:
        read_cur = read_conn.cursor()
        write_cur = write_conn.cursor()
        with job["lock"]:
            job["active_cursors"].extend([read_cur, write_cur])
        try:
            copy_timeout = _require_config()["copy_timeout_seconds"]
            _apply_statement_timeout(read_conn, read_cur, copy_timeout)
            _apply_statement_timeout(write_conn, write_cur, copy_timeout)
            read_cur.execute(unit["select_sql"])
            while True:
                if job["cancel_requested"]:
                    raise _Cancelled()
                rows = read_cur.fetchmany(batch_rows)
                if not rows:
                    break
                # One statement per batch, not per row - a round trip for every
                # row of a million-row table is the difference between minutes
                # and hours.
                sql = unit["insert_prefix"] + ", ".join([row_values] * len(rows))
                params = [value for row in rows for value in row]
                write_cur.execute(sql, params)
                copied += len(rows)
                # The planner checks ROW_COUNT before starting, but a view has
                # none to check - so the ceiling is enforced here as well.
                if copied > TRANSFER_MAX_ROWS:
                    raise HTTPException(
                        status_code=400,
                        detail=(f"{unit['label']} passed {TRANSFER_MAX_ROWS:,} rows, the limit for a copy that "
                                "has to go through this API. The rows written so far are still there - narrow "
                                "the source, or grant the write role read access so Snowflake can do it itself."),
                    )
                with job["lock"]:
                    table_state["rows_copied"] += len(rows)
                _copy_touch(job)
        finally:
            with job["lock"]:
                job["active_cursors"] = [c for c in job["active_cursors"]
                                          if c is not read_cur and c is not write_cur]
            for cur in (read_cur, write_cur):
                try:
                    cur.close()
                except Exception:
                    pass
    return copied


def _run_copy_unit(job: Dict[str, Any], unit: Dict[str, Any]):
    if job["cancel_requested"]:
        return
    _copy_chunk_slots.acquire()
    table_state = job["tables"].get(unit["table"]) if unit["table"] != "__schema__" else next(iter(job["tables"].values()))
    try:
        if job["cancel_requested"]:
            return
        with job["lock"]:
            if table_state["status"] == "pending":
                table_state["status"] = "running"

        if unit["kind"] == "transfer_table":
            try:
                rows = _stream_table(job, unit, table_state)
            except _Cancelled:
                _copy_log(job, "warn", f"cancelled - {unit['label']}")
                raise
            except Exception as e:
                with job["lock"]:
                    table_state["status"] = "failed"
                    table_state["error"] = str(e)
                _copy_log(job, "error", f"failed - {unit['label']}: {e}")
                raise
            with job["lock"]:
                table_state["chunks_done"] += 1
            _copy_log(job, "info", f"done - {unit['label']} ({rows:,} rows)")
            return

        conn, key, created = _pool.acquire(unit["target_database"], unit["target_schema"],
                                           profile=_writer_profile())
        healthy = True
        cur = None
        try:
            cur = conn.cursor()
            with job["lock"]:
                job["active_cursors"].append(cur)
            # A clone of 78 tables, or an INSERT of a hundred million rows, is
            # allowed to take its time.
            _apply_statement_timeout(conn, cur, _require_config()["copy_timeout_seconds"])
            if unit["kind"] == "recreate_view":
                # The statement is the whole point of this one - show it, so a
                # failure can be read against exactly what was run.
                _copy_log(job, "info",
                          f"{unit['references_rewritten']} reference(s) re-pointed at the target schema")
                _copy_log(job, "info", unit["sql"])
            cur.execute(unit["sql"], unit["params"])
            rows = cur.rowcount if isinstance(cur.rowcount, int) and cur.rowcount > 0 else 0
            if unit["kind"] in ("clone_table", "clone_schema"):
                rows = unit.get("est_rows", 0)  # a clone is instant; report the estimate as "copied"
            with job["lock"]:
                table_state["rows_copied"] += rows
                table_state["chunks_done"] += 1
                job["active_cursors"] = [c for c in job["active_cursors"] if c is not cur]
            _copy_touch(job)
            if unit["kind"] == "recreate_view":
                _copy_log(job, "info", f"done - {unit['label']}")
            else:
                _copy_log(job, "info", f"done - {unit['label']} ({rows:,} rows)")
        except Exception as e:
            healthy = not _looks_stale(e)
            with job["lock"]:
                table_state["status"] = "failed"
                table_state["error"] = str(e)
            _copy_log(job, "error", f"failed - {unit['label']}: {e}")
            raise
        finally:
            if cur is not None:
                try:
                    cur.close()
                except Exception:
                    pass
            if healthy:
                _pool.release(conn, key, created)
            else:
                _quiet_close(conn)
    finally:
        _copy_chunk_slots.release()
        # Several chunks of the same table can hit this at nearly the same
        # instant - "_finalized" makes the done/failed count exactly once
        # per table no matter how the threads interleave.
        with job["lock"]:
            if not table_state.get("_finalized"):
                if table_state["status"] == "failed":
                    table_state["_finalized"] = True
                    job["tables_failed"] += 1
                elif table_state["chunks_done"] >= table_state["chunks_total"]:
                    table_state["status"] = "success"
                    table_state["_finalized"] = True
                    job["tables_done"] += 1
                    _drop_table_cache(unit["target_database"], unit["target_schema"])


def _reset_failed_unit(job: Dict[str, Any], unit: Dict[str, Any]):
    """Put a failed object back to pending so it can be tried once more."""
    table_state = job["tables"].get(unit["table"])
    if not table_state:
        return
    with job["lock"]:
        if table_state.get("_finalized") and table_state["status"] == "failed":
            table_state["_finalized"] = False
            table_state["status"] = "pending"
            table_state["error"] = None
            job["tables_failed"] = max(0, job["tables_failed"] - 1)


def _run_view_units(job: Dict[str, Any], view_units: List[Dict[str, Any]]):
    """Recreate views, retrying the ones that failed.

    A view built on another view only compiles once that one exists, and
    nothing here knows which depends on which - so a pass that manages to
    create at least one view is worth repeating for the rest. Passes stop as
    soon as one makes no progress: that failure is real."""
    pending = list(view_units)
    for attempt in range(3):
        if not pending or job["cancel_requested"]:
            break
        if attempt:
            _copy_log(job, "info", f"Retrying {len(pending)} view(s) - a view they read from may "
                                   "only have been created in the pass before")
            for unit in pending:
                _reset_failed_unit(job, unit)

        failed = []
        for unit in pending:
            if job["cancel_requested"]:
                break
            try:
                _run_copy_unit(job, unit)
            except Exception:
                failed.append(unit)

        if len(failed) == len(pending):
            break                       # nothing moved; another pass changes nothing
        pending = failed


def _run_copy_job(job_id: str, prep_steps: List[tuple], units: List[Dict[str, Any]]):
    job = _copy_jobs.get(job_id)
    if not job:
        return
    how = {
        "transfer": "row transfer between two logins",
        "view_ddl": "view recreated from its own DDL",
    }.get(job.get("route"), job["mode"])
    objects = job.get("objects_total") or job["tables_total"]
    _copy_log(job, "info", f"Starting {job['copy_type']} copy: {job['source']} \u2192 {job['target']} "
                           f"({how}, {objects} object{'' if objects == 1 else 's'} "
                           f"in {job['tables_total']} step{'' if job['tables_total'] == 1 else 's'})")
    if job.get("route") == "transfer":
        _copy_log(job, "warn", f"Reading as {_profile_label(READ_PROFILE)}, writing as "
                               f"{_profile_label(WRITE_PROFILE)} - rows pass through the API")
    try:
        for sql, params, label in prep_steps:
            if job["cancel_requested"]:
                raise _Cancelled()
            _copy_log(job, "info", label)
            try:
                with _pooled(job["target_database"], profile=_writer_profile()) as conn:
                    cur = conn.cursor()
                    try:
                        _apply_statement_timeout(conn, cur, _require_config()["copy_timeout_seconds"])
                        cur.execute(sql, params)
                    finally:
                        cur.close()
            except Exception as e:
                _copy_log(job, "error", f"{label} failed: {e}")
                raise

        if job["cancel_requested"]:
            raise _Cancelled()

        # Tables first, views after - one object or seventy, hand-picked or a
        # whole schema. A recreated view points at the target schema, so
        # whatever it selects from has to be there before it runs.
        data_units = [u for u in units if u.get("phase", COPY_PHASE_DATA) == COPY_PHASE_DATA]
        view_units = [u for u in units if u.get("phase", COPY_PHASE_DATA) == COPY_PHASE_VIEWS]

        futures = [_executor.submit(_run_copy_unit, job, unit) for unit in data_units]
        for fut in futures:
            try:
                fut.result()
            except Exception as e:
                logger.info("Copy unit failed for job %s: %s", job_id, e)

        if view_units and not job["cancel_requested"]:
            _run_view_units(job, view_units)

        with job["lock"]:
            statuses = {t["status"] for t in job["tables"].values()}
            if job["cancel_requested"]:
                job["status"] = "cancelled"
            elif statuses == {"failed"}:
                job["status"] = "failed"
            elif "failed" in statuses:
                job["status"] = "partial"
            else:
                job["status"] = "success"
            total_rows = sum(t["rows_copied"] for t in job["tables"].values())
        tail = "" if job.get("route") == "view_ddl" else f", {total_rows:,} rows copied"
        _copy_log(job, "success" if job["status"] == "success" else "warn",
                  f"Copy {job['status']} - {job['tables_done']}/{job['tables_total']} step(s){tail}")
    except _Cancelled:
        with job["lock"]:
            job["status"] = "cancelled"
        _copy_log(job, "warn", "Copy cancelled")
    except HTTPException as e:
        with job["lock"]:
            job["status"] = "failed"
        _copy_log(job, "error", f"Copy failed: {e.detail}")
    except Exception as e:
        with job["lock"]:
            job["status"] = "failed"
        _copy_log(job, "error", f"Copy failed: {e}")
    finally:
        job["finished_at"] = time.time()
        job["done"].set()
        _copy_touch(job)
        try:
            from audit import write_audit_event
            write_audit_event("sfqr_table_copy", job["owner"], job["target"],
                               f"{job['status']}: {job['source']} -> {job['target']} ({job['mode']})")
        except Exception:
            pass


def _copy_job_payload(job: Dict[str, Any], since_log: int = 0) -> Dict[str, Any]:
    with job["lock"]:
        tables = [
            {k: v for k, v in t.items() if not k.startswith("_")}
            for t in job["tables"].values()
        ]
        all_logs = list(job["logs"])
    rows_copied = sum(t["rows_copied"] for t in tables)
    est_rows_total = sum(t["est_rows"] for t in tables)
    percent = round(min(100.0, rows_copied / est_rows_total * 100), 1) if est_rows_total else (
        100.0 if job["status"] in ("success", "partial") else None
    )
    elapsed_ms = int(((job["finished_at"] or time.time()) - job["started_at"]) * 1000)
    return {
        "job_id": job["job_id"], "status": job["status"],
        "copy_type": job["copy_type"], "mode": job["mode"], "route": job.get("route", "server"),
        "source": job["source"], "target": job["target"],
        "tables_total": job["tables_total"], "tables_done": job["tables_done"],
        "tables_failed": job["tables_failed"],
        "objects_total": job.get("objects_total") or job["tables_total"],
        "rows_copied": rows_copied, "est_rows_total": est_rows_total, "percent": percent,
        "elapsed_ms": elapsed_ms,
        "tables": tables,
        "logs": all_logs[since_log:],
        "log_count": len(all_logs),
    }


@snowflake_router.post("/copy/start")
def start_copy(body: CopyRequest, auth: dict = Depends(sf_auth)):
    _require(auth, PERM_COPY, "copy tables")
    cleanup_copy_jobs()
    cfg = _require_config()
    if not cfg["allow_writes"]:
        raise HTTPException(status_code=403, detail="This deployment's Data Studio (Snowflake) is configured read-only")
    if body.copy_type not in ("table", "tables", "schema"):
        raise HTTPException(status_code=400, detail="copy_type must be 'table', 'tables' or 'schema'")
    if body.mode not in ("clone", "insert"):
        raise HTTPException(status_code=400, detail="mode must be 'clone' or 'insert'")
    if body.if_exists not in ("fail", "overwrite", "append"):
        raise HTTPException(status_code=400, detail="if_exists must be 'fail', 'overwrite' or 'append'")
    if body.mode == "clone" and body.if_exists == "append":
        raise HTTPException(
            status_code=400,
            detail="'append' isn't supported in clone mode - a clone always creates a new object. "
                   "Use 'overwrite', or switch to insert mode to add rows to an existing table.",
        )
    if body.copy_type == "table" and not (body.source_table or "").strip():
        raise HTTPException(status_code=400, detail="source_table is required for a table copy")
    if body.copy_type == "tables" and not [n for n in (body.source_tables or []) if (n or "").strip()]:
        raise HTTPException(status_code=400, detail="Pick at least one table or view to copy")
    if not (body.source_database or "").strip() or not (body.source_schema or "").strip():
        raise HTTPException(status_code=400, detail="A source database and schema are required")
    if not (body.target_database or "").strip() or not (body.target_schema or "").strip():
        raise HTTPException(status_code=400, detail="A target database and schema are required")

    target_table = (body.target_table or body.source_table or "").strip() or None
    if body.copy_type == "table":
        same_location = (
            body.source_database == body.target_database
            and body.source_schema == body.target_schema
            and (target_table or "") == (body.source_table or "")
        )
        if same_location:
            raise HTTPException(status_code=400, detail="Source and target are the same table")
    else:
        # A multi-select keeps every name, so landing back in the same schema
        # would mean copying each object onto itself.
        if body.source_database == body.target_database and body.source_schema == body.target_schema:
            raise HTTPException(status_code=400, detail="Source and target are the same schema")

    plan = _build_copy_plan(body, target_table)

    if not body.confirm:
        return {
            "requires_confirmation": True,
            "copy_type": body.copy_type, "mode": body.mode, "if_exists": body.if_exists,
            "source": _copy_endpoint_label(body.source_database, body.source_schema, body.source_table
                                           if body.copy_type == "table" else None),
            "target": _copy_endpoint_label(body.target_database, body.target_schema, target_table
                                           if body.copy_type == "table" else None),
            "items": plan["items"],
            "totals": plan["totals"],
            "warnings": plan["warnings"],
            # "server" = one statement inside Snowflake, "transfer" = the rows
            # come through this API because the two logins see different things.
            "route": plan.get("route", "server"),
            # A whole-schema clone is one statement covering every object in the
            # schema - views and the rest included, not only the tables listed.
            "single_statement": bool(plan.get("single_statement")),
        }

    if not plan["items"]:
        raise HTTPException(status_code=400, detail="Nothing to copy - the source schema has no tables")

    job = _new_copy_job(body, plan, auth["user"]["email"])
    try:
        _executor.submit(_run_copy_job, job["job_id"], plan["prep_steps"], plan["units"])
    except Exception as e:
        with _copy_jobs_lock:
            _copy_jobs.pop(job["job_id"], None)
        raise HTTPException(status_code=503, detail=f"Could not schedule the copy: {e}")

    return {"job_id": job["job_id"], "status": "running", "tables_total": job["tables_total"]}


@snowflake_router.get("/copy/{job_id}/status")
def get_copy_status(
    job_id: str,
    wait_ms: int = Query(0, ge=0, le=15000),
    since_log: int = Query(0, ge=0),
    auth: dict = Depends(sf_auth),
):
    job = _get_copy_job(job_id, auth["user"]["email"])
    if wait_ms and job["status"] == "running":
        job["done"].wait(wait_ms / 1000.0)
    job["updated_at"] = time.time()
    return _copy_job_payload(job, since_log=since_log)


def _copy_sse(event: str, payload: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


@snowflake_router.get("/copy/{job_id}/stream")
def stream_copy(
    job_id: str,
    since_log: int = Query(0, ge=0),
    auth: dict = Depends(sf_auth),
):
    """The job as it happens, rather than as it was 4 seconds ago.

    Polling for a copy meant every log line waited for the next request, which
    is a long time to stare at nothing while 78 tables clone. This waits on the
    job's own pulse instead: a line written is a line sent."""
    job = _get_copy_job(job_id, auth["user"]["email"])

    def events():
        cursor = since_log
        last_progress = 0.0
        heartbeat = time.time()
        while True:
            # Cleared before reading, so an update that lands while this frame
            # is being built sets it again and the next wait returns at once.
            job["pulse"].clear()
            payload = _copy_job_payload(job, since_log=cursor)
            now = time.time()

            if payload["logs"]:
                yield _copy_sse("logs", {"logs": payload["logs"], "log_count": payload["log_count"]})
                cursor = payload["log_count"]
                heartbeat = now

            # Row counts move far faster than anyone can read, so the numbers
            # go out on a tick rather than on every batch.
            if now - last_progress >= 0.25 or payload["status"] != "running":
                progress = {k: v for k, v in payload.items() if k != "logs"}
                yield _copy_sse("progress", progress)
                last_progress = now
                heartbeat = now

            if payload["status"] != "running":
                yield _copy_sse("done", {"status": payload["status"]})
                return

            if now - heartbeat >= 15:
                yield ": keep-alive\n\n"      # proxies drop a silent stream
                heartbeat = now

            job["pulse"].wait(1.0)
            job["updated_at"] = time.time()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",      # nginx would otherwise hold the frames
        },
    )


@snowflake_router.post("/copy/{job_id}/cancel")
def cancel_copy(job_id: str, auth: dict = Depends(sf_auth)):
    job = _get_copy_job(job_id, auth["user"]["email"])
    if job["status"] != "running":
        return {"job_id": job_id, "status": job["status"]}
    job["cancel_requested"] = True
    with job["lock"]:
        cursors = list(job["active_cursors"])
    for cur in cursors:
        try:
            cur.cancel()
        except Exception as e:
            logger.info("Copy chunk cancel failed for job %s: %s", job_id, e)
    _copy_log(job, "warn", "Cancel requested - finishing in-flight chunks, no new ones will start")
    return {"job_id": job_id, "status": "cancelling"}


@snowflake_router.delete("/copy/{job_id}")
def close_copy(job_id: str, auth: dict = Depends(sf_auth)):
    job = _get_copy_job(job_id, auth["user"]["email"])
    with _copy_jobs_lock:
        _copy_jobs.pop(job_id, None)
    return {"job_id": job_id, "status": "closed"}


@snowflake_router.get("/copy/jobs")
def list_copy_jobs(auth: dict = Depends(sf_auth)):
    owner = (auth["user"]["email"] or "").strip().lower()
    with _copy_jobs_lock:
        jobs = [j for j in _copy_jobs.values() if j["owner"] == owner]
    jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return {
        "jobs": [
            {
                "job_id": j["job_id"], "status": j["status"], "copy_type": j["copy_type"],
                "mode": j["mode"], "source": j["source"], "target": j["target"],
                "created_at": j["created_at"], "tables_total": j["tables_total"],
                "tables_done": j["tables_done"],
            }
            for j in jobs[:20]
        ]
    }


def shutdown():
    """Called from app.py's shutdown hook - drop every held connection."""
    with _jobs_lock:
        jobs = list(_jobs.values())
        _jobs.clear()
    for job in jobs:
        _close_job(job)
    with _copy_jobs_lock:
        for job in _copy_jobs.values():
            job["cancel_requested"] = True
        _copy_jobs.clear()
    _pool.close_all()