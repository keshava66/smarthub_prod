"""
Compare every timestamp the SQLite files held with what landed in PostgreSQL.

    cd backend
    python scripts/check_timestamps.py

For each timestamp column it prints how many old values had no time zone
(the ones the import had to guess about), plus a few rows side by side:
the text as it sat in SQLite, and the same row from PostgreSQL shown in
Eastern time. Read-only on both sides.
"""

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import db  # noqa: E402

# (sqlite file, sqlite table, key column, timestamp column, postgres table)
COLUMNS = [
    ("audit_log.db", "audit_log", "id", "timestamp", "audit_log"),
    ("audit_log.db", "assistant_settings", "user_email", "updated_at", "assistant_settings"),
    ("communication_center.db", "email_templates", "id", "created_at", "email_templates"),
    ("communication_center.db", "email_history", "id", "sent_at", "email_history"),
    ("communication_center.db", "email_scheduled", "id", "send_at", "email_scheduled"),
    ("communication_center.db", "email_scheduled", "id", "created_at", "email_scheduled"),
    ("snowflake_query_runner.db", "query_history", "id", "created_at", "sf_query_history"),
    ("release_notes.db", "release_notes", "id", "created_at", "release_notes"),
    ("release_notes.db", "snapshots", "key", "taken_at", "release_note_snapshots"),
    ("data_api_exports.db", "data_apis", "id", "created_at", "data_apis"),
    ("data_api_exports.db", "data_apis", "id", "last_used_at", "data_apis"),
    ("data_api_exports.db", "data_api_requests", None, "ts", "data_api_requests"),
    ("data_api_exports.db", "data_api_embedding_models", "id", "created_at", "data_api_embedding_models"),
]


def has_zone(text):
    t = (text or "").strip()
    return t.endswith("Z") or "+" in t[10:] or "-" in t[10:]


def main():
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else BACKEND
    for filename, table, key, column, target in COLUMNS:
        path = source / filename
        if not path.exists():
            continue
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = con.execute(f'SELECT {key or "rowid"}, "{column}" FROM "{table}" '
                               f'WHERE "{column}" IS NOT NULL AND "{column}" <> ""').fetchall()
        except sqlite3.Error:
            con.close()
            continue
        con.close()
        naive = [(k, v) for k, v in rows if not has_zone(v)]
        print(f"\n{target}.{column}: {len(rows)} values, {len(naive)} without a time zone")
        if not naive or key is None:
            for k, v in naive[:3]:
                print(f"   sqlite: {v}")
            continue
        sample = naive[:3] + naive[-2:] if len(naive) > 5 else naive
        with db.transaction(read_only=True) as conn:
            for k, v in sample:
                row = conn.execute(f'SELECT "{column}" AS t FROM {target} WHERE {key} = ?', (k,)).fetchone()
                print(f"   sqlite: {v:<32}  postgres (Eastern): {row['t'] if row else '(missing)'}")


if __name__ == "__main__":
    main()