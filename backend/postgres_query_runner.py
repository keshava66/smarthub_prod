from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time as time_, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Wired in from app.py via set_postgres_dependencies()
_config: Optional[Dict[str, Any]] = None

# ============================================================================
# Config
# ============================================================================

DEFAULT_MAX_CONCURRENT_JOBS = 6              # dedicated connections held at once
DEFAULT_JOB_TTL_SECONDS = 45 * 60            # jobs (and their held cursor/connection) are GC'd after this


def _cfg_value(key: str, env_var: str, default=None):
    if _config and _config.get(key) not in (None, ""):
        return _config[key]
    if os.environ.get(env_var):
        return os.environ[env_var]
    return default


def _require_config() -> Dict[str, Any]:
    if not _config and not os.environ.get("PGQR_HOST"):
        raise HTTPException(
            status_code=503,
            detail="Postgres Query Runner is not configured - missing 'postgres_query_runner' section in dq_config.json",
        )
    return {
        "host": _cfg_value("host", "PGQR_HOST"),
        "port": int(_cfg_value("port", "PGQR_PORT", 5432)),
        "user": _cfg_value("user", "PGQR_USER"),
        "password": _cfg_value("password", "PGQR_PASSWORD", ""),
        "maintenance_database": _cfg_value("maintenance_database", "PGQR_MAINTENANCE_DATABASE", "postgres"),
        "default_database": _cfg_value("default_database", "PGQR_DATABASE") or _cfg_value("maintenance_database", "PGQR_MAINTENANCE_DATABASE", "postgres"),
        "sslmode": _cfg_value("sslmode", "PGQR_SSLMODE", "prefer"),
        "connect_timeout": int(_cfg_value("connect_timeout_seconds", "PGQR_CONNECT_TIMEOUT", 10)),
        "max_concurrent_jobs": int(_cfg_value("max_concurrent_jobs", "PGQR_MAX_CONCURRENT_JOBS", DEFAULT_MAX_CONCURRENT_JOBS)),
        "job_ttl_seconds": int(_cfg_value("job_ttl_seconds", "PGQR_JOB_TTL_SECONDS", DEFAULT_JOB_TTL_SECONDS)),
    }


def set_postgres_dependencies(config: Optional[Dict[str, Any]]):
    """Called once from app.py with the 'postgres_query_runner' section of dq_config.json."""
    global _config
    _config = config
    if not _config:
        logger.warning("⚠️ postgres_query_runner config not found - Data Studio Postgres will be unavailable unless PGQR_* env vars are set")


def _to_json_safe(val):
    """Coerce Postgres/psycopg2 result values into something json-encodable.
    Numeric/temporal precision is preserved by rendering as text rather than
    float, since a browser round-tripping a NUMERIC(38,10) through a JS
    number would silently corrupt it."""
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
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(val).hex()
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
    # Ranges, custom composite types, etc.
    try:
        return str(val)
    except Exception:
        return None


# ============================================================================
# SQL validation / classification
# ----------------------------------------------------------------------------
# Full PostgreSQL is allowed (SELECT, INSERT, UPDATE, DELETE, DDL, DO blocks,
# ...) - this is NOT a read-only runner. Two safety rules are enforced
# instead: only a single statement per run (stacked-statement injection is
# never possible), and anything that mutates data/schema requires an explicit
# `confirm: true` from the caller, with an extra warning surfaced when an
# UPDATE/DELETE has no WHERE clause.
# ============================================================================

_ALWAYS_BLOCKED = [
    # Filesystem / program execution surface - never worth the risk from a
    # web query box regardless of confirmation.
    r"\bCOPY\b.*\bPROGRAM\b",
    r"\bLO_IMPORT\b",
    r"\bLO_EXPORT\b",
    r"\bPG_READ_FILE\b",
    r"\bPG_READ_BINARY_FILE\b",
    r"\bPG_LS_DIR\b",
]

_DESTRUCTIVE_LEAD_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "DROP", "ALTER",
    "CREATE", "GRANT", "REVOKE", "VACUUM", "REINDEX", "CLUSTER", "REFRESH",
    "DO", "CALL", "COMMENT", "SECURITY", "LOCK", "IMPORT",
}

_DQL_LEAD_KEYWORDS = {"SELECT", "TABLE", "VALUES", "SHOW"}


def _strip_sql_comments_and_dollar_quotes(sql: str) -> str:
    """Removes '--' and block comments (keeping their newlines) without
    touching quoted strings, dollar-quoted strings/bodies ($$...$$ or
    $tag$...$tag$ - used by DO blocks and function bodies), or quoted
    identifiers."""
    out = []
    i, n = 0, len(sql)
    in_sq = in_dq = False
    dollar_tag = None  # the $tag$ currently open, or None
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""
        if dollar_tag is not None:
            out.append(ch)
            if sql.startswith(dollar_tag, i):
                out.append(sql[i + 1: i + len(dollar_tag)])
                i += len(dollar_tag)
                dollar_tag = None
                continue
            i += 1
            continue
        if in_sq:
            out.append(ch)
            if ch == "'":
                if nxt == "'":
                    out.append(nxt); i += 1
                else:
                    in_sq = False
            i += 1
            continue
        if in_dq:
            out.append(ch)
            if ch == '"':
                in_dq = False
            i += 1
            continue
        if ch == "'":
            in_sq = True; out.append(ch); i += 1; continue
        if ch == '"':
            in_dq = True; out.append(ch); i += 1; continue
        if ch == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m:
                dollar_tag = m.group(0)
                out.append(dollar_tag)
                i += len(dollar_tag)
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
                    depth += 1; i += 2; continue
                if sql[i] == "*" and sql[i + 1: i + 2] == "/":
                    depth -= 1; i += 2; continue
                i += 1
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_top_level_statements(sql: str) -> List[str]:
    """Splits on ';' that are not inside a string/identifier/dollar-quote.
    Operates on the ALREADY comment-stripped text."""
    stmts, cur = [], []
    in_sq = in_dq = False
    dollar_tag = None
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if dollar_tag is not None:
            cur.append(ch)
            if sql.startswith(dollar_tag, i):
                cur.append(sql[i + 1: i + len(dollar_tag)])
                i += len(dollar_tag)
                dollar_tag = None
                continue
            i += 1; continue
        if in_sq:
            cur.append(ch)
            if ch == "'":
                if sql[i + 1: i + 2] == "'":
                    cur.append("'"); i += 2; continue
                in_sq = False
            i += 1; continue
        if in_dq:
            cur.append(ch)
            if ch == '"':
                in_dq = False
            i += 1; continue
        if ch == "'":
            in_sq = True; cur.append(ch); i += 1; continue
        if ch == '"':
            in_dq = True; cur.append(ch); i += 1; continue
        if ch == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m:
                dollar_tag = m.group(0)
                cur.append(dollar_tag)
                i += len(dollar_tag)
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


def _paren_depth_zero_search(sql: str, pattern: str) -> bool:
    """True if `pattern` (a compiled-ready regex fragment, word-bounded)
    appears at parenthesis depth 0 - i.e. not inside a subquery."""
    depth = 0
    in_sq = in_dq = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        if in_sq:
            if ch == "'" and sql[i + 1: i + 2] != "'":
                in_sq = False
            elif ch == "'":
                i += 1
            i += 1; continue
        if in_dq:
            if ch == '"':
                in_dq = False
            i += 1; continue
        if ch == "'":
            in_sq = True; i += 1; continue
        if ch == '"':
            in_dq = True; i += 1; continue
        if ch == "(":
            depth += 1; i += 1; continue
        if ch == ")":
            depth = max(0, depth - 1); i += 1; continue
        if depth == 0:
            m = re.match(pattern, sql[i:], re.IGNORECASE)
            if m:
                return True
        i += 1
    return False


@dataclass
class SqlClassification:
    cleaned_sql: str
    lead_keyword: str
    is_destructive: bool
    returns_rows: bool
    cursorable: bool = True
    warning: Optional[str] = None


def classify_and_validate_sql(sql: str) -> SqlClassification:
    """Validates a single, well-formed statement and classifies it. Raises
    HTTPException(400) for anything structurally invalid (empty, multiple
    statements, blocked filesystem/program surface)."""
    cleaned_full = _strip_sql_comments_and_dollar_quotes(sql).strip()
    if not cleaned_full:
        raise HTTPException(status_code=400, detail="Query is empty")

    statements = _split_top_level_statements(cleaned_full)
    if len(statements) == 0:
        raise HTTPException(status_code=400, detail="Query is empty")
    if len(statements) > 1:
        raise HTTPException(status_code=400, detail="Only a single statement is allowed per run")

    # Re-slice the ORIGINAL sql (not the comment-stripped copy) so what
    # actually executes still contains the user's comments/formatting -
    # only the trailing ';' is trimmed.
    cleaned = sql.strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()

    for pattern in _ALWAYS_BLOCKED:
        if re.search(pattern, cleaned_full, re.IGNORECASE):
            raise HTTPException(status_code=400, detail="This statement touches server filesystem/program execution and is not allowed from the query runner")

    m = re.match(r"^\s*([A-Za-z]+)", cleaned_full)
    lead = m.group(1).upper() if m else ""

    # WITH ... can prefix a SELECT or a data-modifying statement
    # (WITH x AS (...) DELETE FROM t ...). Find the top-level command after
    # the CTE list to classify it correctly.
    effective_lead = lead
    if lead == "WITH":
        depth = 0
        in_sq = in_dq = False
        i, n = 0, len(cleaned_full)
        found = None
        while i < n:
            ch = cleaned_full[i]
            if in_sq:
                if ch == "'" and cleaned_full[i + 1: i + 2] != "'":
                    in_sq = False
                i += 1; continue
            if in_dq:
                if ch == '"':
                    in_dq = False
                i += 1; continue
            if ch == "'":
                in_sq = True; i += 1; continue
            if ch == '"':
                in_dq = True; i += 1; continue
            if ch == "(":
                depth += 1; i += 1; continue
            if ch == ")":
                depth = max(0, depth - 1); i += 1; continue
            if depth == 0:
                mm = re.match(r"\b(SELECT|INSERT|UPDATE|DELETE|MERGE)\b", cleaned_full[i:], re.IGNORECASE)
                if mm and i > 0:  # skip the WITH's own leading match
                    found = mm.group(1).upper()
                    break
            i += 1
        effective_lead = found or "SELECT"

    if lead == "EXPLAIN":
        # EXPLAIN ANALYZE (either "EXPLAIN ANALYZE ..." or the modern
        # "EXPLAIN (ANALYZE, ...) ..." option-list form) actually executes
        # the inner statement, so it inherits that statement's destructiveness.
        rest = cleaned_full[len("EXPLAIN"):].lstrip()
        analyzes = False
        if rest.startswith("("):
            close = rest.find(")")
            opts = rest[1:close] if close != -1 else ""
            analyzes = bool(re.search(r"\bANALYZE\b", opts, re.IGNORECASE)) and not re.search(r"\bANALYZE\s+(FALSE|OFF|0)\b", opts, re.IGNORECASE)
            rest = rest[close + 1:].lstrip() if close != -1 else rest
        else:
            m2 = re.match(r"ANALYZE\b\s*", rest, re.IGNORECASE)
            if m2:
                analyzes = True
                rest = rest[m2.end():].lstrip()
        if analyzes:
            inner_m = re.match(r"^\s*([A-Za-z]+)", rest)
            effective_lead = (inner_m.group(1).upper() if inner_m else "SELECT")
        else:
            effective_lead = "EXPLAIN"

    is_destructive = effective_lead in _DESTRUCTIVE_LEAD_KEYWORDS
    returns_rows = effective_lead in _DQL_LEAD_KEYWORDS or effective_lead == "EXPLAIN"

    # DECLARE ... CURSOR only accepts a plain SELECT/VALUES/TABLE query.
    # EXPLAIN (including EXPLAIN ANALYZE, which classifies as its inner
    # statement) and DML ... RETURNING return rows but cannot be wrapped in a
    # cursor, so those results get read straight into memory instead.
    cursorable = lead in ("SELECT", "VALUES", "TABLE") or (lead == "WITH" and effective_lead == "SELECT")

    warning = None
    if effective_lead in ("INSERT",):
        returns_rows = bool(_paren_depth_zero_search(cleaned_full, r"\bRETURNING\b"))
    elif effective_lead in ("UPDATE", "DELETE"):
        returns_rows = bool(_paren_depth_zero_search(cleaned_full, r"\bRETURNING\b"))
        if not _paren_depth_zero_search(cleaned_full, r"\bWHERE\b"):
            warning = f"This {effective_lead} has no WHERE clause and will affect every row in the target table."
    elif effective_lead == "TRUNCATE":
        warning = "TRUNCATE removes every row in the target table and cannot be filtered."
    elif effective_lead == "DROP":
        warning = "DROP permanently removes the object and its data."
    elif effective_lead == "ALTER" and re.search(r"\bDROP\s+COLUMN\b", cleaned_full, re.IGNORECASE):
        warning = "This ALTER drops a column, which permanently discards its data."

    return SqlClassification(
        cleaned_sql=cleaned,
        lead_keyword=effective_lead,
        is_destructive=is_destructive,
        returns_rows=returns_rows,
        cursorable=cursorable,
        warning=warning,
    )