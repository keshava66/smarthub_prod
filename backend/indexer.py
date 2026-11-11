"""Index health check for any Postgres table.

Pick a table, get back what indexes it has, whether they're valid, and which
ones are missing for the usual ways a table gets queried: exact lookups,
filters and sorts, substring search (ILIKE), full-text search and vector
similarity. The missing ones can be built from here, with a live log the UI polls.
"""

from __future__ import annotations

import hashlib
import logging
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth import require_permission
from data_validation import DEFAULT_ENVIRONMENT, ENVIRONMENTS, _empty_or_none, _source_cfg, _validate_identifier
from data_validation_connectors import ConnectorError, PostgreSQLConnector, create_connector

logger = logging.getLogger(__name__)

index_health_router = APIRouter(prefix="/index-health", tags=["Index Health"])
PERMISSION = "viewPGindexer"
# DDL on prod is not a "view" right. Both names must be in auth.ALL_PERMISSIONS.
BUILD_PERMISSION = "buildPGindexer"

SOURCE = "postgresql"
# Must be the config the app's word search queries with, or the index is invisible to it.
TEXT_CONFIG = PostgreSQLConnector.TEXT_SEARCH_CONFIG
VECTOR_TYPES = {"vector", "halfvec", "sparsevec"}
TEXTISH = {"text", "varchar", "bpchar", "citext"}
TRGM_OK = {"text", "varchar"}  # gin_trgm_ops rejects bpchar and citext; those get an index on (col::text)
NO_BTREE = VECTOR_TYPES | {"json", "xml", "tsvector", "point", "polygon", "box", "circle", "line", "lseg", "path"}
METRIC_OPS = {"cosine_ops": "cosine", "l2_ops": "l2", "ip_ops": "inner_product"}
OPS_FOR_METRIC = {v: k for k, v in METRIC_OPS.items()}
# for naming existing indexes only; never suggested
ALL_METRICS = {**METRIC_OPS, "l1_ops": "l1", "hamming_ops": "hamming", "jaccard_ops": "jaccard"}
HNSW_MAX_DIMS = {"vector": 2000, "halfvec": 4000}

BUILD_MEM = os.environ.get("INDEX_HEALTH_BUILD_MEM", "1GB")
BUILD_WORKERS = int(os.environ.get("INDEX_HEALTH_BUILD_WORKERS", "4"))
if not re.fullmatch(r"\d{1,6}(kB|MB|GB)", BUILD_MEM):
    raise ValueError(f"INDEX_HEALTH_BUILD_MEM must look like 512MB or 1GB, got {BUILD_MEM!r}")
# Each build takes BUILD_MEM plus BUILD_WORKERS on the server; cap how many run at once.
MAX_RUNNING = int(os.environ.get("INDEX_HEALTH_MAX_RUNNING", "2"))
# btree can't hold a value over ~2.7 KB. Above this average width a text column
# almost certainly has some that big, so lookups get a hash index instead.
# ponytail: avg_width from pg_stats is a guess, not the max; the build log catches the rest.
WIDE_BYTES = int(os.environ.get("INDEX_HEALTH_WIDE_BYTES", "256"))
POLL_SECONDS = 2


COLUMNS_SQL = """
    SELECT a.attname, format_type(a.atttypid, a.atttypmod), COALESCE(b.typname, t.typname),
           a.attnotnull, s.avg_width, s.n_distinct
    FROM pg_attribute a
    JOIN pg_type t ON t.oid = a.atttypid
    LEFT JOIN pg_type b ON b.oid = t.typbasetype AND t.typtype = 'd'  -- a domain behaves like its base type
    JOIN pg_class c ON c.oid = a.attrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    LEFT JOIN pg_stats s ON s.schemaname = n.nspname AND s.tablename = c.relname AND s.attname = a.attname
                        AND s.inherited = (c.relkind = 'p')  -- parents with children have two rows per column
    WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped
    ORDER BY a.attnum
"""

INDEXES_SQL = """
    SELECT ix.relname, am.amname, i.indisvalid, i.indisready, i.indisprimary, i.indisunique,
           i.indnkeyatts, i.indpred IS NOT NULL, i.indexprs IS NOT NULL,
           pg_get_indexdef(i.indexrelid),
           pg_relation_size(i.indexrelid), pg_size_pretty(pg_relation_size(i.indexrelid)),
           COALESCE(s.idx_scan, 0),
           ARRAY(SELECT a.attname FROM unnest(i.indkey::smallint[]) WITH ORDINALITY k(attnum, ord)
                 LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
                 ORDER BY k.ord),
           ARRAY(SELECT o.opcname FROM unnest(i.indclass::oid[]) WITH ORDINALITY c(oid, ord)
                 JOIN pg_opclass o ON o.oid = c.oid ORDER BY c.ord),
           EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid),
           i.indcollation::oid[]
    FROM pg_index i
    JOIN pg_class ix ON ix.oid = i.indexrelid
    JOIN pg_am am ON am.oid = ix.relam
    LEFT JOIN pg_stat_all_indexes s ON s.indexrelid = i.indexrelid
    WHERE i.indrelid = %s::regclass
    ORDER BY ix.relname
"""

TABLE_SQL = """
    SELECT c.reltuples::bigint, pg_size_pretty(pg_total_relation_size(c.oid)),
           GREATEST(s.last_analyze, s.last_autoanalyze), s.n_live_tup, s.n_dead_tup, c.relkind
    FROM pg_class c
    LEFT JOIN pg_stat_all_tables s ON s.relid = c.oid
    WHERE c.oid = %s::regclass
"""

# Index names are unique per schema, not per table, so a name match alone
# could be some other table's index. `ours` says it's on this table and, for
# the key, a plain unique index on exactly that one column.
INDEX_STATE_SQL = """
    SELECT i.indisvalid AND i.indisready,
           i.indrelid = %(ref)s::regclass,
           i.indrelid = %(ref)s::regclass AND i.indisunique AND i.indnatts = 1
             AND i.indpred IS NULL AND i.indexprs IS NULL
             AND i.indkey[0] = (SELECT attnum FROM pg_attribute
                                WHERE attrelid = %(ref)s::regclass AND attname = %(col)s)
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %(schema)s AND c.relname = %(name)s
"""

# A unique index on the key column that no constraint owns yet can become the
# primary key as-is — no second hour-long build on a big table.
REUSABLE_UNIQUE_SQL = """
    SELECT c.relname
    FROM pg_index i
    JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_am am ON am.oid = c.relam
    WHERE i.indrelid = %(ref)s::regclass AND am.amname = 'btree'
      AND i.indisunique AND i.indisvalid AND i.indisready AND i.indnatts = 1
      AND i.indpred IS NULL AND i.indexprs IS NULL
      AND i.indkey[0] = (SELECT attnum FROM pg_attribute WHERE attrelid = %(ref)s::regclass AND attname = %(col)s)
      AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid)
    ORDER BY c.relname
    LIMIT 1
"""

PROGRESS_SQL = """
    SELECT phase, blocks_done, blocks_total, tuples_done, tuples_total, lockers_done, lockers_total
    FROM pg_stat_progress_create_index WHERE pid = %s
"""


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _ident(stem: str, suffix: str = "") -> str:
    """Postgres cuts names at 63 bytes; cutting blindly makes two different
    indexes share a name (the second build then 'skips' as already done)."""
    full = stem + suffix
    if len(full.encode()) <= 63:
        return full
    tag = "_" + hashlib.sha1(full.encode()).hexdigest()[:8]
    room = 63 - len((tag + suffix).encode())
    return stem.encode()[:room].decode(errors="ignore") + tag + suffix


def index_name(table: str, col: str, kind: str) -> str:
    raw = f"idx_{table}_{col}"
    clean = re.sub(r"\W", "_", raw.lower())
    if clean != raw:
        # "Name" and "name", or "a b" and "a_b", would otherwise get the same index name
        clean += "_" + hashlib.sha1(raw.encode()).hexdigest()[:6]
    return _ident(clean, f"_{kind}")


def _usable(ix: dict) -> bool:
    return ix["valid"] and ix["ready"] and not ix["partial"]


def _leads(ix: dict, col: str) -> bool:
    return bool(ix["columns"]) and ix["columns"][0] == col


def coverage_for(col: dict, indexes: List[dict]) -> dict:
    name = col["name"]
    live = [ix for ix in indexes if _usable(ix)]
    btree = [ix for ix in live if ix["method"] == "btree" and _leads(ix, name)]
    hashed = [ix for ix in live if ix["method"] == "hash" and _leads(ix, name)]
    trgm = [ix for ix in live if ix["method"] in ("gin", "gist") and _leads(ix, name)
            and ix["opclasses"][0].endswith("_trgm_ops")]
    colpat = f"(?:{re.escape(name)}|{re.escape(q(name))})"
    # citext/bpchar: the app searches CAST(col AS TEXT) ILIKE, which an index on ((col)::text) serves
    cast_re = re.compile(rf"USING gi(?:n|st) \(\(\({colpat}\)::text\) (?:[^\s(]+\.)?gi(?:n|st)_trgm_ops")
    trgm += [ix for ix in live if ix["expression"] and cast_re.search(ix["definition"])]
    # Full-text queries write to_tsvector('english', col); the planner only uses an index built on that exact expression.
    # Postgres prints varchar/bpchar/citext with a cast: to_tsvector('english'::regconfig, (col)::text).
    fts_re = re.compile(rf"to_tsvector\('{TEXT_CONFIG}'::regconfig, (?:{colpat}|\({colpat}\)::text)\)")
    fts = [ix for ix in live if ix["method"] == "gin" and fts_re.search(ix["definition"])]
    ann = [ix for ix in live if ix["method"] in ("hnsw", "ivfflat") and _leads(ix, name)]
    metrics = sorted({ALL_METRICS.get(ix["opclasses"][0].removeprefix(col["typname"] + "_"), "?") for ix in ann})
    prefix = trgm + [ix for ix in btree if ix["opclasses"][0].endswith("_pattern_ops")]
    return {
        "column": name,
        "type": col["type"],
        "exact": [ix["name"] for ix in btree + hashed],
        "sort": [ix["name"] for ix in btree if not ix["opclasses"][0].endswith("_pattern_ops")],
        "starts_with": [ix["name"] for ix in prefix],
        "substring": [ix["name"] for ix in trgm],
        "words": [ix["name"] for ix in fts],
        "vector": [ix["name"] for ix in ann],
        "vector_metrics": metrics,
    }


NOT_KEYABLE = NO_BTREE | {"jsonb", "bytea", "bool"}


def key_candidates(table: str, columns: List[dict], indexes: List[dict]) -> List[str]:
    """Columns that could be the primary key, likeliest first. Only a ranking —
    the build still checks the chosen one for NULLs and duplicates."""
    unique_single = {ix["columns"][0] for ix in indexes
                     if ix["unique"] and _usable(ix) and ix["method"] == "btree" and ix["nkey"] == 1 and not ix["expression"]}
    stem = table.lower().rstrip("s")

    def score(c):
        n = c["name"].lower()
        return (
            c["name"] not in unique_single,
            c.get("n_distinct") != -1,          # pg_stats: -1 means every row has its own value
            not (n == "id" or (n.endswith("_id") and n[:-3] in stem)),
            not n.endswith("_id"),
            not c["notnull"],
            (c.get("avg_width") or 0) > 64,
        )

    ok = [c for c in columns if c["typname"] not in NOT_KEYABLE and not c["typname"].startswith("_")]
    return [c["name"] for c in sorted(ok, key=score)]


def key_names(table: str):
    return _ident(table, "_pkey"), _ident(table, "_key_not_null")


KEY_PLACEHOLDER = "<id_column>"


def key_statements(schema: str, table: str, col: str) -> List[str]:
    """What the key build runs, in order. Shown to the user as-is; _build_key runs the real thing."""
    ref, pk, nn = f"{q(schema)}.{q(table)}", *key_names(table)
    c = col if col == KEY_PLACEHOLDER else q(col)
    return [
        f"-- check {col} for empty and duplicate values (stops here if any)",
        f"CREATE UNIQUE INDEX CONCURRENTLY {q(pk)} ON {ref} ({c});",
        f"ALTER TABLE {ref} ADD CONSTRAINT {q(nn)} CHECK ({c} IS NOT NULL) NOT VALID;",
        f"ALTER TABLE {ref} VALIDATE CONSTRAINT {q(nn)};",
        f"ALTER TABLE {ref} ALTER COLUMN {c} SET NOT NULL;",
        f"ALTER TABLE {ref} DROP CONSTRAINT {q(nn)};",
        f"ALTER TABLE {ref} ADD CONSTRAINT {q(pk)} PRIMARY KEY USING INDEX {q(pk)};",
    ]


def find_key(columns: List[dict], indexes: List[dict]) -> Optional[dict]:
    """A usable row key: a valid unique btree with no expressions, no predicate
    and every key column NOT NULL. Primary key first."""
    notnull = {c["name"] for c in columns if c["notnull"]}
    picks = [ix for ix in indexes
             if ix["unique"] and ix["method"] == "btree" and _usable(ix) and not ix["expression"]
             and all(c in notnull for c in ix["columns"][:ix["nkey"]])]
    picks.sort(key=lambda ix: (not ix["primary"], ix["nkey"], ix["name"]))
    return picks[0] if picks else None


def _flag_indexes(indexes: List[dict]) -> None:
    by_def = {}
    # constraint-backed indexes first, so of two identical ones the droppable one is flagged
    for ix in sorted(indexes, key=lambda ix: (not ix.get("constraint"), ix["name"])):
        ix["status"], ix["note"] = "ok", ""
        if not ix["valid"] or not ix["ready"]:
            ix["status"] = "invalid"
            ix["note"] = "Unusable — the planner ignores it, but every write still pays for it. Usually a failed CONCURRENTLY build."
            continue
        same = re.sub(r'INDEX (?:"(?:[^"]|"")+"|\S+) ON', "INDEX ON", ix["definition"], count=1)
        if same in by_def:
            ix["status"], ix["note"] = "redundant", f"Exact duplicate of {by_def[same]}."
            continue
        by_def[same] = ix["name"]

    plain = [ix for ix in indexes if ix["status"] == "ok" and ix["method"] == "btree"
             and not ix["partial"] and not ix["expression"]]
    for a in plain:
        if a["unique"]:
            continue
        # key columns only — INCLUDE columns can't be searched or sorted on
        ak = a["columns"][:a["nkey"]]
        for b in plain:
            bk = b["columns"][:b["nkey"]]
            # same opclass and collation too: (x COLLATE "C") serves LIKE 'abc%', (x, y) doesn't
            if a is not b and len(bk) > len(ak) and bk[:len(ak)] == ak \
                    and b["opclasses"][:len(a["opclasses"])] == a["opclasses"] \
                    and b.get("collations", [])[:len(ak)] == a.get("collations", [])[:len(ak)]:
                a["status"], a["note"] = "redundant", f"{b['name']} starts with the same column(s) and covers these lookups."
                break

    for ix in indexes:
        if ix["status"] == "ok" and ix["scans"] == 0 and not ix["unique"]:
            ix["status"], ix["note"] = "unused", "No scans since stats were last reset. Check again after real traffic before dropping."


def analyze(schema: str, table: str, columns: List[dict], indexes: List[dict], extensions: set,
            search: Optional[List[str]] = None, filters: Optional[List[str]] = None,
            vectors: Optional[List[str]] = None, table_info: Optional[dict] = None) -> dict:
    """Pure function, no DB — everything the endpoint reports is decided here.

    search / filters / vectors are the columns the user said get queried that
    way. Only those get "missing" suggestions; nothing is assumed."""
    ref = f"{q(schema)}.{q(table)}"
    # dedupe, keep order; a repeated column would give two fixes with one id
    search, filters, vectors = (list(dict.fromkeys(x or [])) for x in (search, filters, vectors))
    names = {c["name"] for c in columns}
    for col in search + filters + vectors:
        if col not in names:
            raise ValueError(f"Column '{col}' is not on {schema}.{table}.")

    _flag_indexes(indexes)
    coverage = [coverage_for(c, indexes) for c in columns]
    cov = {c["column"]: c for c in coverage}
    types = {c["name"]: c["typname"] for c in columns}
    missing = []

    # Opclasses live in the extension's schema; if that schema isn't on search_path
    # the bare name fails ("operator class gin_trgm_ops does not exist"). Qualify it.
    def ops(ext, name):
        s = extensions.get(ext) if isinstance(extensions, dict) else None
        return f"{q(s)}.{name}" if s else name

    # Every fix carries its own statements, generated here. The build endpoint
    # only ever runs these — never SQL sent from the browser.
    def need(id, priority, column, feature, why, statements, index=None, buildable=True):
        missing.append({"id": id, "priority": priority, "column": column, "feature": feature, "why": why,
                        "sql": "\n".join(statements), "statements": statements, "index": index,
                        "buildable": buildable})

    for ix in indexes:
        if ix["status"] == "invalid" and ix.get("constraint"):
            need(f"drop:{ix['name']}", "critical", ", ".join(c or "(expr)" for c in ix["columns"]), "drop",
                 f"{ix['name']} is invalid but backs a constraint, so it can't be dropped on its own — "
                 f"drop and re-add the constraint by hand.", [f"-- ALTER TABLE {ref} DROP CONSTRAINT ...;"],
                 buildable=False)
        elif ix["status"] == "invalid":
            # Drop, not REINDEX: whatever made the build fail (too-long values, a
            # bad expression) makes a rebuild fail the same way. If it was a one-off
            # (cancel, lost connection), the "missing" list offers to create it again.
            need(f"drop:{ix['name']}", "critical", ", ".join(c or "(expr)" for c in ix["columns"]), "drop",
                 f"{ix['name']} is invalid (a failed or cancelled build). The planner ignores it but every "
                 f"write still updates it.",
                 [f"DROP INDEX CONCURRENTLY IF EXISTS {q(schema)}.{q(ix['name'])};"])

    kind = (table_info or {}).get("kind", "r")
    key = find_key(columns, indexes)
    if not key:
        cands = key_candidates(table, columns, indexes)
        need("key", "critical", None, "key",
             "No primary key (or NOT NULL unique btree). Row lookups, stable ordering and keyset paging all "
             "fall back to scanning and sorting the table. Pick the id column — the build checks it for empty "
             "and duplicate values before touching anything. If it already has a unique index, that index "
             "becomes the key instead of building a second one.",
             key_statements(schema, table, KEY_PLACEHOLDER), buildable=bool(cands) and kind == "r")
        missing[-1]["candidates"] = cands
        if kind == "m":
            missing[-1]["why"] = ("Materialized view with no unique index — REFRESH ... CONCURRENTLY needs one. "
                                  "Views can't have a primary key; add a unique index on the id column by hand.")

    if table_info and table_info.get("last_analyze") is None:
        need("stats", "high", None, "stats",
             "Table has never been analyzed — the planner is guessing and may skip good indexes.",
             [f"ANALYZE {ref};"])

    for c in columns:
        if c["typname"] not in VECTOR_TYPES or c["name"] not in vectors:
            continue
        name = c["name"]
        dims = re.search(r"\((\d+)\)", c["type"])
        dims = int(dims.group(1)) if dims else None
        limit = HNSW_MAX_DIMS.get(c["typname"])
        if not cov[name]["vector"]:
            if "vector" not in extensions:
                continue
            ix = index_name(table, name, "hnsw")
            why = "Vector column with no HNSW/IVFFlat index — every similarity search reads the whole table."
            ok = True
            if c["typname"] != "sparsevec" and dims is None:
                why += " The column has no fixed size (vector without (n)), and HNSW needs one — alter the type first."
                ok = False
            elif limit and dims > limit:
                why += (f" {dims} dimensions is over HNSW's {limit} limit for {c['typname']}; index a halfvec "
                        f"expression or reduce dimensions — do it by hand.")
                ok = False
            need(f"vector:{name}", "critical", name, "vector", why,
                 [f"CREATE INDEX CONCURRENTLY {q(ix)} ON {ref} USING hnsw ({q(name)} {ops('vector', c['typname'] + '_cosine_ops')});"], ix,
                 buildable=ok)
            continue
        for m in METRIC_OPS.values():
            if m in cov[name]["vector_metrics"]:
                continue
            ix = index_name(table, name, f"hnsw_{m}")
            need(f"vector_{m}:{name}", "info", name, "vector",
                 f"ANN index only serves {', '.join(cov[name]['vector_metrics'])}. {m} searches will scan the "
                 f"table. Skip unless callers use {m}.",
                 [f"CREATE INDEX CONCURRENTLY {q(ix)} ON {ref} USING hnsw ({q(name)} {ops('vector', c['typname'] + '_' + OPS_FOR_METRIC[m])});"], ix)

    trgm_needed = False
    for name in search:
        if types[name] not in TEXTISH:
            raise ValueError(f"'{name}' is {types[name]}, not text — it can't be text-searched.")
        if not cov[name]["substring"]:
            ix = index_name(table, name, "trgm")
            if types[name] in TRGM_OK:
                trgm_needed = True
                need(f"substring:{name}", "high", name, "substring",
                     "Substring search (ILIKE '%term%') reads the whole table without a trigram index.",
                     [f"CREATE INDEX CONCURRENTLY {q(ix)} ON {ref} USING gin ({q(name)} {ops('pg_trgm', 'gin_trgm_ops')});"], ix)
            else:
                trgm_needed = True
                need(f"substring:{name}", "high", name, "substring",
                     f"Substring search (CAST(col AS TEXT) ILIKE '%term%') reads the whole table. gin_trgm_ops "
                     f"doesn't take {types[name]} directly, so this indexes the text cast the search uses.",
                     [f"CREATE INDEX CONCURRENTLY {q(ix)} ON {ref} USING gin (({q(name)}::text) {ops('pg_trgm', 'gin_trgm_ops')});"], ix)
        if not cov[name]["words"]:
            ix = index_name(table, name, "fts")
            need(f"words:{name}", "high", name, "words",
                 f"Full-text search (to_tsvector('{TEXT_CONFIG}', col) @@ …) needs a GIN index on that exact expression.",
                 [f"CREATE INDEX CONCURRENTLY {q(ix)} ON {ref} USING gin (to_tsvector('{TEXT_CONFIG}', {q(name)}));"], ix)

    widths = {c["name"]: c.get("avg_width") for c in columns}
    for name in filters:
        if cov[name]["exact"]:
            continue
        if types[name] in NO_BTREE:
            raise ValueError(f"'{name}' is {types[name]} — it has no ordering, so it can't be a lookup/sort column.")
        width = widths.get(name) or 0
        if types[name] in TEXTISH and width > WIDE_BYTES:
            ix = index_name(table, name, "hash")
            need(f"filter:{name}", "high", name, "filter",
                 f"Values here are long (avg {width} bytes) and a btree can't hold any over ~2.7 KB. A hash index "
                 f"handles = and IN of any length, but not sorting or ranges.",
                 [f"CREATE INDEX CONCURRENTLY {q(ix)} ON {ref} USING hash ({q(name)});"], ix)
        else:
            ix = index_name(table, name, "btree")
            need(f"filter:{name}", "high", name, "filter",
                 "Lookups (=, IN, <, >) and ORDER BY on this column need a btree index led by it.",
                 [f"CREATE INDEX CONCURRENTLY {q(ix)} ON {ref} ({q(name)});"], ix)

    for name in vectors:
        if types[name] not in VECTOR_TYPES:
            raise ValueError(f"'{name}' is {types[name]}, not a vector column.")

    if trgm_needed and "pg_trgm" not in extensions:
        need("extension:pg_trgm", "critical", None, "extension",
             "pg_trgm is not installed; the trigram indexes need it. Build this first.",
             ["CREATE EXTENSION IF NOT EXISTS pg_trgm;"])

    if kind == "p":
        # CONCURRENTLY doesn't work on a partitioned parent and a plain build blocks writes.
        for m in missing:
            if m["buildable"] and "CONCURRENTLY" in m["sql"]:
                m["buildable"] = False
                m["why"] += (" Partitioned table: build it on each partition CONCURRENTLY, then attach "
                             "(CREATE INDEX ON ONLY parent + ALTER INDEX ... ATTACH PARTITION) — by hand.")

    order = {"critical": 0, "high": 1, "info": 2}
    missing.sort(key=lambda m: (order[m["priority"]], m["feature"] != "extension"))

    for ix in indexes:
        if ix["status"] == "redundant":
            if ix.get("constraint"):
                ix["note"] += " It backs a constraint, so drop the constraint (ALTER TABLE ... DROP CONSTRAINT) instead."
            else:
                ix["drop_sql"] = f"DROP INDEX CONCURRENTLY {q(schema)}.{q(ix['name'])};"

    # Redundant and unused indexes the UI can offer to drop. Kept out of `missing`
    # so they never count as gaps; the build accepts ids from either list.
    cleanup = []
    for ix in indexes:
        if ix["status"] not in ("redundant", "unused"):
            continue
        ok = not ix.get("constraint") and not ix["primary"] and kind != "p"
        why = ix["note"]
        if kind == "p":
            why += " Partitioned table: drop it on each partition by hand."
        cleanup.append({
            "id": f"drop:{ix['name']}", "priority": "info", "column": ", ".join(c or "(expr)" for c in ix["columns"]),
            "feature": "drop", "status": ix["status"], "why": why, "index": None, "buildable": ok,
            "size": ix["size"], "scans": ix["scans"], "method": ix["method"],
            "statements": [f"DROP INDEX CONCURRENTLY IF EXISTS {q(schema)}.{q(ix['name'])};"],
        })
        cleanup[-1]["sql"] = cleanup[-1]["statements"][0]

    return {
        "table": {"schema": schema, "name": table, **(table_info or {})},
        "extensions": {"pg_trgm": "pg_trgm" in extensions, "vector": "vector" in extensions},
        "key": {"index": key["name"] if key else None,
                "columns": key["columns"][:key["nkey"]] if key else []},
        "checked": {"search": search, "filter": filters, "vector": vectors},
        "columns": [{"name": c["name"], "type": c["type"], "text": c["typname"] in TEXTISH,
                     "vector": c["typname"] in VECTOR_TYPES} for c in columns],
        "indexes": [{k: v for k, v in ix.items() if k not in ("nkey", "size_bytes", "collations")} for ix in indexes],
        "coverage": coverage,
        "missing": missing,
        "cleanup": cleanup,
        "summary": {
            "fast": not any(m["priority"] in ("critical", "high") for m in missing),
            "indexes": len(indexes),
            "invalid": sum(ix["status"] == "invalid" for ix in indexes),
            "redundant": sum(ix["status"] == "redundant" for ix in indexes),
            "unused": sum(ix["status"] == "unused" for ix in indexes),
            "critical": sum(m["priority"] == "critical" for m in missing),
            "high": sum(m["priority"] == "high" for m in missing),
        },
    }


def _split(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


@contextmanager
def _pg(env, database):
    """A fresh read-only connection per call; nothing here is hot enough to need a pool."""
    src = _open(env, database, read_only=True)
    try:
        yield src
    finally:
        src.disconnect()


def _env(env: Optional[str]) -> str:
    e = (env or DEFAULT_ENVIRONMENT).strip().lower()
    if e not in ENVIRONMENTS:
        raise HTTPException(400, f"Unknown environment '{env}'. Use one of: {', '.join(ENVIRONMENTS)}.")
    return e


def _clean_target(database, schema, table):
    database = _empty_or_none(database)  # a cleared picker sends ''
    _validate_identifier(database, "database")
    _validate_identifier(schema, "schema")
    _validate_identifier(table, "table")
    # Names come from the catalog via the tree and are always quoted here, so
    # keep their case — lowercasing would 404 on a "MixedCase" table.
    return database, schema, table


def _read_table(env, database, schema, table):
    ref = f"{q(schema)}.{q(table)}"
    try:
        with _pg(env, database) as src:
            with src.conn.cursor() as cur:
                cur.execute("SELECT to_regclass(%s)", (ref,))
                if cur.fetchone()[0] is None:
                    raise HTTPException(404, f"Table {schema}.{table} not found.")
                cur.execute(COLUMNS_SQL, (ref,))
                columns = [{"name": n, "type": t, "typname": tn, "notnull": nn, "avg_width": w, "n_distinct": d}
                           for n, t, tn, nn, w, d in cur.fetchall()]
                cur.execute(INDEXES_SQL, (ref,))
                indexes = [{
                    "name": r[0], "method": r[1], "valid": r[2], "ready": r[3], "primary": r[4],
                    "unique": r[5], "nkey": r[6], "partial": r[7], "expression": r[8], "definition": r[9],
                    "size_bytes": r[10], "size": r[11], "scans": r[12], "columns": list(r[13]),
                    "opclasses": list(r[14]), "constraint": r[15], "collations": list(r[16]),
                } for r in cur.fetchall()]
                cur.execute(TABLE_SQL, (ref,))
                rows, size, analyzed, live, dead, kind = cur.fetchone()
                if kind not in ("r", "p", "m"):
                    raise HTTPException(400, f"{schema}.{table} is a view or foreign table — it has no indexes of its own.")
                cur.execute("SELECT e.extname, n.nspname FROM pg_extension e "
                            "JOIN pg_namespace n ON n.oid = e.extnamespace")
                extensions = dict(cur.fetchall())  # name -> schema it lives in
    except HTTPException:
        raise
    except ConnectorError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        if getattr(e, "pgcode", None) == "55P03":
            raise HTTPException(409, f"{schema}.{table} is locked by another session (a migration, VACUUM FULL, "
                                     f"or an ALTER TABLE). Try again in a minute.")
        logger.exception("index health: reading %s.%s on %s failed", schema, table, env)
        raise HTTPException(400, f"Could not read indexes for {schema}.{table}: {e}")

    table_info = {
        "database": database, "env": env,
        "rows_estimate": rows if rows is not None and rows >= 0 else None,
        "size": size, "live_rows": live, "dead_rows": dead,
        "last_analyze": analyzed.isoformat() if analyzed else None,
        "kind": kind,
    }
    return columns, indexes, extensions, table_info


def run_check(env, database, schema, table, search=None, filters=None, vectors=None) -> dict:
    columns, indexes, extensions, table_info = _read_table(env, database, schema, table)
    try:
        return analyze(schema, table, columns, indexes, extensions, search=search,
                       filters=filters, vectors=vectors, table_info=table_info)
    except ValueError as e:
        raise HTTPException(400, str(e))


def describe_columns(columns: List[dict], indexes: List[dict]) -> List[dict]:
    """Columns for the usage picker. `suggested` pre-ticks whatever the existing
    indexes say the table was already built for."""
    _flag_indexes(indexes)
    out = []
    for c in columns:
        cov = coverage_for(c, indexes)
        text, vector = c["typname"] in TEXTISH, c["typname"] in VECTOR_TYPES
        suggested = []
        if cov["exact"]:
            suggested.append("filter")
        if text and (cov["substring"] or cov["words"]):
            suggested.append("search")
        if vector and cov["vector"]:
            suggested.append("vector")
        out.append({"name": c["name"], "type": c["type"], "text": text, "vector": vector,
                    "lookup": c["typname"] not in NO_BTREE, "suggested": suggested})
    return out


def _browse(env, database, call):
    try:
        with _pg(env, database) as src:
            return call(src)
    except HTTPException:
        raise
    except ConnectorError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("index health: browsing %s failed", env)
        raise HTTPException(502, f"Could not reach PostgreSQL ({env}): {e}")


# Only databases this login may connect to; templates and datallowconn=false (rdsadmin) are skipped.
DATABASES_SQL = """
    SELECT datname FROM pg_database
    WHERE NOT datistemplate AND datallowconn AND has_database_privilege(datname, 'CONNECT')
    ORDER BY datname
"""


def _databases(src):
    src._ensure_conn(src.maintenance_database)
    with src.conn.cursor() as cur:
        cur.execute(DATABASES_SQL)
        return [r[0] for r in cur.fetchall()]


@index_health_router.get("/databases")
def list_databases(env: str = Query(DEFAULT_ENVIRONMENT), auth: dict = Depends(require_permission(PERMISSION))):
    return {"databases": _browse(_env(env), None, _databases)}


@index_health_router.get("/schemas")
def list_schemas(database: str = Query(...), env: str = Query(DEFAULT_ENVIRONMENT),
                 auth: dict = Depends(require_permission(PERMISSION))):
    _validate_identifier(database, "database")
    return {"schemas": _browse(_env(env), database, lambda s: s.list_schemas(database))}


@index_health_router.get("/tables")
def list_tables(database: str = Query(...), schema: str = Query(...), env: str = Query(DEFAULT_ENVIRONMENT),
                auth: dict = Depends(require_permission(PERMISSION))):
    _validate_identifier(database, "database")
    _validate_identifier(schema, "schema")
    return {"tables": _browse(_env(env), database, lambda s: _tables(s, schema))}


# Own query, not the connector's list_tables: that one lowercases the schema (so
# "My Schema" lists nothing) and lists views, which have no indexes, but skips
# materialized views, which do. Partitions are listed: that is where a partitioned
# table's indexes get built.
TABLES_SQL = """
    SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = %s AND c.relkind IN ('r', 'p', 'm')
      AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY c.relname
"""


def _tables(src, schema):
    with src.conn.cursor() as cur:
        cur.execute(TABLES_SQL, (schema,))
        return [r[0] for r in cur.fetchall()]


# Everything the picker needs in one query per database. Sizes come from relpages
# (as of the last VACUUM/ANALYZE), not pg_total_relation_size, which would queue
# behind any exclusive lock on any table in the database.
CATALOG_SQL = """
    SELECT n.nspname, c.relname, c.relkind, c.reltuples::bigint,
           (c.relpages::bigint
            + COALESCE((SELECT t.relpages FROM pg_class t WHERE t.oid = c.reltoastrelid), 0)
            + COALESCE((SELECT sum(x.relpages) FROM pg_index i JOIN pg_class x ON x.oid = i.indexrelid
                        WHERE i.indrelid = c.oid), 0)) * current_setting('block_size')::bigint,
           (SELECT count(*) FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped),
           (SELECT count(*) FROM pg_index i WHERE i.indrelid = c.oid)
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relkind IN ('r', 'p', 'm')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
      AND n.nspname NOT LIKE 'pg_toast%%' AND n.nspname NOT LIKE 'pg_temp%%'
      AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT')
    ORDER BY 1, 2
    LIMIT %s
"""
# ponytail: hard cap per database; page it if a database ever has more tables than this
CATALOG_MAX = int(os.environ.get("INDEX_HEALTH_CATALOG_MAX", "5000"))


def _pretty(n: int) -> str:
    for unit in ("bytes", "kB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


def _catalog_db(env, database):
    try:
        with _pg(env, database) as src:
            with src.conn.cursor() as cur:
                cur.execute(CATALOG_SQL, (CATALOG_MAX + 1,))
                rows = cur.fetchall()
    except Exception as e:
        logger.warning("index health: catalog of %s on %s failed: %s", database, env, e)
        return {"name": database, "schemas": [], "tables": 0, "error": str(e).strip().splitlines()[0][:300]}
    schemas = {}
    for schema, name, kind, tuples, size, ncols, nidx in rows[:CATALOG_MAX]:
        schemas.setdefault(schema, []).append({
            "name": name, "kind": kind, "rows": tuples if tuples is not None and tuples >= 0 else None,
            "size_bytes": size, "size": _pretty(size), "columns": ncols, "indexes": nidx,
        })
    return {"name": database, "tables": min(len(rows), CATALOG_MAX), "truncated": len(rows) > CATALOG_MAX,
            "schemas": [{"name": s, "tables": t} for s, t in schemas.items()]}


def _catalog(env):
    """Yield each readable database as soon as it's read. Databases that won't connect, or
    where this login can read no table, are left out: the picker only shows what can be checked."""
    names = _browse(env, None, _databases)
    yield {"type": "databases", "names": names}
    tables, hidden, first_error = 0, 0, None
    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in as_completed([pool.submit(_catalog_db, env, d) for d in names]):
            d = f.result()
            if d.get("error") or not d["schemas"]:
                hidden += 1
                first_error = first_error or d.get("error")
                yield {"type": "skip", "name": d["name"]}
                continue
            tables += d["tables"]
            yield {"type": "db", "db": d}
    # every database failing is a cluster problem, not a permissions one; say so
    yield {"type": "done", "tables": tables, "hidden": hidden,
           "error": first_error if names and hidden == len(names) else None}


@index_health_router.get("/catalog")
def catalog(env: str = Query(DEFAULT_ENVIRONMENT), auth: dict = Depends(require_permission(PERMISSION))):
    """Whole catalog in one response, for scripts. The page uses /catalog/stream."""
    env = _env(env)
    out, done = [], {}
    for ev in _catalog(env):
        if ev["type"] == "db":
            out.append(ev["db"])
        elif ev["type"] == "done":
            done = ev
    return {"env": env, "databases": sorted(out, key=lambda d: d["name"]), "tables": done.get("tables", 0),
            "hidden": done.get("hidden", 0)}


@index_health_router.get("/catalog/stream")
def catalog_stream(env: str = Query(DEFAULT_ENVIRONMENT), auth: dict = Depends(require_permission(PERMISSION))):
    """NDJSON, one event per line: databases (names, so the UI can draw a skeleton per database),
    then db / skip as each one finishes, then done."""
    env = _env(env)
    events = _catalog(env)
    first = next(events)  # list the databases before streaming, so a dead cluster is a real HTTP error

    def lines():
        yield json.dumps(first) + "\n"
        try:
            for ev in events:
                yield json.dumps(ev, default=str) + "\n"
        except Exception as e:  # headers are gone by now; report in-band
            logger.exception("index health: catalog stream on %s failed", env)
            yield json.dumps({"type": "done", "tables": 0, "hidden": 0, "error": str(e)}) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@index_health_router.get("")
def check_table_indexes(
    table: str = Query(...),
    schema: str = Query(...),
    database: Optional[str] = Query(None),
    env: str = Query(DEFAULT_ENVIRONMENT),
    search: Optional[str] = Query(None, description="Comma list of columns that get text-searched (ILIKE / full-text)."),
    filter: Optional[str] = Query(None, description="Comma list of columns that get looked up, filtered or sorted on."),
    vector: Optional[str] = Query(None, description="Comma list of vector columns that get similarity-searched."),
    auth: dict = Depends(require_permission(PERMISSION)),
):
    database, schema, table = _clean_target(database, schema, table)
    return run_check(_env(env), database, schema, table, _split(search), _split(filter), _split(vector))


@index_health_router.get("/columns")
def table_columns(
    table: str = Query(...),
    schema: str = Query(...),
    database: Optional[str] = Query(None),
    env: str = Query(DEFAULT_ENVIRONMENT),
    auth: dict = Depends(require_permission(PERMISSION)),
):
    database, schema, table = _clean_target(database, schema, table)
    columns, indexes, _, table_info = _read_table(_env(env), database, schema, table)
    return {"table": {"schema": schema, "name": table, **table_info},
            "columns": describe_columns(columns, indexes)}


# ============================================================================
# BUILD — runs selected fixes in a background thread; the UI polls the log.
#
# The build keeps going if the browser closes (that's the point: a GIN index on
# a big table takes an hour). Jobs live in process memory, same as the query
# runners — fine on the single worker the Readme asks for.
# ponytail: in-memory job store; move to a table if this ever runs on more than one worker.
# ============================================================================

_jobs: dict = {}
_active: dict = {}           # (env, db, schema, table) -> job id
_jobs_lock = threading.Lock()
MAX_JOBS = 50


class BuildRequest(BaseModel):
    env: str = DEFAULT_ENVIRONMENT
    database: Optional[str] = None
    schema_name: str
    table: str
    items: List[str]
    search: Optional[List[str]] = None
    filter: Optional[List[str]] = None
    vector: Optional[List[str]] = None
    key_column: Optional[str] = None


class Job:
    def __init__(self, target, items, user):
        self.id = uuid.uuid4().hex[:12]
        self.target = target
        self.items = items
        self.user = user
        self.status = "queued"
        self.log = []
        self.pid = None
        self.current = None
        self.cancelled = False
        self.started = datetime.now().isoformat(timespec="seconds")
        self.finished = None
        self.t0 = time.monotonic()        # whole job
        self.step_t0 = time.monotonic()   # current statement; what "elapsed" means in the log

    def say(self, msg, level="info"):
        self.log.append({"ts": datetime.now().strftime("%H:%M:%S"), "level": level, "msg": msg})
        logger.info("index build %s: %s", self.id, msg)


READ_TIMEOUT = "30s"


def _open(env, database, read_only=False):
    src = create_connector(SOURCE, _source_cfg(SOURCE, env))
    src._api_read_only = read_only  # the connector opens the session read-only when this is set
    src._ensure_conn(database)
    if read_only:
        # pg_relation_size waits behind an exclusive lock (VACUUM FULL, a migration);
        # fail the request instead of hanging it
        try:
            with src.conn.cursor() as cur:
                cur.execute(f"SET statement_timeout = '{READ_TIMEOUT}'")
                cur.execute("SET lock_timeout = '5s'")
        except Exception:
            src.disconnect()
            raise
    return src


def _eta(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60:02d}m"


def _took(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 60 else _clock(seconds)


ACTIVITY_SQL = """
    SELECT wait_event_type, wait_event, pg_blocking_pids(pid)
    FROM pg_stat_activity WHERE pid = %s AND state = 'active'
"""


def _watch(job: Job, env, database, stop: threading.Event):
    """Second connection; the build session is stuck inside CREATE INDEX and can't report on itself.

    The time left is the current phase's pace so far projected to 100%. Each
    phase runs at its own speed (scan, sort, then for CONCURRENTLY a second
    validation pass), so the estimate covers this phase only."""
    src = None
    phase_seen, phase_t0, phase_pct0 = None, 0.0, 0.0
    last_pct, last_said = -10.0, 0.0
    while not stop.wait(POLL_SECONDS):
        if not job.pid:
            continue
        try:
            if src is None:
                src = _open(env, database, read_only=True)
            with src.conn.cursor() as cur:
                cur.execute(PROGRESS_SQL, (job.pid,))
                row = cur.fetchone()
                if not row:
                    # Not a CREATE INDEX (the pre-check scan, VALIDATE, ANALYZE…), so no progress
                    # view. Still say something every 30s, and say so when it's stuck behind a lock.
                    cur.execute(ACTIVITY_SQL, (job.pid,))
                    act = cur.fetchone()
            now = time.monotonic()
            if not row:
                phase_seen = None
                if act and now - max(last_said, job.step_t0) >= 30:
                    wait_type, wait, blockers = act
                    if wait_type == "Lock":
                        job.say(f"waiting for a {wait} lock held by other session(s) {blockers} · "
                                f"{_clock(now - job.step_t0)} elapsed", "warn")
                    else:
                        job.say(f"still running · {_clock(now - job.step_t0)} elapsed", "progress")
                    last_said = now
                continue
            phase, bd, bt, td, tt, ld, lt = row
            done, total = (bd, bt) if bt else (td, tt) if tt else (ld, lt)
            pct = 100.0 * done / total if total else None
            new_phase = phase != phase_seen
            if new_phase:
                phase_seen, phase_t0, phase_pct0 = phase, now, pct or 0.0
                # Parallel builds report a scan as complete for a moment before the
                # workers start counting; don't show that or measure pace from it.
                if pct is not None and pct >= 99.5 and phase.endswith("scanning table"):
                    pct, phase_pct0 = None, 0.0
            elif pct is not None and pct < phase_pct0:
                phase_t0, phase_pct0 = now, pct
            # every 5%, on a phase change, or at least every 30s so a slow phase still shows life
            if not (new_phase or (pct is not None and pct - last_pct >= 5) or now - last_said >= 30):
                continue
            line = phase
            if phase.startswith("waiting for"):
                # CONCURRENTLY waits for every older transaction touching the table; name them.
                with src.conn.cursor() as cur:
                    cur.execute(ACTIVITY_SQL, (job.pid,))
                    act = cur.fetchone()
                if act and act[2]:
                    line += f" (held up by session(s) {act[2]} — a long query or open transaction)"
            if pct is not None:
                line += f" — {pct:.0f}% ({done:,}/{total:,})"
                gained, spent = pct - phase_pct0, now - phase_t0
                if gained >= 1 and spent >= 10:
                    left = spent / gained * (100 - pct)
                    line += ", almost done" if left < 1 else f", ~{_eta(left)} left in this phase"
            line += f" · {_clock(now - job.step_t0)} elapsed"
            job.say(line, "progress")
            last_pct, last_said = (pct if pct is not None else -10.0), now
        except Exception as e:
            job.say(f"progress monitor hiccup (build unaffected): {e}", "warn")
            try:
                src and src.disconnect()
            except Exception:
                pass
            src = None
    if src:
        src.disconnect()


LOCK_WAIT = "5s"
LOCK_TRIES = 60


def _short_lock(job: Job, cur, stmt: str):
    """ALTER TABLE needs a brief exclusive lock. Waiting for it forever would
    queue every other query on the table behind us, so wait 5s at a time,
    step back, and try again."""
    for attempt in range(1, LOCK_TRIES + 1):
        if job.cancelled:
            raise RuntimeError("cancelled")
        cur.execute(f"SET lock_timeout = '{LOCK_WAIT}'")
        try:
            cur.execute(stmt)
            return
        except Exception as e:
            if getattr(e, "pgcode", None) != "55P03":  # lock_not_available
                raise
            job.say(f"table is busy with other queries — waiting for the lock (try {attempt}/{LOCK_TRIES})", "warn")
            time.sleep(2)
        finally:
            cur.execute("SET lock_timeout = 0")
    raise RuntimeError("gave up waiting for the table lock; try again when it's quieter")


def _index_state(cur, schema: str, table: str, name: str, col: Optional[str] = None):
    """None if no index has that name, else (valid, on_this_table, plain_unique_on_col)."""
    cur.execute(INDEX_STATE_SQL, {"ref": f"{q(schema)}.{q(table)}", "col": col, "schema": schema, "name": name})
    return cur.fetchone()


def _build_key(job: Job, conn, schema: str, table: str, col: str):
    ref, c = f"{q(schema)}.{q(table)}", q(col)
    pk, nn = key_names(table)
    shown = iter(key_statements(schema, table, col))

    def step(stmt, locked=False, show=None):
        if job.cancelled:
            raise RuntimeError("cancelled")
        planned = next(shown)
        job.say(show or planned, "sql")
        t0 = job.step_t0 = time.monotonic()
        with conn.cursor() as cur:
            _short_lock(job, cur, stmt) if locked else cur.execute(stmt)
        job.say(f"done in {_took(time.monotonic() - t0)}", "ok")

    with conn.cursor() as cur:
        # cheap name checks before the expensive scan
        state = _index_state(cur, schema, table, pk, col)
        if state and not state[1]:
            raise RuntimeError(f"an index named {pk} already exists on another table in {schema}. Nothing was changed.")
        if state and state[0] and not state[2]:
            raise RuntimeError(f"{pk} already exists on this table but isn't a plain unique index on {col}. "
                               f"Drop or rename it first. Nothing was changed.")

        reuse = None
        if not (state and state[0]):
            cur.execute(REUSABLE_UNIQUE_SQL, {"ref": ref, "col": col})
            reuse = (cur.fetchone() or [None])[0]

        job.say(next(shown), "sql")
        job.say("scanning the whole table — this takes a while on a big one")
        t0 = job.step_t0 = time.monotonic()
        if reuse:  # the unique index already rules out duplicates; only NULLs need counting
            cur.execute(f"SELECT count(*) FILTER (WHERE {c} IS NULL), 0 FROM {ref}")
        else:
            cur.execute(f"SELECT count(*) FILTER (WHERE {c} IS NULL), count({c}) - count(DISTINCT {c}) FROM {ref}")
        nulls, dupes = cur.fetchone()
        if nulls or dupes:
            raise RuntimeError(f"{col} has {nulls:,} empty and {dupes:,} duplicate values — it can't be the key. "
                               f"Nothing was changed.")
        job.say(f"{col} is unique and never empty ({_took(time.monotonic() - t0)})", "ok")

        if state and not state[0]:
            job.say(f"{pk} is an invalid leftover from an earlier run — dropping it first", "warn")
            cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {q(schema)}.{q(pk)}")
            state = None
        cur.execute("SELECT a.attnotnull FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attname = %s",
                    (ref, col))
        notnull = cur.fetchone()[0]
        cur.execute("SELECT 1 FROM pg_constraint WHERE conrelid = %s::regclass AND conname = %s", (ref, nn))
        check_left = cur.fetchone() is not None

    if state:
        next(shown)
        job.say(f"{pk} already exists — reusing it")
    elif reuse:
        next(shown)
        job.say(f"{reuse} is already a unique index on {col} — using it as the key instead of building another")
    else:
        step(f"CREATE UNIQUE INDEX CONCURRENTLY {q(pk)} ON {ref} ({c})")

    if notnull:
        for _ in range(4):
            next(shown)
        job.say(f"{col} is already NOT NULL — skipping that part")
    else:
        # The CHECK route proves "no NULLs" without holding a lock during the
        # scan; SET NOT NULL then trusts it instead of scanning again (PG 12+).
        if check_left:
            # may be from a run that picked another column; it's ours by name, so start clean
            job.say(f"{nn} is left over from an earlier run — dropping it first", "warn")
            with conn.cursor() as cur:
                _short_lock(job, cur, f"ALTER TABLE {ref} DROP CONSTRAINT IF EXISTS {q(nn)}")
        step(f"ALTER TABLE {ref} ADD CONSTRAINT {q(nn)} CHECK ({c} IS NOT NULL) NOT VALID", locked=True)
        step(f"ALTER TABLE {ref} VALIDATE CONSTRAINT {q(nn)}")
        step(f"ALTER TABLE {ref} ALTER COLUMN {c} SET NOT NULL", locked=True)
        step(f"ALTER TABLE {ref} DROP CONSTRAINT {q(nn)}", locked=True)
    using = reuse if (reuse and not state) else pk
    final = f"ALTER TABLE {ref} ADD CONSTRAINT {q(pk)} PRIMARY KEY USING INDEX {q(using)}"
    step(final, locked=True, show=final + ";")
    job.say(f"primary key {pk} ({col}) added", "ok")


def _build_item(job: Job, conn, schema: str, table: str, item: dict):
    with conn.cursor() as cur:
        if item["index"]:
            state = _index_state(cur, schema, table, item["index"])
            if state and not state[1]:
                raise RuntimeError(f"an index named {item['index']} already exists on another table in {schema}; "
                                   f"rename that one or build this by hand")
            if state and state[0]:
                job.say(f"{item['index']} already exists and is valid — skipping")
                return
            if state:
                job.say(f"{item['index']} is an invalid leftover from an earlier run — dropping it first", "warn")
                cur.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {q(schema)}.{q(item['index'])}")
        for stmt in item["statements"]:
            if job.cancelled:  # a cancel that landed between statements finds nothing to interrupt
                raise RuntimeError("cancelled")
            job.say(stmt, "sql")
            t0 = job.step_t0 = time.monotonic()
            cur.execute(stmt)
            job.say(f"done in {_took(time.monotonic() - t0)}", "ok")


def _run_job(job: Job):
    env, database, schema, table = job.target
    stop = threading.Event()
    src = None
    try:
        job.status = "running"
        job.say(f"connecting to {env.upper()} {database or ''} as build session")
        src = _open(env, database)
        with src.conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")
            cur.execute("SET lock_timeout = 0")
            cur.execute("SET maintenance_work_mem = %s", (BUILD_MEM,))
            cur.execute("SET max_parallel_maintenance_workers = %s", (BUILD_WORKERS,))
            cur.execute("SELECT pg_backend_pid()")
            job.pid = cur.fetchone()[0]
        job.say(f"session ready (pid {job.pid}, maintenance_work_mem={BUILD_MEM}, workers={BUILD_WORKERS})")
        threading.Thread(target=_watch, args=(job, env, database, stop), daemon=True).start()

        failed, ext_failed = 0, False
        for n, item in enumerate(job.items, 1):
            if job.cancelled:
                job.say("cancelled — skipping the rest", "warn")
                break
            if src.conn.closed:
                raise RuntimeError("lost the database connection; run the check again to see what got built")
            job.current = item["id"]
            job.say(f"[{n}/{len(job.items)}] {item['feature']} {item['column'] or ''}".rstrip())
            if item["feature"] == "key":
                try:
                    _build_key(job, src.conn, schema, table, item["key_column"])
                except Exception as e:
                    failed += 1
                    job.say(f"key not added: {str(e).strip()}", "error")
                continue
            if ext_failed and "_trgm_ops" in item["sql"]:
                failed += 1
                job.say("skipped — pg_trgm couldn't be installed, so this index can't be built", "error")
                continue
            try:
                _build_item(job, src.conn, schema, table, item)
            except Exception as e:
                failed += 1
                job.say(f"failed: {str(e).strip()}", "error")
                if "index row requires" in str(e) or "index row size" in str(e):
                    job.say(f"some values in {item['column']} are too long for a btree (max ~2.7 KB). "
                            f"Lookups on it need a hash index: USING hash ({item['column']}) — or untick "
                            f"Lookup for this column if it isn't really looked up by value", "warn")
                if item["index"]:
                    job.say("an invalid index may be left behind; run the check again to drop it", "warn")
                if item["feature"] == "extension":
                    ext_failed = True

        if not job.cancelled:
            job.say(f"ANALYZE {schema}.{table} so the planner sees the new indexes")
            job.step_t0 = time.monotonic()
            with src.conn.cursor() as cur:
                cur.execute(f"ANALYZE {q(schema)}.{q(table)}")
            job.say("analyze done", "ok")
        job.status = "cancelled" if job.cancelled else "failed" if failed else "done"
        job.say(f"finished — {job.status} in {_clock(time.monotonic() - job.t0)}", "error" if failed else "ok")
    except Exception as e:
        job.status = "cancelled" if job.cancelled else "failed"
        job.say(f"stopped: {str(e).strip()}", "error")
        if job.cancelled:
            job.say("a cancelled CONCURRENTLY build leaves an invalid index; run the check again to drop it", "warn")
    finally:
        stop.set()
        job.current = None
        job.finished = datetime.now().isoformat(timespec="seconds")
        if src:
            src.disconnect()
        with _jobs_lock:
            _active.pop(job.target, None)


def _job_view(job: Job, since: int = 0) -> dict:
    return {
        "id": job.id, "status": job.status, "target": dict(zip(("env", "database", "schema", "table"), job.target)),
        "items": [i["id"] for i in job.items], "current": job.current, "user": job.user,
        "started": job.started, "finished": job.finished,
        "log": job.log[since:], "next": len(job.log),
    }


@index_health_router.post("/build")
def start_build(body: BuildRequest, auth: dict = Depends(require_permission(PERMISSION, BUILD_PERMISSION))):
    database, schema, table = _clean_target(body.database, body.schema_name, body.table)
    env = _env(body.env)
    if not body.items:
        raise HTTPException(400, "Pick at least one fix to build.")

    # Re-run the check server-side and only build what it says is missing.
    result = run_check(env, database, schema, table, body.search, body.filter, body.vector)
    ranked = result["missing"] + result["cleanup"]
    by_id = {m["id"]: m for m in ranked}
    body.items = list(dict.fromkeys(body.items))
    unknown = [i for i in body.items if i not in by_id]
    if unknown:
        raise HTTPException(409, f"No longer missing (or never was): {', '.join(unknown)}. Run the check again.")
    items = [by_id[i] for i in body.items]
    blocked = [m["id"] for m in items if not m["buildable"]]
    if blocked:
        raise HTTPException(400, f"Can't be built from here: {', '.join(blocked)}.")
    for m in items:
        if m["feature"] == "key":
            if body.key_column not in m.get("candidates", []):
                raise HTTPException(400, "Pick which column should be the primary key.")
            m["key_column"] = m["column"] = body.key_column
    # extension first, then everything else in the order the check ranked them
    order = [x["id"] for x in ranked]
    items.sort(key=lambda m: order.index(m["id"]))

    target = (env, database, schema, table)
    user = ((auth or {}).get("user") or {}).get("email") or "unknown"
    with _jobs_lock:
        if target in _active:
            raise HTTPException(409, f"A build is already running on this table (job {_active[target]}).")
        if len(_active) >= MAX_RUNNING:
            raise HTTPException(429, f"Too many builds running ({len(_active)} of {MAX_RUNNING}); try again when one finishes.")
        job = Job(target, items, user)
        _jobs[job.id] = job
        _active[target] = job.id
        while len(_jobs) > MAX_JOBS:
            oldest = next(iter(_jobs))
            if _jobs[oldest].finished is None:
                break
            _jobs.pop(oldest)
    job.say(f"queued by {user}: {len(items)} fix(es) on {schema}.{table}")
    threading.Thread(target=_run_job, args=(job,), daemon=True, name=f"index-build-{job.id}").start()
    return _job_view(job)


@index_health_router.get("/jobs/{job_id}")
def get_build(job_id: str, since: int = Query(0, ge=0),
              auth: dict = Depends(require_permission(PERMISSION))):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found (the server may have restarted).")
    return _job_view(job, since)


@index_health_router.post("/jobs/{job_id}/cancel")
def cancel_build(job_id: str, auth: dict = Depends(require_permission(PERMISSION, BUILD_PERMISSION))):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    if job.finished:
        return _job_view(job)
    job.cancelled = True
    job.say("cancel requested", "warn")
    if job.pid:
        env, database, _, _ = job.target
        try:
            src = _open(env, database)
            try:
                with src.conn.cursor() as cur:
                    cur.execute("SELECT pg_cancel_backend(%s)", (job.pid,))
            finally:
                src.disconnect()
        except Exception as e:
            job.say(f"could not cancel the running statement: {e}", "error")
    return _job_view(job)