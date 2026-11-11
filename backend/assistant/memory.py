"""
Per-user long-term memory for the assistant, backed by Postgres.

Two things live here, one row per user (email is the key):
  - preferences   jsonb   things the user explicitly asked Regwiz to remember
                          ("always show me delta mode", "call me by my first name")
  - recent_summary text   a short rolling digest of recent turns, so a new
                          session can pick up roughly where the last one left
                          off instead of starting from nothing.

This is deliberately NOT a full conversation transcript store. A single bounded
row per user keeps it cheap to read on every chat request and impossible to
grow into an unbounded liability. If per-user analytics or a full history view
is ever wanted, that's a separate table/feature, not this one.

Stored in the portal database, table smarthub_core.assistant_user_memory
(created by the core migrations). Best-effort by design: if the database is
unreachable every function here degrades to a no-op and logs, and the
assistant carries on without cross-session memory.
"""

import json
import logging

import db

logger = logging.getLogger(__name__)

# Keep the rolling summary bounded — this is a "where did we leave off" primer
# for the model, not an archive. Old turns fall off the front.
MAX_SUMMARY_CHARS = 1500
MAX_PREFERENCES = 50  # a runaway "remember this" loop shouldn't grow this forever


def _with_conn(fn, default=None):
    """Run fn(conn) in one transaction. Any failure is logged and answered with
    `default`; memory must never break a chat. The pool already drops dead
    connections and pings stale ones, so there is no retry loop here."""
    if not db.is_configured():
        return default
    try:
        with db.transaction() as conn:
            return fn(conn)
    except (db.DatabaseUnavailable, db.OperationalError) as e:
        # An outage, not a bug: one line per call, no traceback flood.
        logger.warning("assistant memory unavailable: %s", e)
        return default
    except Exception:
        logger.exception("assistant memory: query failed")
        return default


def get_memory(email: str) -> dict:
    """{"preferences": {...}, "recent_summary": "..."} — empty shape if unavailable/unknown user."""
    if not email:
        return {"preferences": {}, "recent_summary": ""}

    def q(conn):
        row = conn.execute(
            "SELECT preferences, recent_summary FROM assistant_user_memory WHERE email = ?",
            (email,),
        ).fetchone()
        if not row:
            return {"preferences": {}, "recent_summary": ""}
        return {"preferences": row["preferences"] or {}, "recent_summary": row["recent_summary"] or ""}

    return _with_conn(q, default={"preferences": {}, "recent_summary": ""})


def set_preference(email: str, key: str, value: str) -> bool:
    """Explicit "remember this about me" fact, set by the assistant on the user's
    own request (via the remember_preference tool). Merged into the jsonb blob."""
    if not email or not key:
        return False
    key = str(key).strip()[:80]
    value = str(value).strip()[:500]
    if not key:
        return False

    def q(conn):
        # Make sure the row exists, then lock it: a bare SELECT ... FOR UPDATE
        # locks nothing for a brand-new user, and two first-time saves would
        # each overwrite the other's key.
        conn.execute(
            "INSERT INTO assistant_user_memory (email) VALUES (?) ON CONFLICT (email) DO NOTHING",
            (email,),
        )
        row = conn.execute(
            "SELECT preferences FROM assistant_user_memory WHERE email = ? FOR UPDATE",
            (email,),
        ).fetchone()
        prefs = dict(row["preferences"] or {})
        if key not in prefs and len(prefs) >= MAX_PREFERENCES:
            # Refuse new keys once the user has piled up an unreasonable
            # number of them; updating an existing one is still fine.
            return False
        prefs[key] = value
        conn.execute(
            "UPDATE assistant_user_memory SET preferences = ?::jsonb, updated_at = now() WHERE email = ?",
            (json.dumps(prefs), email),
        )
        return True

    return bool(_with_conn(q, default=False))


def append_turn(email: str, question: str, answer: str) -> None:
    """Extractive rolling summary — no extra LLM call, just keeps a bounded,
    readable trail of recent topics so the next session has some continuity.
    Best-effort: failures here must never affect the chat response itself."""
    if not email or not (question or answer):
        return
    question = (question or "").strip().replace("\n", " ")[:200]
    answer = (answer or "").strip().replace("\n", " ")[:200]
    if not question and not answer:
        return
    line = f"- Q: {question}" + (f" -> A: {answer}" if answer else "")

    def q(conn):
        conn.execute(
            "INSERT INTO assistant_user_memory (email) VALUES (?) ON CONFLICT (email) DO NOTHING",
            (email,),
        )
        row = conn.execute(
            "SELECT recent_summary FROM assistant_user_memory WHERE email = ? FOR UPDATE",
            (email,),
        ).fetchone()
        combined = ((row["recent_summary"] or "") + "\n" + line).strip()
        # keep only the most recent lines that fit the budget
        lines = combined.splitlines()
        while lines and len("\n".join(lines)) > MAX_SUMMARY_CHARS:
            lines.pop(0)
        conn.execute(
            "UPDATE assistant_user_memory SET recent_summary = ?, updated_at = now() WHERE email = ?",
            ("\n".join(lines), email),
        )

    _with_conn(q)


def is_configured() -> bool:
    return db.is_configured()