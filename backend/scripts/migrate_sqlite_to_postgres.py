"""
One-time copy of the old SQLite files into the portal database (smarthub_core).

    cd backend
    python scripts/migrate_sqlite_to_postgres.py --dry-run     # read, convert, count, roll back
    python scripts/migrate_sqlite_to_postgres.py               # do it

Reads (whichever exist, from --source-dir, default backend/):
    audit_log.db  rbac.db  communication_center.db  data_api_exports.db
    snowflake_query_runner.db  release_notes.db

A deployment that already moved the Data API metadata to PostgreSQL can copy
that instead with --data-api-pg-schema smarthub_data_api, and assistant memory
kept in PostgreSQL before comes across with --assistant-memory-schema public
(both read from the same database as SMARTHUB_DB_DSN).

Everything happens in one transaction: it all lands or none of it does.
Rows are merged: anything whose key is already in smarthub_core is left as it
is, so running it twice (or after the portal has already started writing)
duplicates nothing and loses nothing. The one exception is the Data API
request log, which has no key to match on: it is copied only into an empty
table. --replace empties each target table first instead. Stop the old
portal first; rows it writes while this runs would not be copied.

Conversions: ISO text timestamps become timestamptz (naive values are read
as UTC for the Data API and Data Studio, whose old code wrote naive UTC, and
as Eastern everywhere else); 0/1 flags become booleans; JSON columns are
checked and anything unreadable falls back to the column's empty default;
legacy gaps the old startup code used to patch (missing slugs and API keys,
reserved slugs, What's New subjects) are filled in on the way through.
"""

import argparse
import json
import logging
import re
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytz

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import db  # noqa: E402

log = logging.getLogger("migrate")
EASTERN = pytz.timezone("America/New_York")
UTC = timezone.utc


# ─── value converters ───────────────────────────────────────────────────────

class Skip(Exception):
    """This row can't be carried over; say why and move on."""


def ts(naive_zone):
    def convert(value, required=False):
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise Skip("missing timestamp")
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                if required:
                    raise Skip(f"unreadable timestamp {value!r}")
                return None
        if parsed.tzinfo is None:
            parsed = naive_zone.localize(parsed) if hasattr(naive_zone, "localize") else parsed.replace(tzinfo=naive_zone)
        return parsed
    return convert


ts_eastern = ts(EASTERN)
ts_utc = ts(UTC)


def flag(default):
    def convert(value, required=False):
        if value is None or value == "":
            return default
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "t", "yes")
        return bool(value)
    return convert


def text(default=""):
    def convert(value, required=False):
        if value is None:
            if required:
                raise Skip("missing value")
            return default
        return str(value)
    return convert


def json_text(default):
    def convert(value, required=False):
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise Skip("missing JSON")
            return default
        try:
            json.loads(value)
            return value
        except (TypeError, ValueError):
            if required:
                raise Skip("unreadable JSON")
            return default
    return convert


def integer(default=None, low=None, high=None):
    def convert(value, required=False):
        if value is None or value == "":
            if required and default is None:
                raise Skip("missing number")
            return default
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        if low is not None:
            number = max(low, number)
        if high is not None:
            number = min(high, number)
        return number
    return convert


def nullable_text(value, required=False):
    return None if value is None else str(value)


# ─── what goes where ────────────────────────────────────────────────────────
# (source table, target table, {column: (converter, required)}, conflict target)

REQ = True
SPECS = {
    "audit_log.db": [
        ("audit_log", "audit_log", {
            "id": (text(), REQ), "timestamp": (ts_eastern, REQ), "action": (text(), REQ),
            "performed_by": (text(), False), "target": (text(), False), "details": (text(), False),
            "ip_address": (text(), False)}, "id"),
        ("assistant_settings", "assistant_settings", {
            "user_email": (text(), REQ), "enabled": (flag(False), False), "avatar_id": (text(), False),
            "appearance": (text("standard"), False), "reduce_motion": (flag(False), False),
            "text_size": (text("medium"), False), "avatar_size": (text("medium"), False),
            "auto_send_voice": (flag(True), False), "updated_at": (ts_eastern, REQ)}, "user_email"),
    ],
    "rbac.db": [
        ("rbac_custom_roles", "rbac_custom_roles", {
            "role_id": (text(), REQ), "definition": (json_text(None), REQ)}, "role_id"),
        ("rbac_user_overrides", "rbac_user_overrides", {
            "email": (text(), REQ), "role_name": (text(), REQ), "roles": (json_text(None), False),
            "keep_ad": (flag(False), False)}, "email"),
        ("rbac_user_permissions", "rbac_user_permissions", {
            "email": (text(), REQ), "grants": (json_text("[]"), False),
            "revokes": (json_text("[]"), False)}, "email"),
        ("rbac_meta", "rbac_meta", {"key": (text(), REQ), "value": (nullable_text, False)}, "key"),
    ],
    "communication_center.db": [
        ("email_templates", "email_templates", {
            "id": (text(), REQ), "name": (text(), REQ), "category": (text("custom"), False),
            "subject": (text(), False), "body": (text(), False), "layout": (json_text("{}"), False),
            "created_by": (text(), False), "created_at": (ts_eastern, REQ), "updated_at": (ts_eastern, REQ)}, "id"),
        ("email_history", "email_history", {
            "id": (text(), REQ), "sent_at": (ts_eastern, REQ), "sent_by": (text(), REQ),
            "category": (text("custom"), False), "subject": (text(), False), "body": (text(), False),
            "to_addrs": (text(), False), "cc_addrs": (text(), False), "bcc_addrs": (text(), False),
            "attachments": (json_text("[]"), False), "recipient_count": (integer(0, low=0), False),
            "status": (text("sent"), False), "layout": (json_text("{}"), False), "error": (text(), False),
            "is_test": (flag(False), False), "sensitivity": (text(), False), "attachment_dir": (text(), False),
            "scheduled_id": (text(), False), "resent_from": (text(), False),
            "release_notes": (text(), False)}, "id"),
        ("email_scheduled", "email_scheduled", {
            "id": (text(), REQ), "created_at": (ts_eastern, REQ), "created_by": (text(), REQ),
            "send_at": (ts_eastern, REQ), "category": (text("custom"), False), "subject": (text(), False),
            "body": (text(), False), "layout": (json_text("{}"), False), "to_addrs": (text(), False),
            "cc_addrs": (text(), False), "bcc_addrs": (text(), False), "attachments": (json_text("[]"), False),
            "attachment_dir": (text(), False), "recipient_count": (integer(0, low=0), False),
            "status": (text("pending"), False), "error": (text(), False), "history_id": (text(), False),
            "sensitivity": (text(), False), "release_notes": (text(), False)}, "id"),
    ],
    "snowflake_query_runner.db": [
        ("query_history", "sf_query_history", {
            "id": (text(), REQ), "user_email": (text(), REQ), "sql": (text(), REQ),
            "database": (text(), False), "schema": (text(), False), "status": (text(), False),
            "elapsed_ms": (integer(0), False), "row_count": (integer(None), False), "error": (text(), False),
            "created_at": (ts_utc, REQ)}, "id"),
        ("saved_queries", "sf_saved_queries", {
            "id": (text(), REQ), "user_email": (text(), REQ), "name": (text(), REQ), "sql": (text(), REQ),
            "database": (text(), False), "schema": (text(), False), "shared": (flag(False), False),
            "created_at": (ts_utc, REQ), "updated_at": (ts_utc, REQ)}, "id"),
    ],
    "release_notes.db": [
        ("release_notes", "release_notes", {
            "id": (text(), REQ), "title": (text(), REQ), "summary": (text(), False), "detail": (text(), False),
            "area": (text(), False), "kind": (text("feature"), False), "status": (text("draft"), False),
            "source": (text("manual"), False), "fingerprint": (nullable_text, False), "subject": (text(), False),
            "created_at": (ts_eastern, REQ), "created_by": (text("system"), False),
            "announced_at": (ts_eastern, False), "announced_in": (nullable_text, False)}, "id"),
        ("snapshots", "release_note_snapshots", {
            "key": (text(), REQ), "payload": (json_text(None), REQ), "taken_at": (ts_eastern, REQ)}, "key"),
    ],
    "data_api_exports.db": [
        ("data_apis", "data_apis", {
            "id": (text(), REQ), "slug": (nullable_text, False), "name": (text(), REQ),
            "description": (text(), False), "api_key": (nullable_text, False), "created_by": (nullable_text, False),
            "created_at": (ts_utc, REQ), "is_active": (flag(True), False), "last_used_at": (ts_utc, False)}, "id"),
        ("data_api_tables", "data_api_tables", {
            "id": (text(), REQ), "api_id": (text(), REQ), "source_system": (text(), REQ), "env": (text(), REQ),
            "database_name": (nullable_text, False), "schema_name": (nullable_text, False),
            "table_name": (text(), REQ), "alias": (text(), REQ),
            "row_limit": (integer(1000, low=1, high=10000), False),
            "columns_csv": (nullable_text, False)}, None),
        ("data_api_requests", "data_api_requests", {
            "api_id": (text(), REQ), "alias": (nullable_text, False), "endpoint": (text(), REQ),
            "ts": (ts_utc, REQ), "ip": (text(), False), "user_agent": (text(), False),
            "status": (integer(200), False), "row_count": (integer(None), False),
            "duration_ms": (integer(None), False)}, None),
        ("data_api_embedding_models", "data_api_embedding_models", {
            "id": (text(), REQ), "name": (text(), REQ), "deployment": (text(), REQ),
            "dimensions": (integer(None, low=1, high=16000), REQ), "endpoint": (text(), False),
            "api_version": (text(), False), "api_key_env": (text(), REQ), "query_prefix": (text(), False),
            "send_dimensions": (flag(False), False), "created_by": (nullable_text, False),
            "created_at": (ts_utc, REQ), "updated_by": (nullable_text, False),
            "updated_at": (ts_utc, False)}, None),
        ("data_api_vector_registrations", "data_api_vector_registrations", {
            "id": (text(), REQ), "source_system": (text(), REQ), "env": (text(), REQ),
            "database_name": (text(), False), "schema_name": (text(), REQ), "table_name": (text(), REQ),
            "column_name": (text(), REQ), "model_id": (text(), REQ), "created_by": (nullable_text, False),
            "created_at": (ts_utc, REQ)}, None),
        ("data_api_contracts", "data_api_contracts", {
            "id": (text(), REQ), "name": (text(), REQ), "version": (integer(None, low=1), REQ),
            "description": (text(), False), "definition": (json_text(None), REQ),
            "created_by": (nullable_text, False), "created_at": (ts_utc, REQ)}, None),
        ("data_api_search_settings", "data_api_search_settings", {
            "table_id": (text(), REQ), "settings": (json_text(None), REQ), "updated_by": (nullable_text, False),
            "updated_at": (ts_utc, REQ)}, None),
    ],
}

# Child tables first when clearing with --replace.
CLEAR_ORDER = [
    "data_api_search_settings", "data_api_vector_registrations", "data_api_contracts",
    "data_api_embedding_models", "data_api_requests", "data_api_tables", "data_api_rate_windows", "data_apis",
]

RESERVED_SLUGS = {
    "list", "create", "sources", "source-databases", "source-schemas",
    "source-tables", "source-columns", "docs", "openapi", "redoc", "mcp", "search",
}
KINDS = {"feature", "improvement", "fix", "removal", "commit"}
NOTE_STATUSES = {"draft", "ready", "announced", "hidden"}
SCHEDULE_STATUSES = {"pending", "sending", "sent", "failed", "cancelled"}


def slugify(name):
    """Same rule as data_api._slugify."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    if not slug:
        return "api"
    return f"{slug}-api" if slug in RESERVED_SLUGS else slug


# ─── per-table fix-ups the old startup code used to do ─────────────────────

class Fixups:
    def __init__(self):
        self.slugs = set()
        self.api_ids = set()
        self.table_ids = set()
        self.model_ids = set()

    def apply(self, target, row):
        if target == "data_apis":
            slug = row.get("slug")
            if not slug or slug in RESERVED_SLUGS or slug in self.slugs:
                base = slugify(row["name"])
                slug, n = base, 2
                while slug in self.slugs:
                    slug, n = f"{base}-{n}", n + 1
                row["slug"] = slug
            self.slugs.add(row["slug"])
            if not row.get("api_key"):
                row["api_key"] = f"smarthub_{secrets.token_urlsafe(32)}"
            self.api_ids.add(row["id"])
        elif target in ("data_api_tables", "data_api_requests"):
            if row["api_id"] not in self.api_ids:
                raise Skip(f"its API {row['api_id']} no longer exists")
            if target == "data_api_tables":
                self.table_ids.add(row["id"])
        elif target == "data_api_embedding_models":
            self.model_ids.add(row["id"])
        elif target == "data_api_vector_registrations":
            if row["model_id"] not in self.model_ids:
                raise Skip(f"its model {row['model_id']} no longer exists")
        elif target == "data_api_search_settings":
            if row["table_id"] not in self.table_ids:
                raise Skip(f"its table {row['table_id']} no longer exists")
        elif target == "release_notes":
            if row["kind"] not in KINDS:
                row["kind"] = "feature"
            if row["status"] not in NOTE_STATUSES:
                row["status"] = "draft"
            fingerprint = row.get("fingerprint") or ""
            if not row["subject"] and fingerprint.startswith(("page:", "route:")):
                row["subject"] = fingerprint
        elif target == "email_history":
            if row["status"] not in ("sent", "failed"):
                row["status"] = "failed"
        elif target == "email_scheduled":
            if row["status"] not in SCHEDULE_STATUSES:
                row["status"] = "failed"
        return row


# ─── sources ────────────────────────────────────────────────────────────────

def sqlite_rows(path, table):
    """Rows of one SQLite table as dicts, streamed, or None if there's no such
    table. rowid order keeps "first row wins" for duplicates the same as the
    old code saw them."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    if not exists:
        conn.close()
        return None

    def stream():
        try:
            cursor = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            while True:
                chunk = cursor.fetchmany(5000)
                if not chunk:
                    return
                for r in chunk:
                    yield dict(r)
        finally:
            conn.close()
    return stream()


def pg_rows(conn, schema, table):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"{schema}.{table}",))
        if cur.fetchone()[0] is None:
            return None

    def stream():
        # Named (server-side) cursor: a big request log comes across in
        # batches instead of all at once.
        with conn.cursor(name=f"src_{table}", cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.itersize = 5000
            cur.execute(f'SELECT * FROM "{schema}"."{table}"')
            for r in cur:
                yield dict(r)
    return stream()


# ─── copy ───────────────────────────────────────────────────────────────────

def target_count(cur, table):
    cur.execute(f"SELECT count(*) FROM {table}")
    return cur.fetchone()[0]


def copy_table(cur, source_rows, target, columns, conflict, fixups, report):
    names = list(columns)
    # No conflict target: a row clashing with ANY unique key already there
    # (id, slug, fingerprint, name...) is skipped rather than aborting the run.
    statement = f"INSERT INTO {target} ({', '.join(names)}) VALUES %s ON CONFLICT DO NOTHING"
    before = target_count(cur, target)
    total = converted = skipped = 0
    batch = []

    def flush():
        if batch:
            psycopg2.extras.execute_values(cur, statement, batch, page_size=1000)
            batch.clear()

    for raw in source_rows:
        total += 1
        try:
            row = {}
            for name, (convert, required) in columns.items():
                value = raw.get(name)
                if isinstance(value, str) and "\x00" in value:
                    value = value.replace("\x00", "")   # PostgreSQL text can't hold NUL
                row[name] = convert(value, required) if required else convert(value)
            row = fixups.apply(target, row)
        except Skip as why:
            skipped += 1
            if skipped <= 5:
                log.warning("  %s row %d skipped: %s", target, total, why)
            continue
        converted += 1
        batch.append(tuple(row[n] for n in names))
        if len(batch) >= 5000:
            flush()
    flush()
    if skipped > 5:
        log.warning("  %s: %d more rows skipped", target, skipped - 5)
    added = target_count(cur, target) - before
    report.append((target, total, added, skipped, converted - added))


def preload_parents(cur, target, fixups):
    if target == "data_apis":
        cur.execute("SELECT id, slug FROM data_apis")
        for api_id, slug in cur.fetchall():
            fixups.api_ids.add(api_id)
            fixups.slugs.add(slug)
    elif target == "data_api_tables":
        cur.execute("SELECT id FROM data_api_tables")
        fixups.table_ids.update(r[0] for r in cur.fetchall())
    elif target == "data_api_embedding_models":
        cur.execute("SELECT id FROM data_api_embedding_models")
        fixups.model_ids.update(r[0] for r in cur.fetchall())


def copy_assistant_memory(cur, schema, replace, report):
    cur.execute("SELECT to_regclass(%s)", (f"{schema}.assistant_user_memory",))
    if cur.fetchone()[0] is None:
        log.info("assistant memory: no %s.assistant_user_memory, skipped", schema)
        return
    if replace:
        cur.execute("DELETE FROM assistant_user_memory")
    cur.execute(f'SELECT count(*) FROM "{schema}".assistant_user_memory')
    total = cur.fetchone()[0]
    cur.execute(f"""
        INSERT INTO assistant_user_memory (email, preferences, recent_summary, updated_at)
        SELECT email, COALESCE(preferences, '{{}}'::jsonb), COALESCE(recent_summary, ''), COALESCE(updated_at, now())
          FROM "{schema}".assistant_user_memory
        ON CONFLICT (email) DO NOTHING""")
    report.append(("assistant_user_memory", total, cur.rowcount, 0, total - cur.rowcount))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--source-dir", default=str(BACKEND), help="folder holding the .db files")
    parser.add_argument("--data-api-pg-schema", default="",
                        help="copy Data API metadata from this PostgreSQL schema instead of data_api_exports.db")
    parser.add_argument("--assistant-memory-schema", default="",
                        help="copy assistant_user_memory from this PostgreSQL schema")
    parser.add_argument("--replace", action="store_true", help="empty each target table first instead of merging")
    parser.add_argument("--dry-run", action="store_true", help="do everything, then roll back")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    applied = db.migrate()
    if applied:
        log.info("Schema created/updated: migrations %s", applied)

    source_dir = Path(args.source_dir)
    raw = db.open_connection(statement_timeout_ms=0, lock_timeout_ms=30000,
                             application_name="smarthub-sqlite-import")
    report = []
    fixups = Fixups()
    try:
        with raw.cursor() as cur:
            # Serialised against a second copy of this script, and against the
            # schema migrator.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (db.lock_key("sqlite-import"),))
            for filename, tables in SPECS.items():
                from_pg = filename == "data_api_exports.db" and args.data_api_pg_schema
                path = source_dir / filename
                if not from_pg and not path.exists():
                    log.info("%s: not found, skipped", filename)
                    continue
                log.info("%s%s", filename, f" (from PostgreSQL schema {args.data_api_pg_schema})" if from_pg else "")
                for source_table, target, columns, conflict in tables:
                    rows = pg_rows(raw, args.data_api_pg_schema, source_table) if from_pg else sqlite_rows(path, source_table)
                    if rows is None:
                        log.info("  %s: no such table in the source, skipped", source_table)
                        continue
                    existing = target_count(cur, target)
                    if existing and args.replace:
                        if target in CLEAR_ORDER:
                            for child in CLEAR_ORDER[:CLEAR_ORDER.index(target) + 1]:
                                cur.execute(f"DELETE FROM {child}")
                        else:
                            cur.execute(f"DELETE FROM {target}")
                    elif existing and target == "data_api_requests":
                        log.info("  %s: already has %d rows and has no key to merge on, left alone "
                                 "(use --replace to overwrite)", target, existing)
                        continue
                    # Rows already in PostgreSQL count as parents for the
                    # rows about to be merged under them.
                    preload_parents(cur, target, fixups)
                    copy_table(cur, rows, target, columns, conflict, fixups, report)
            if args.assistant_memory_schema:
                copy_assistant_memory(cur, args.assistant_memory_schema, args.replace, report)
        if args.dry_run:
            raw.rollback()
        else:
            raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()

    log.info("")
    log.info("%-32s %8s %8s %8s %10s", "table", "source", "copied", "skipped", "duplicate")
    for target, total, added, skipped, dupes in report:
        log.info("%-32s %8d %8d %8d %10d", target, total, added, skipped, dupes)
    log.info("")
    log.info("Dry run: nothing was written." if args.dry_run else "Done. The .db files were not touched; "
             "keep them until you've checked the portal, then archive them.")


if __name__ == "__main__":
    main()