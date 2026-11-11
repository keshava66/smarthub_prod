"""Integration tests for semantic / hybrid retrieval against real PostgreSQL + pgvector.

Needs a disposable database with the pgvector extension available:
    DATA_API_TEST_PG_DSN="host=localhost port=5432 user=postgres password=pg dbname=regtest" \\
        python -m pytest tests/test_data_api_retrieval_pg.py -q

Skipped when DATA_API_TEST_PG_DSN is not set. The embedding provider is
replaced by a deterministic fake; everything else (routing, auth, rate limit,
SQL, activity log) is the production code path.
"""

import hashlib
import json
import math
import os
import threading
import time
import uuid

import pytest

DSN = os.environ.get("DATA_API_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="DATA_API_TEST_PG_DSN not set")

# The portal's own tables go in a throwaway schema in the same database, so
# the test never touches a real smarthub_core.
META_SCHEMA = "smarthub_test_" + uuid.uuid4().hex[:8]
if DSN:
    os.environ["SMARTHUB_DB_DSN"] = DSN
    os.environ["SMARTHUB_DB_SCHEMA"] = META_SCHEMA

psycopg2 = pytest.importorskip("psycopg2")

SCHEMA = "retrieval_test"
KEY = "test-key-" + uuid.uuid4().hex
FIXTURE_SQL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE {SCHEMA}.rim_docs (
  chunk_source_id text PRIMARY KEY, object_name text, a_status text, approved_date date, indication text);
INSERT INTO {SCHEMA}.rim_docs
SELECT 'src-'||d, 'Doc '||d, 'Approved', DATE '2025-01-01' + d, CASE WHEN d%3=0 THEN NULL ELSE 'Oncology' END
FROM generate_series(1,40) d;
CREATE TABLE {SCHEMA}.rim_chunks (
  chunk_id uuid PRIMARY KEY, chunk_source_id text, chunk_number int, chunk_text text,
  region text, amount numeric(10,2), embedding vector(8), secret text);
INSERT INTO {SCHEMA}.rim_chunks
SELECT md5('c'||i)::uuid, 'src-'||(1+(i%40)), i%7,
  CASE i%6 WHEN 0 THEN 'Sterility failure observed in aseptic filling line'
           WHEN 1 THEN 'Complete response letter regarding manufacturing facility inspection'
           WHEN 2 THEN 'Stability data for the drug product'
           WHEN 3 THEN 'Clinical protocol amendment for oncology indication'
           WHEN 4 THEN 'Sterility assurance and container closure integrity'
           ELSE 'Labeling update for dose guidance' END || ' chunk ' || i,
  CASE WHEN i%2=0 THEN 'US' ELSE 'EU' END, (i*1.25)::numeric(10,2),
  ARRAY[sin(i),cos(i),sin(2*i),cos(2*i),sin(3*i),cos(3*i),sin(i/2.0),cos(i/2.0)]::vector(8), 'hidden'
FROM generate_series(1,600) i;
CREATE INDEX ON {SCHEMA}.rim_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ON {SCHEMA}.rim_chunks USING gin (to_tsvector('english', chunk_text));
-- same shape, no full-text index
CREATE TABLE {SCHEMA}.no_fts AS SELECT * FROM {SCHEMA}.rim_chunks;
ALTER TABLE {SCHEMA}.no_fts ADD PRIMARY KEY (chunk_id);
CREATE INDEX ON {SCHEMA}.no_fts USING hnsw (embedding vector_cosine_ops);
-- large table so a tiny keyword timeout reliably trips
CREATE TABLE {SCHEMA}.big (id int PRIMARY KEY, body text, embedding vector(8));
INSERT INTO {SCHEMA}.big SELECT i, 'common regulatory words appear here ' || i,
  ARRAY[sin(i),cos(i),sin(2*i),cos(2*i),sin(3*i),cos(3*i),sin(i/2.0),cos(i/2.0)]::vector(8)
FROM generate_series(1,150000) i;
CREATE INDEX ON {SCHEMA}.big USING gin (to_tsvector('english', body));
-- metadata table whose join key is NOT unique (lookup must be rejected)
CREATE TABLE {SCHEMA}.rim_docs_nokey AS SELECT * FROM {SCHEMA}.rim_docs;
-- keyword index but no ANN index
CREATE TABLE {SCHEMA}.no_ann AS SELECT * FROM {SCHEMA}.rim_chunks;
ALTER TABLE {SCHEMA}.no_ann ADD PRIMARY KEY (chunk_id);
CREATE INDEX ON {SCHEMA}.no_ann USING gin (to_tsvector('english', chunk_text));
-- fully indexed copy whose GIN index a test drops after saving (drift)
CREATE TABLE {SCHEMA}.drift AS SELECT * FROM {SCHEMA}.rim_chunks;
ALTER TABLE {SCHEMA}.drift ADD PRIMARY KEY (chunk_id);
CREATE INDEX ON {SCHEMA}.drift USING hnsw (embedding vector_cosine_ops);
CREATE INDEX drift_gin ON {SCHEMA}.drift USING gin (to_tsvector('english', chunk_text));
ANALYZE;
"""


def stored_vector(i):
    return [math.sin(i), math.cos(i), math.sin(2 * i), math.cos(2 * i), math.sin(3 * i), math.cos(3 * i),
            math.sin(i / 2.0), math.cos(i / 2.0)]


def fake_embed(model, inputs):
    """'... chunk N' embeds to chunk N's stored vector, so the right answer is known."""
    out = []
    for text in inputs:
        words = text.split()
        if "chunk" in words and words.index("chunk") + 1 < len(words):
            out.append(stored_vector(int(words[words.index("chunk") + 1])))
        else:
            h = hashlib.sha256(text.encode()).digest()
            out.append([b / 255 - 0.5 for b in h[:model.dimensions]])
    return out


CONTRACT = {
    "lookups": {"doc": {"schema": SCHEMA, "table": "rim_docs", "key": "chunk_source_id", "on": "chunk_source_id"}},
    "fields": {"chunk_id": "chunk_id", "chunk_source_id": "chunk_source_id",
               "chunk_number": "chunk_number", "chunk_text": "chunk_text"},
    "objects": {"rimdocs_metadata": {"object_name": "doc.object_name", "a_status": "doc.a_status",
                                     "approved_date": "doc.approved_date"},
                "document_metadata": {"indication": "doc.indication"}},
}
ALIASES = [
    ("chunks", "rim_chunks", "chunk_id,chunk_source_id,chunk_number,chunk_text,region,amount,embedding"),
    ("chunks_b", "rim_chunks", ""),          # a second API alias over the SAME table
    ("narrow", "rim_chunks", "chunk_id,chunk_text,embedding"),
    ("nofts", "no_fts", ""), ("noann", "no_ann", ""), ("big", "big", ""), ("drift", "drift", ""),
]


@pytest.fixture(scope="module")
def env():
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(FIXTURE_SQL)
    from psycopg2.extensions import parse_dsn
    params = parse_dsn(DSN)  # handles quoted values and paths with spaces
    conn.close()

    import auth
    import db as core_db
    import data_api
    import data_api_retrieval
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    assert core_db.schema() == META_SCHEMA, "test must own the metadata schema"
    data_api._source_cfg = lambda system, env_name: {"host": params["host"], "port": int(params.get("port", 5432)),
                                                    "user": params["user"], "password": params.get("password", "")}
    data_api_retrieval._azure_embed = fake_embed
    data_api_retrieval._cache._data.clear()

    api_id = str(uuid.uuid4())
    ids = {}
    with core_db.transaction() as db:
        db.execute("INSERT INTO data_apis (id, slug, name, api_key, created_at, is_active) VALUES (?,?,?,?,?,true)",
                   (api_id, "retrieval-test", "Retrieval test", KEY, "2026-01-01T00:00:00Z"))
        for alias, table, cols in ALIASES:
            ids[alias] = str(uuid.uuid4())
            db.execute("INSERT INTO data_api_tables (id, api_id, source_system, env, database_name, schema_name, "
                       "table_name, alias, row_limit, columns_csv) VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (ids[alias], api_id, "postgresql", "DEV", params["dbname"], SCHEMA, table, alias, 1000, cols))

    app = FastAPI()
    app.include_router(data_api.data_api_router)
    # Portal admin identity for the management endpoints (consumer endpoints use the API key).
    app.dependency_overrides[auth.require_auth] = lambda: {
        "user": {"email": "admin@test", "name": "Admin"}, "role": "admin",
        "permissions": {"manageDataAPIExports": True}}
    client = TestClient(app)
    models = {}
    for name, dims in (("fake8", 8), ("fake8b", 8), ("fake3", 3)):
        r = client.post("/data-api/search/models", json={"name": name, "deployment": "fake", "dimensions": dims})
        assert r.status_code == 200, r.text
        models[name] = r.json()["id"]
    r = client.post("/data-api/search/contracts", json={"name": "rim_chunks", "definition": CONTRACT})
    assert r.status_code == 200 and r.json()["version"] == 1, r.text

    def save(alias, expect=200, register="fake8", replace=False, **settings):
        body = {"settings": dict({"vector_column": "embedding", "text_column": "chunk_text"}, **settings),
                "replace_registration": replace}
        if register:
            body["registration"] = {body["settings"].get("vector_column") or "embedding": models[register]}
        r = client.put(f"/data-api/retrieval-test/tables/{ids[alias]}/search", json=body)
        assert r.status_code == expect, r.text
        return r.json()

    def disable(alias):
        client.delete(f"/data-api/retrieval-test/tables/{ids[alias]}/search")

    yield {"client": client, "api": data_api, "retrieval": data_api_retrieval, "ids": ids, "models": models,
           "contract_v1": r.json()["id"], "save": save, "disable": disable, "dbname": params["dbname"]}

    data_api.STORE.close()
    core_db.close_pool()
    pg(f"DROP SCHEMA IF EXISTS {META_SCHEMA} CASCADE")


def post(env, alias, endpoint, body, **kw):
    headers = dict(kw.pop("headers", {}) or {})
    headers.setdefault("X-API-Key", KEY)
    return env["client"].post(f"/data-api/retrieval-test/{alias}/{endpoint}", json=body, headers=headers, **kw)


def pg(sql):
    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.close()

# ---------------------------------------------------------------- /retrieve

def test_hybrid_contract_response(env):
    env["save"]("chunks", contract_id=env["contract_v1"])
    r = post(env, "chunks", "retrieve", {"retrieval_queries": [{"query": "aseptic filling chunk 54", "k": 5}]})
    assert r.status_code == 200, r.text
    body = r.json()
    [result] = body["results"]
    assert set(result) == {"k", "matches"} and result["k"] == 5
    top = result["matches"][0]
    assert set(top) == {"rank", "score", "dense_score", "sparse_score", "chunk_id", "chunk_source_id",
                        "chunk_number", "chunk_text", "rimdocs_metadata", "document_metadata"}
    assert top["chunk_id"] == str(uuid.UUID(hashlib.md5(b"c54").hexdigest()))
    assert top["rank"] == 1 and top["dense_score"] == pytest.approx(1.0, abs=1e-6) and top["sparse_score"] > 0
    assert top["rimdocs_metadata"] == {"object_name": "Doc 15", "a_status": "Approved", "approved_date": "2025-01-16"}
    assert top["document_metadata"] == {"indication": None}
    scores = [m["score"] for m in result["matches"]]
    assert scores == sorted(scores, reverse=True)
    assert all(m["sparse_score"] is None for m in result["matches"][1:])
    assert body["embedding_model"] == "fake8" and body["response_contract"] == "rim_chunks v1"


def test_batch_filters_modes_and_default_shape(env):
    env["save"]("chunks")
    r = post(env, "chunks", "retrieve", {"retrieval_queries": [
        {"query": "sterility", "k": 4, "where": ["region:eq:EU"]},
        {"query": "sterility", "k": 3, "mode": "sparse"},
        {"query": "stability chunk 8", "k": 2, "mode": "dense"},
    ]})
    assert r.status_code == 200, r.text
    first, sparse, dense = r.json()["results"]
    assert [x["k"] for x in (first, sparse, dense)] == [4, 3, 2]
    assert all(m["row"]["region"] == "EU" for m in first["matches"])
    assert "embedding" not in first["matches"][0]["row"]
    assert isinstance(first["matches"][0]["row"]["amount"], str)
    assert all(m["dense_score"] is None and m["sparse_score"] > 0 for m in sparse["matches"])
    assert dense["matches"][0]["row"]["chunk_number"] == 8 % 7 and dense["matches"][0]["sparse_score"] is None


def test_modes_limited_per_alias(env):
    env["save"]("chunks", modes=["dense"])
    r = post(env, "chunks", "retrieve", {"retrieval_queries": [{"query": "x", "mode": "sparse"}]})
    assert r.status_code == 400 and "available: dense" in r.text
    env["save"]("chunks")


def test_exact_search_is_off_by_default_then_matches_brute_force(env):
    env["save"]("chunks")
    body = {"search_mode": "exact", "retrieval_queries": [{"query": "x chunk 99", "k": 5, "mode": "dense"}]}
    r = post(env, "chunks", "retrieve", body)
    assert r.status_code == 409 and "Exact search is disabled" in r.text
    env["save"]("chunks", allow_exact=True)
    r = post(env, "chunks", "retrieve", body)
    assert r.status_code == 200, r.text
    got = [m["row"]["chunk_id"] for m in r.json()["results"][0]["matches"]]
    q = stored_vector(99)

    def cos(v):
        dot = sum(a * b for a, b in zip(q, v))
        return 1 - dot / (math.sqrt(sum(a * a for a in q)) * math.sqrt(sum(b * b for b in v)))
    expected = sorted(range(1, 601), key=lambda i: cos(stored_vector(i)))[:5]
    assert got == [str(uuid.UUID(hashlib.md5(f"c{i}".encode()).hexdigest())) for i in expected]
    env["save"]("chunks")


def test_keyword_timeout_is_per_alias_and_degrades_to_semantic(env):
    env["save"]("big", text_column="body", keyword_timeout_ms=100, allow_exact=True)
    r = post(env, "big", "retrieve", {"search_mode": "exact",
                                      "retrieval_queries": [{"query": "common regulatory words", "k": 3}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert any("keyword search timed out" in w for w in body["warnings"])
    assert all(m["sparse_score"] is None and m["dense_score"] is not None for m in body["results"][0]["matches"])


# ------------------------------------------------ save-time validation (422)

@pytest.mark.parametrize("alias, settings, register, fragment", [
    ("nofts", {}, "fake8", "no GIN full-text index"),
    ("narrow", {"contract": True}, "fake8", "does not expose"),
    ("chunks", {"replace": True}, "fake3", "produces 3 dimensions"),
    ("chunks", {"text_column": "secret"}, "fake8", "not exposed"),
    ("chunks", {"text_column": "chunk_number"}, "fake8", "choose a text or tsvector column"),
    ("noann", {}, "fake8", "No valid ANN"),
    ("chunks", {"vector_column": "missing"}, "fake8", "not a pgvector column"),
])
def test_invalid_configuration_is_rejected_at_save(env, alias, settings, register, fragment):
    if settings.pop("contract", False):
        settings["contract_id"] = env["contract_v1"]
    replace = settings.pop("replace", False)
    body = env["save"](alias, expect=422, register=register, replace=replace, **settings)
    assert fragment in json.dumps(body)
    # nothing was stored: the alias still has no working search
    r = post(env, alias, "retrieve", {"retrieval_queries": [{"query": "x"}]})
    assert r.status_code == 409 or alias == "chunks"


def test_non_unique_lookup_key_is_rejected(env):
    bad = json.loads(json.dumps(CONTRACT))
    bad["lookups"]["doc"]["table"] = "rim_docs_nokey"
    r = env["client"].post("/data-api/search/contracts", json={"name": "bad_lookup", "definition": bad})
    assert r.status_code == 200
    body = env["save"]("chunks", expect=422, contract_id=r.json()["id"])
    assert "primary key or single-column unique index" in json.dumps(body)


def test_no_ann_index_is_allowed_only_with_exact_and_warns(env):
    body = env["save"]("noann", allow_exact=True)
    assert body["saved"] and any("reads every row" in w for w in body["warnings"])
    env["disable"]("noann")


def test_drift_after_save_fails_safely(env):
    env["save"]("drift")
    pg(f"DROP INDEX {SCHEMA}.drift_gin")
    try:
        r = post(env, "drift", "retrieve", {"retrieval_queries": [{"query": "sterility"}]})
        assert r.status_code == 409 and "Keyword search is unavailable" in r.text
        r = post(env, "drift", "retrieve", {"retrieval_queries": [{"query": "sterility", "mode": "dense"}]})
        assert r.status_code == 200   # semantic still works
    finally:
        pg(f"CREATE INDEX drift_gin ON {SCHEMA}.drift USING gin (to_tsvector('english', chunk_text))")
        env["api"]._meta_evict([env["ids"]["drift"]])


def test_validate_reports_planner_and_embedding(env):
    body = {"settings": {"vector_column": "embedding", "text_column": "chunk_text"},
            "registration": {"embedding": env["models"]["fake8"]}, "planner": True, "test_embedding": True}
    r = env["client"].post(f"/data-api/retrieval-test/tables/{env['ids']['chunks']}/search/validate", json=body)
    assert r.status_code == 200, r.text
    report = r.json()
    assert report["errors"] == []
    assert report["info"]["row_estimate"] == 600
    assert report["info"]["semantic_plan"] and report["info"]["keyword_plan"]
    assert report["info"]["embedding_test"]["dimensions"] == 8
    assert report["info"]["keyword"] == {"column": "chunk_text", "config": "english", "stored_tsvector": False}


# ---------------------------------------- shared registrations across APIs

def test_second_api_alias_reuses_the_shared_registration(env):
    env["save"]("chunks")
    env["save"]("chunks_b", register=None, modes=["dense"])   # no model chosen: reuses the registration
    r = post(env, "chunks_b", "retrieve", {"retrieval_queries": [{"query": "x chunk 3", "k": 2}]})
    assert r.status_code == 200 and r.json()["embedding_model"] == "fake8"
    lib = env["client"].get("/data-api/search/library").json()
    [reg] = [x for x in lib["registrations"] if x["table"] == "rim_chunks"]
    assert sorted(reg["used_by"]) == ["Retrieval test /chunks", "Retrieval test /chunks_b"]
    # re-pointing a shared column needs explicit confirmation
    body = env["save"]("chunks", expect=422, register="fake8b")
    assert "chunks_b" in json.dumps(body) and "confirm" in json.dumps(body)
    env["save"]("chunks", register="fake8b", replace=True)
    r = post(env, "chunks_b", "retrieve", {"retrieval_queries": [{"query": "x chunk 3", "k": 2}]})
    assert r.json()["embedding_model"] == "fake8b"          # every alias follows the column's model
    env["save"]("chunks", register="fake8", replace=True)
    env["disable"]("chunks_b")


def test_model_and_registration_safety_rules(env):
    c, m = env["client"], env["models"]
    env["save"]("chunks")
    base = {"name": "fake8", "deployment": "fake", "dimensions": 8}
    assert c.put(f"/data-api/search/models/{m['fake8']}", json=dict(base, api_version="2024-08-01-preview")).status_code == 200
    r = c.put(f"/data-api/search/models/{m['fake8']}", json=dict(base, dimensions=16))
    assert r.status_code == 409 and "mismatch vectors already stored" in r.text
    assert c.delete(f"/data-api/search/models/{m['fake8']}").status_code == 409
    lib = c.get("/data-api/search/library").json()
    reg = next(x for x in lib["registrations"] if x["table"] == "rim_chunks")
    assert c.delete(f"/data-api/search/registrations/{reg['id']}").status_code == 409
    r = c.post("/data-api/search/models", json={"name": "evil", "deployment": "d", "dimensions": 8,
                                                 "endpoint": "https://evil.example.com"})
    assert r.status_code == 400 and "not allowed" in r.text
    r = c.post("/data-api/search/models", json={"name": "evil", "deployment": "d", "dimensions": 8,
                                                 "api_key_env": "SNOWFLAKE_PASSWORD"})
    assert r.status_code == 400
    assert "key_configured" in lib["models"][0] and "SECRET" not in json.dumps(lib)


def test_contracts_are_versioned_and_protected(env):
    c = env["client"]
    env["save"]("chunks", contract_id=env["contract_v1"])
    changed = json.loads(json.dumps(CONTRACT))
    changed["fields"]["text"] = changed["fields"].pop("chunk_text")
    r = c.post("/data-api/search/contracts", json={"name": "rim_chunks", "definition": changed})
    assert r.status_code == 200 and r.json()["version"] == 2
    v2 = r.json()["id"]
    top = post(env, "chunks", "retrieve", {"retrieval_queries": [{"query": "sterility", "k": 1}]}).json()
    assert "chunk_text" in top["results"][0]["matches"][0]      # pinned v1 did not change shape
    assert c.delete(f"/data-api/search/contracts/{env['contract_v1']}").status_code == 409
    assert c.delete(f"/data-api/search/contracts/{v2}").status_code == 200


def test_settings_survive_ordinary_table_list_edits(env):
    env["save"]("chunks")
    detail = env["client"].get("/data-api/retrieval-test").json()
    tables = [{"source_system": t["source_system"], "env": t["env"], "database": t["database"], "db_schema": t["schema"],
               "table": t["table"], "alias": t["alias"], "row_limit": t["row_limit"], "columns": t["columns"]}
              for t in detail["tables"]]
    r = env["client"].patch("/data-api/retrieval-test", json={"description": "edited", "tables": tables})
    assert r.status_code == 200, r.text
    r = post(env, "chunks", "retrieve", {"retrieval_queries": [{"query": "sterility", "k": 1}]})
    assert r.status_code == 200


def test_not_installed_degrades_gracefully(env, monkeypatch):
    monkeypatch.setattr(env["api"], "_search_ready", lambda: False)
    lib = env["client"].get("/data-api/search/library").json()
    assert lib["installed"] is False and "db.py migrate" in lib["message"]
    r = post(env, "chunks", "retrieve", {"retrieval_queries": [{"query": "x"}]})
    assert r.status_code == 409 and "not installed" in r.text
    r = env["client"].get("/data-api/retrieval-test/chunks/columns", headers={"X-API-Key": KEY})
    assert r.status_code == 200 and r.json()["retrieval"] is None
    q = [0.1] * 8
    r = post(env, "chunks", "vector-search", {"vector_column": "embedding", "query_vector": q})
    assert r.status_code == 200                               # existing feature unaffected


# ------------------------------------------------------ consumer behaviour

def test_embedding_outage_is_503_with_retry_after(env, monkeypatch):
    env["save"]("chunks")

    def down(model, inputs):
        raise env["retrieval"].EmbeddingUnavailable("The embedding service is unavailable.", retryable=True)
    monkeypatch.setattr(env["retrieval"], "_azure_embed", down)
    r = post(env, "chunks", "retrieve", {"retrieval_queries": [{"query": "never cached " + uuid.uuid4().hex}]})
    assert r.status_code == 503 and r.headers["retry-after"] == "2"


def test_retrieve_requires_json_and_key(env):
    env["save"]("chunks")
    r = env["client"].post("/data-api/retrieval-test/chunks/retrieve", content=b"x",
                           headers={"Content-Type": "text/plain", "X-API-Key": KEY})
    assert r.status_code == 415
    r = post(env, "chunks", "retrieve", {"retrieval_queries": [{"query": "x"}]}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_activity_log_records_retrieve_without_query_text(env):
    env["save"]("chunks")
    marker = "unique marker " + uuid.uuid4().hex
    assert post(env, "chunks", "retrieve", {"retrieval_queries": [{"query": marker, "k": 2}]}).status_code == 200
    import db as core_db
    with core_db.transaction(read_only=True) as db:
        rows = db.execute("SELECT * FROM data_api_requests WHERE endpoint = 'retrieve' AND status = 200").fetchall()
        dump = json.dumps(db.execute("SELECT * FROM data_api_requests").fetchall())
    assert rows and marker not in dump


def test_vector_search_text_query_needs_search_enabled(env):
    env["disable"]("chunks")
    r = post(env, "chunks", "vector-search", {"vector_column": "embedding", "query_text": "x"})
    assert r.status_code == 409 and "not enabled" in r.text
    env["save"]("chunks")
    r = post(env, "chunks", "vector-search", {"vector_column": "embedding", "query_text": "anything chunk 54",
                                              "top_k": 3, "search_mode": "exact"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["query_source"] == "text" and body["embedding_model"] == "fake8"
    assert body["matches"][0]["row"]["chunk_id"] == str(uuid.UUID(hashlib.md5(b"c54").hexdigest()))
    r = post(env, "chunks", "vector-search", {"vector_column": "embedding", "query_text": "x", "query_vector": [1.0] * 8})
    assert r.status_code == 400 and "not both" in r.text


def test_vector_search_with_raw_vector_is_unchanged(env):
    env["disable"]("chunks")   # the original feature must not depend on search settings
    q = [0.3, -0.2, 0.5, 0.1, -0.4, 0.2, 0.7, -0.1]
    r = post(env, "chunks", "vector-search", {"vector_column": "embedding", "query_vector": q, "top_k": 5,
                                              "search_mode": "exact", "columns": ["chunk_id", "amount"]})
    assert r.status_code == 200, r.text
    body = r.json()
    original_keys = {"alias", "vector_column", "dimensions", "metric", "search_mode", "requested_top_k", "top_k",
                     "row_count", "columns", "matches", "elapsed_ms", "warnings", "recall_guaranteed",
                     "eligible_ann_index_count", "returned_top_k", "top_k_filled", "decimal_encoding"}
    assert original_keys <= set(body) and set(body) - original_keys == {"query_source", "embedding_model"}
    assert set(body["matches"][0]) == {"row", "distance", "similarity", "rank"}


def test_exact_gate_is_keyed_per_api_key(env, monkeypatch):
    seen = []

    class Recorder:
        def enter(self, caller): seen.append(("enter", caller))
        def leave(self, caller): seen.append(("leave", caller))
    monkeypatch.setattr(env["api"], "EXACT_GATE", Recorder())
    env["save"]("chunks", allow_exact=True)
    post(env, "chunks", "vector-search", {"vector_column": "embedding", "query_vector": [0.1] * 8, "search_mode": "exact"})
    post(env, "chunks", "retrieve", {"search_mode": "exact", "retrieval_queries": [{"query": "x", "mode": "dense"}]})
    expected = hashlib.sha256(KEY.encode()).hexdigest()
    assert seen == [("enter", expected), ("leave", expected)] * 2
    env["save"]("chunks")


def test_columns_discovery_is_additive(env):
    env["save"]("chunks", contract_id=env["contract_v1"])
    body = env["client"].get("/data-api/retrieval-test/chunks/columns", headers={"X-API-Key": KEY}).json()
    assert body["text_query_columns"] == ["embedding"]
    assert body["retrieval"]["modes"] == ["hybrid", "dense", "sparse"] and body["retrieval"]["max_queries"] == 10
    assert body["retrieval"]["response_contract"] == "rim_chunks v1" and body["retrieval"]["exact_search"] is False
    assert "fake" not in json.dumps(body["retrieval"])       # no deployment names leak to consumers
    env["disable"]("chunks")
    body = env["client"].get("/data-api/retrieval-test/chunks/columns", headers={"X-API-Key": KEY}).json()
    assert body["retrieval"] is None and body["text_query_columns"] == [] and body["vector_search"] is True


def test_admin_options_describe_the_table(env):
    r = env["client"].get(f"/data-api/retrieval-test/tables/{env['ids']['chunks']}/search")
    assert r.status_code == 200
    opts = r.json()["options"]
    assert opts["supported"] and opts["row_estimate"] == 600 and opts["unique_key"] == ["chunk_id"]
    assert opts["vector_columns"] == [{"name": "embedding", "type": "vector", "dimensions": 8,
                                       "pgvector": opts["vector_columns"][0]["pgvector"], "ann_metrics": ["cosine"]}]
    assert {"name": "chunk_text", "type": "text", "gin_configs": ["english"]} in opts["text_columns"]


# --------------------------------------------------------------------- MCP

def test_mcp_tools_end_to_end(env, monkeypatch):
    pytest.importorskip("mcp.server.mcpserver")
    httpx2 = pytest.importorskip("httpx2")
    import asyncio
    import socket
    import uvicorn
    from fastapi import FastAPI
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client
    import data_api_mcp

    env["save"]("chunks", contract_id=env["contract_v1"])
    monkeypatch.setenv("DATA_API_MCP_ENABLED", "1")
    app = FastAPI()
    app.include_router(env["api"].data_api_router)
    assert data_api_mcp.install_data_api_mcp(app) is True
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    async def session(key, fn):
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}) as http:
            async with streamable_http_client(f"http://127.0.0.1:{port}/data-api/mcp/retrieval-test",
                                              http_client=http) as (read, write, *_):
                async with ClientSession(read, write) as s:
                    await s.initialize()
                    return await fn(s)

    async def happy(s):
        tools = {t.name: t for t in (await s.list_tools()).tools}
        listed = await s.call_tool("list_tables", {})
        described = await s.call_tool("describe_table", {"alias": "chunks"})
        found = await s.call_tool("search", {"alias": "chunks", "query": "aseptic filling chunk 54", "k": 3})
        bad_alias = await s.call_tool("search", {"alias": "nope", "query": "x"})
        return tools, listed, described, found, bad_alias

    async def unauthorized(s):
        return await s.call_tool("list_tables", {})

    try:
        tools, listed, described, found, bad_alias = asyncio.run(session(KEY, happy))
        denied = asyncio.run(session("wrong-key", unauthorized))
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    assert set(tools) == {"list_tables", "describe_table", "search"}
    assert all(t.annotations.read_only_hint for t in tools.values())
    tables = json.loads(listed.content[0].text)["tables"]
    assert {"alias": "chunks", "semantic_search": True, "search_modes": ["hybrid", "dense", "sparse"]} in tables
    assert {"alias": "nofts", "semantic_search": False, "search_modes": []} in tables
    assert json.loads(described.content[0].text)["search"]["modes"]
    assert json.loads(found.content[0].text)["matches"][0]["rimdocs_metadata"]["object_name"] == "Doc 15"
    assert bad_alias.is_error and "No table with alias" in bad_alias.content[0].text
    assert denied.is_error and "Invalid API key" in denied.content[0].text