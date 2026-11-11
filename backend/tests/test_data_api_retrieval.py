"""Unit tests for data_api_retrieval (no database, no network).

Run from backend/:  python -m pytest tests/test_data_api_retrieval.py -q
"""

import os

import pytest

import data_api_retrieval as r
from data_api_runtime import AdmissionGate


CONTRACT = {
    "lookups": {"doc": {"schema": "vector_embedding", "table": "rim_docs", "key": "chunk_source_id",
                        "on": "chunk_source_id"}},
    "fields": {"chunk_id": "chunk_id", "chunk_text": "chunk_text"},
    "objects": {"rimdocs_metadata": {"object_name": "doc.object_name", "a_status": "doc.a_status"},
                "document_metadata": {"title": "doc.title"}},
}
M1 = r.EmbeddingModel("m1", {"deployment": "emb", "dimensions": 3})


def _settings(**over):
    base = {"vector_column": "embedding", "text_column": "chunk_text"}
    base.update(over)
    return base


# ---------------------------------------------------------------- settings

def test_settings_defaults_and_modes():
    s = r.validate_settings(_settings())
    assert s["modes"] == ["hybrid", "dense", "sparse"] and s["default_mode"] == "hybrid"
    assert s["allow_exact"] is False and s["enabled"] is True and s["keyword_timeout_ms"] == 3000
    assert r.validate_settings({"text_column": "body"})["modes"] == ["sparse"]
    assert r.validate_settings(_settings(modes=["sparse", "dense"]))["modes"] == ["dense", "sparse"]


@pytest.mark.parametrize("override, message", [
    ({"vector_column": None, "text_column": None}, "Choose an embedding column"),
    ({"metric": "l2"}, "cosine or inner_product"),
    ({"modes": ["hybrid"], "text_column": None}, "subset"),
    ({"default_mode": "sparse", "modes": ["dense"]}, "Default mode"),
    ({"candidate_pool": 5000}, "Candidate pool"),
    ({"keyword_timeout_ms": 10}, "Keyword timeout"),
    ({"dense_weight": 0, "sparse_weight": 0}, "At least one weight"),
    ({"text_search_config": "english'); drop"}, "text search configuration"),
    ({"vector_column": "bad name"}, "valid column"),
    ({"surprise": 1}, "Unknown settings"),
])
def test_invalid_settings_are_rejected(override, message):
    with pytest.raises(r.SearchConfigError, match=message):
        r.validate_settings(_settings(**override))


# ---------------------------------------------------------------- contracts

def test_contract_normalises_and_derives_lookup_columns():
    normalised, lookups = r.validate_contract(CONTRACT)
    assert normalised["matches_key"] == "matches"
    assert lookups["doc"].columns == ["object_name", "a_status", "title"]
    p = r.SearchProfile(_settings(), {"embedding": M1}, CONTRACT, "rim v1")
    assert p.base_columns() == ["chunk_id", "chunk_text", "chunk_source_id"]
    assert p.describe()["response_contract"] == "rim v1"


@pytest.mark.parametrize("definition, message", [
    ({"fields": {"rank": "chunk_id"}}, "reserved"),
    ({"fields": {"x": "missing.col"}}, "undefined lookup"),
    ({"fields": {}}, "at least one"),
    ({"fields": {"x": "a"}, "extra": 1}, "Unknown contract keys"),
    ({"fields": {"x": "a"}, "lookups": {"d": {"schema": "s", "table": "t", "key": "k"}}}, "'on'"),
    ({"objects": {"o": {}}}, "empty"),
])
def test_invalid_contracts_are_rejected(definition, message):
    with pytest.raises(r.SearchConfigError, match=message):
        r.validate_contract(definition)


# ---------------------------------------------------------------- models

def test_model_validation_and_endpoint_allowlist(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://main.openai.azure.com/")
    monkeypatch.delenv("AZURE_OPENAI_EMBED_ENDPOINT", raising=False)
    monkeypatch.setenv("DATA_API_EMBED_ALLOWED_HOSTS", "gateway.example.com")
    ok = r.validate_model({"name": "m", "deployment": "text-embedding-3-small", "dimensions": 1536,
                           "endpoint": "https://gateway.example.com/openai"})
    assert ok["api_key_env"] == "AZURE_OPENAI_EMBED_API_KEY"
    assert r.allowed_embedding_hosts() == ["gateway.example.com", "main.openai.azure.com"]
    with pytest.raises(r.SearchConfigError, match="not allowed"):
        r.validate_model({"name": "m", "deployment": "d", "dimensions": 3, "endpoint": "https://evil.example.com"})
    with pytest.raises(r.SearchConfigError, match="https"):
        r.validate_model({"name": "m", "deployment": "d", "dimensions": 3, "endpoint": "http://gateway.example.com"})
    # A portal admin must not be able to make the server send some other secret.
    with pytest.raises(r.SearchConfigError, match="API key variable"):
        r.validate_model({"name": "m", "deployment": "d", "dimensions": 3, "api_key_env": "SNOWFLAKE_PASSWORD"})
    with pytest.raises(r.SearchConfigError, match="Dimensions"):
        r.validate_model({"name": "m", "deployment": "d", "dimensions": 0})


def test_client_rechecks_allowlist_at_use(monkeypatch):
    monkeypatch.setenv("DATA_API_EMBED_ALLOWED_HOSTS", "gateway.example.com")
    model = r.EmbeddingModel("m", {"deployment": "d", "dimensions": 3, "endpoint": "https://gateway.example.com"})
    monkeypatch.setenv("DATA_API_EMBED_ALLOWED_HOSTS", "")   # ops removed the host later
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_EMBED_ENDPOINT", raising=False)
    with pytest.raises(r.EmbeddingUnavailable, match="no longer allows"):
        r._client(model)


# --------------------------------------------------------------- embedding

MODEL = r.EmbeddingModel("m1", {"deployment": "emb", "dimensions": 3, "query_prefix": "query: "})


def test_embed_batches_dedupes_and_caches():
    calls = []

    def fake(model, inputs):
        calls.append(list(inputs))
        return [[1.0, float(len(t)), 0.5] for t in inputs]

    r._cache._data.clear()
    out = r.embed_queries(MODEL, ["a b", "cd", "a b"], embedder=fake)
    assert calls == [["query: a b", "query: cd"]]        # one call, deduped, prefixed
    assert out[0] == out[2] and out[0] != out[1]
    r.embed_queries(MODEL, ["cd"], embedder=fake)
    assert len(calls) == 1                                 # served from cache


def test_embedding_dimension_mismatch_fails_loudly():
    r._cache._data.clear()
    with pytest.raises(r.EmbeddingUnavailable, match="returned 2 dimensions"):
        r.embed_queries(MODEL, ["x"], embedder=lambda m, t: [[0.1, 0.2]])


def test_unconfigured_model_is_unavailable(monkeypatch):
    monkeypatch.delenv("AZURE_OPENAI_EMBED_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_EMBED_API_KEY", raising=False)
    r._cache._data.clear()
    with pytest.raises(r.EmbeddingUnavailable, match="not configured"):
        r.embed_queries(MODEL, ["x"])


def test_embed_env_falls_back_to_main_azure_vars(monkeypatch):
    # Repo convention (.env.example, assistant/router.py): blank EMBED_* vars
    # fall back to the main AZURE_OPENAI_* ones.
    for name in ("AZURE_OPENAI_EMBED_ENDPOINT", "AZURE_OPENAI_EMBED_API_KEY", "AZURE_OPENAI_EMBED_API_VERSION"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://main.example.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "main-key")
    monkeypatch.delenv("AZURE_OPENAI_API_VERSION", raising=False)
    r._clients.clear()
    client = r._client(MODEL)
    assert str(client._azure_endpoint if hasattr(client, "_azure_endpoint") else client.base_url).startswith("https://main.example.com")
    assert client.api_key == "main-key"
    monkeypatch.setenv("AZURE_OPENAI_EMBED_ENDPOINT", "https://embed.example.com")
    assert r._env("AZURE_OPENAI_EMBED_ENDPOINT") == "https://embed.example.com"   # explicit value wins
    r._clients.clear()


def test_credentials_resolve_like_the_assistant(monkeypatch):
    """Same order as assistant/router.py: dq_config.json "assistant" section,
    then environment (.env), then the main AZURE_OPENAI_* variables."""
    import sys
    import types
    fake_app = types.ModuleType("app")
    fake_app.CONFIG = {"assistant": {"embed_api_key": "key-from-config",
                                     "embed_endpoint": "https://config-gateway.example.com/openai"}}
    monkeypatch.setitem(sys.modules, "app", fake_app)
    monkeypatch.setenv("AZURE_OPENAI_EMBED_API_KEY", "key-from-env")
    monkeypatch.setenv("AZURE_OPENAI_EMBED_API_VERSION", "2024-10-21")
    monkeypatch.delenv("DATA_API_EMBED_ALLOWED_HOSTS", raising=False)
    assert r._env("AZURE_OPENAI_EMBED_API_KEY") == "key-from-config"       # config wins, as for the assistant
    assert r._env("AZURE_OPENAI_EMBED_API_VERSION") == "2024-10-21"        # not in config: environment
    assert r.credential_configured("AZURE_OPENAI_EMBED_API_KEY")
    assert "config-gateway.example.com" in r.allowed_embedding_hosts()     # config endpoint is allowed
    fake_app.CONFIG = {"assistant": {}}
    assert r._env("AZURE_OPENAI_EMBED_API_KEY") == "key-from-env"
    monkeypatch.delenv("AZURE_OPENAI_EMBED_API_KEY")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "main-key")
    assert r._env("AZURE_OPENAI_EMBED_API_KEY") == "main-key"             # EMBED_* falls back to main
    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    assert not r.credential_configured("AZURE_OPENAI_EMBED_API_KEY")
    # custom key variables come only from the environment / .env, never from config
    monkeypatch.setenv("AZURE_OPENAI_TEAMX_API_KEY", "teamx")
    assert r.credential_configured("AZURE_OPENAI_TEAMX_API_KEY")


def test_config_is_read_without_importing_app(monkeypatch):
    import sys
    monkeypatch.delitem(sys.modules, "app", raising=False)
    assert r._assistant_config() == {} and "app" not in sys.modules      # never starts a second portal


def test_clean_query():
    assert r.clean_query("  sterility \n failure ") == "sterility failure"
    with pytest.raises(ValueError):
        r.clean_query("   ")
    with pytest.raises(ValueError):
        r.clean_query("x" * (r.MAX_QUERY_CHARS + 1))


# ------------------------------------------------------------------ fusion

def test_rrf_scores_ranks_and_single_leg_nulls():
    dense = [(("a",), 0.1), (("b",), 0.2), (("c",), 0.3)]
    sparse = [(("c",), 0.9), (("d",), 0.5)]
    fused = r.reciprocal_rank_fusion(dense, sparse, k=4, rrf_k=60)
    # b (dense #2) and d (keyword #2) tie at 1/62; the key breaks the tie.
    assert [i["key"] for i in fused] == [("c",), ("a",), ("b",), ("d",)]
    assert fused[2]["score"] == fused[3]["score"] == pytest.approx(1 / 62)
    c = fused[0]
    assert c["score"] == pytest.approx(1 / 63 + 1 / 61)
    assert (c["distance"], c["sparse_score"]) == (0.3, 0.9)
    d = next(i for i in fused if i["key"] == ("d",))
    assert d["distance"] is None and d["sparse_score"] == 0.5   # keyword-only hit


def test_rrf_is_deterministic_on_ties_and_respects_weights():
    # a and x tie on score (both rank 1 in one leg); key order breaks the tie
    fused = r.reciprocal_rank_fusion([(("x",), 0.1)], [(("a",), 1.0)], k=2)
    assert [i["key"] for i in fused] == [("a",), ("x",)]
    fused = r.reciprocal_rank_fusion([(("x",), 0.1)], [(("a",), 1.0)], k=2, dense_weight=2.0)
    assert fused[0]["key"] == ("x",)


def test_dense_similarity():
    assert r.dense_similarity(0.25, "cosine") == 0.75
    assert r.dense_similarity(-3.0, "inner_product") == 3.0
    assert r.dense_similarity(None, "cosine") is None


# ------------------------------------------------------- response mapping

def test_contract_shape_with_nulls_and_missing_lookup():
    p = r.SearchProfile(_settings(), {"embedding": M1}, CONTRACT)
    item = {"key": ("u1",), "score": 0.03, "distance": 0.2, "sparse_score": None}
    row = {"chunk_id": "u1", "chunk_text": "t", "chunk_source_id": "s1"}
    m = r.build_match(item, 1, row, {"doc": None}, p, [])
    assert m == {"rank": 1, "score": 0.03, "dense_score": pytest.approx(0.8), "sparse_score": None,
                 "chunk_id": "u1", "chunk_text": "t",
                 "rimdocs_metadata": {"object_name": None, "a_status": None},
                 "document_metadata": {"title": None}}


def test_default_shape_without_contract():
    p = r.SearchProfile(_settings(), {"embedding": M1})
    item = {"key": ("u1",), "score": 0.02, "distance": None, "sparse_score": 0.4}
    m = r.build_match(item, 2, {"chunk_id": "u1", "x": 1}, {}, p, ["chunk_id", "x"])
    assert m == {"rank": 2, "score": 0.02, "dense_score": None, "sparse_score": 0.4,
                 "row": {"chunk_id": "u1", "x": 1}}


def test_request_model_rejects_unknown_fields():
    with pytest.raises(Exception):
        r.RetrievalRequest(retrieval_queries=[{"query": "x", "top_k": 3}])


# ------------------------------------------------------------ exact gate

def test_admission_gate_limits_per_key():
    gate = AdmissionGate(total=2, per_caller=1)
    gate.enter("key-a")
    with pytest.raises(Exception) as e:
        gate.enter("key-a")
    assert e.value.status_code == 429
    gate.enter("key-b")                 # a different key still gets the other slot
    with pytest.raises(Exception) as e:
        gate.enter("key-c")
    assert e.value.status_code == 503
    gate.leave("key-a"); gate.leave("key-b")