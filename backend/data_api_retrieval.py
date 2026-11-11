"""Semantic / hybrid retrieval support for the Data API.

Everything here is deliberately free of FastAPI routing and of data_api.py
internals so it can be unit tested on its own. All configuration lives in the
Data API metadata database and is managed from the portal (Data API Export,
"Semantic search" tab); nothing about a table, model or response shape is
hardcoded. This module validates that configuration and implements:

  1. Configuration  - validation of the three admin-managed objects:
                      embedding models, response contracts, alias settings.
  2. Query embedding - text -> vector with the registered model, cached.
  3. Fusion          - Reciprocal Rank Fusion of the dense and keyword legs.
  4. Response mapping - rows -> the nested JSON contract an API publishes.

Which model built a column's vectors is a fact about the DATA, so it is
registered once per physical column and shared by every API that exposes the
column. Everything else (modes, weights, limits, response contract) is a
choice per API alias. That split lets separate APIs over the same table differ
where they may, and agree where they must.
"""

from __future__ import annotations

import logging
import math
import os
import re
import sys
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

from pydantic import BaseModel, Field, StrictInt, StrictStr

logger = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_TS_CONFIG = re.compile(r"^[A-Za-z0-9_.]+$")
# Only Azure OpenAI key variables may be referenced from the portal, so an
# admin can never make the server send some other secret to an endpoint.
_KEY_ENV = re.compile(r"^AZURE_OPENAI_[A-Z0-9_]*API_KEY$")
SCORE_FIELDS = ("rank", "score", "dense_score", "sparse_score")
MODES = ("hybrid", "dense", "sparse")
METRICS = ("cosine", "inner_product")
MAX_CANDIDATE_POOL = 1000
DEFAULT_KEYWORD_TIMEOUT_MS = 3000


class SearchConfigError(ValueError):
    """Invalid admin configuration. The message is shown to the admin."""


class EmbeddingUnavailable(Exception):
    """The embedding provider failed or is not configured."""

    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


# ============================================================================
# 1. CONFIGURATION (validated at save time and again when used)
# ============================================================================

def allowed_embedding_hosts() -> List[str]:
    """Hosts an embedding endpoint may point at: the hosts of the Azure OpenAI
    endpoints already configured for this server (environment, backend/.env or
    dq_config.json's "assistant" section), plus any listed by ops in
    DATA_API_EMBED_ALLOWED_HOSTS. Server configuration, never portal input,
    because an endpoint receives an API key."""
    hosts = set()
    configured = [os.environ.get("AZURE_OPENAI_ENDPOINT", ""), _env("AZURE_OPENAI_EMBED_ENDPOINT"),
                  str(_assistant_config().get("azure_endpoint") or "")]
    for url in configured:
        host = urlparse(url).hostname
        if host:
            hosts.add(host.lower())
    for h in os.environ.get("DATA_API_EMBED_ALLOWED_HOSTS", "").split(","):
        if h.strip():
            hosts.add(h.strip().lower())
    return sorted(hosts)


def validate_model(raw: dict) -> dict:
    """Normalised embedding-model record, or SearchConfigError."""
    if not isinstance(raw, dict):
        raise SearchConfigError("Model definition must be an object.")
    name = str(raw.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\- ]{0,63}", name):
        raise SearchConfigError("Name: 1-64 letters, digits, spaces, dot, dash or underscore.")
    deployment = str(raw.get("deployment") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}", deployment):
        raise SearchConfigError("Deployment: the Azure OpenAI deployment name (letters, digits, dot, dash, underscore).")
    dims = raw.get("dimensions")
    if isinstance(dims, bool) or not isinstance(dims, int) or not 1 <= dims <= 16000:
        raise SearchConfigError("Dimensions must be a whole number from 1 to 16000.")
    endpoint = str(raw.get("endpoint") or "").strip()
    if endpoint:
        u = urlparse(endpoint)
        if u.scheme != "https" or not u.hostname:
            raise SearchConfigError("Endpoint must be an https:// URL, or blank to use the server's configured endpoint.")
        allowed = allowed_embedding_hosts()
        if u.hostname.lower() not in allowed:
            raise SearchConfigError(
                f"Endpoint host '{u.hostname}' is not allowed. Allowed: {', '.join(allowed) or 'none configured'}. "
                "Server operators add hosts with DATA_API_EMBED_ALLOWED_HOSTS.")
    api_version = str(raw.get("api_version") or "").strip()
    if api_version and not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(-preview)?", api_version):
        raise SearchConfigError("API version looks like 2024-08-01-preview, or blank for the server default.")
    key_env = str(raw.get("api_key_env") or "AZURE_OPENAI_EMBED_API_KEY").strip()
    if not _KEY_ENV.match(key_env):
        raise SearchConfigError("API key variable must be an AZURE_OPENAI_..._API_KEY environment variable name.")
    prefix = raw.get("query_prefix") or ""
    if not isinstance(prefix, str) or len(prefix) > 200:
        raise SearchConfigError("Query prefix must be text of at most 200 characters.")
    return {"name": name, "deployment": deployment, "dimensions": dims, "endpoint": endpoint,
            "api_version": api_version, "api_key_env": key_env, "query_prefix": prefix,
            "send_dimensions": bool(raw.get("send_dimensions", False))}


class EmbeddingModel:
    """A registered model. Credentials are never stored: the key is read from
    the named server environment variable when the model is used."""

    def __init__(self, name: str, raw: dict):
        v = validate_model(dict(raw, name=name))
        self.name = v["name"]
        self.deployment = v["deployment"]
        self.dimensions = v["dimensions"]
        self.endpoint = v["endpoint"]
        self.api_version = v["api_version"]
        self.api_key_env = v["api_key_env"]
        self.query_prefix = v["query_prefix"]
        self.send_dimensions = v["send_dimensions"]
        self.id = raw.get("id")

    def cache_identity(self) -> tuple:
        return (self.name, self.deployment, self.endpoint, self.dimensions, self.query_prefix, self.send_dimensions)


class Lookup:
    """One-hop metadata join: base.<on> = lookup.<key>, same database. The
    lookup key must be unique in its table (checked against the catalog when
    an alias is saved), so each row gets exactly one metadata record."""

    def __init__(self, name: str, raw: dict, where: str):
        self.name = name
        self.schema = _req_ident(raw, "schema", where)
        self.table = _req_ident(raw, "table", where)
        self.key = _req_ident(raw, "key", where)
        self.on = _req_ident(raw, "on", where)
        self.columns: List[str] = []  # filled from the response mapping


def validate_contract(definition: dict) -> Tuple[dict, Dict[str, Lookup]]:
    """A response contract: {"matches_key", "fields", "objects", "lookups"}.
    Returns (normalised definition, lookups). Column existence is checked
    later, against each alias that uses the contract."""
    if not isinstance(definition, dict):
        raise SearchConfigError("Contract must be a JSON object.")
    unknown = set(definition) - {"matches_key", "fields", "objects", "lookups"}
    if unknown:
        raise SearchConfigError(f"Unknown contract keys: {', '.join(sorted(unknown))}.")
    lookups: Dict[str, Lookup] = {}
    raw_lookups = definition.get("lookups") or {}
    if not isinstance(raw_lookups, dict):
        raise SearchConfigError("'lookups' must map a name to {schema, table, key, on}.")
    for name, lk in raw_lookups.items():
        if not _IDENT.match(str(name)):
            raise SearchConfigError(f"Invalid lookup name {name!r}.")
        lookups[name] = Lookup(name, lk or {}, f"lookups.{name}")
    parsed = _parse_response(definition, lookups, "contract")
    if not parsed["fields"] and not parsed["objects"]:
        raise SearchConfigError("A contract needs at least one field or object.")
    normalised = {
        "matches_key": parsed["matches_key"],
        "fields": {k: _ref_str(v) for k, v in parsed["fields"].items()},
        "objects": {o: {k: _ref_str(v) for k, v in m.items()} for o, m in parsed["objects"].items()},
        "lookups": {n: {"schema": lk.schema, "table": lk.table, "key": lk.key, "on": lk.on} for n, lk in lookups.items()},
    }
    return normalised, lookups


def _ref_str(ref: Tuple[Optional[str], str]) -> str:
    return ref[1] if ref[0] is None else f"{ref[0]}.{ref[1]}"


DEFAULT_SETTINGS = {
    "enabled": True, "vector_column": None, "metric": "cosine", "text_column": None,
    "text_search_config": None, "modes": None, "default_mode": None, "rrf_k": 60,
    "dense_weight": 1.0, "sparse_weight": 1.0, "candidate_pool": 50,
    "keyword_timeout_ms": DEFAULT_KEYWORD_TIMEOUT_MS, "semantic_timeout_ms": None, "allow_exact": False,
    "contract_id": None,
}


def validate_settings(raw: dict) -> dict:
    """Shape of one alias's search settings (catalog checks happen elsewhere)."""
    if not isinstance(raw, dict):
        raise SearchConfigError("Settings must be an object.")
    unknown = set(raw) - set(DEFAULT_SETTINGS)
    if unknown:
        raise SearchConfigError(f"Unknown settings: {', '.join(sorted(unknown))}.")
    s = dict(DEFAULT_SETTINGS, **raw)
    s["enabled"] = bool(s["enabled"])
    s["allow_exact"] = bool(s["allow_exact"])
    for key in ("vector_column", "text_column"):
        if s[key] in ("", None):
            s[key] = None
        elif not _IDENT.match(str(s[key])):
            raise SearchConfigError(f"'{key}' is not a valid column name.")
    if not s["vector_column"] and not s["text_column"]:
        raise SearchConfigError("Choose an embedding column, a keyword column, or both.")
    if s["metric"] not in METRICS:
        raise SearchConfigError("Metric must be cosine or inner_product.")
    if s["text_search_config"] in ("", None):
        s["text_search_config"] = None
    elif not _TS_CONFIG.match(str(s["text_search_config"])):
        raise SearchConfigError("Invalid text search configuration name.")
    available = []
    if s["vector_column"]:
        available.append("dense")
    if s["text_column"]:
        available.append("sparse")
    if len(available) == 2:
        available.insert(0, "hybrid")
    modes = s["modes"] or available
    if not isinstance(modes, list) or not modes or any(m not in available for m in modes):
        raise SearchConfigError(f"Modes must be a non-empty subset of: {', '.join(available)}.")
    s["modes"] = [m for m in MODES if m in modes]
    s["default_mode"] = s["default_mode"] or s["modes"][0]
    if s["default_mode"] not in s["modes"]:
        raise SearchConfigError("Default mode must be one of the enabled modes.")
    s["rrf_k"] = _num(s, "rrf_k", 1, 1000, "rrf_k")
    s["dense_weight"] = _num(s, "dense_weight", 0, 100, "Semantic weight")
    s["sparse_weight"] = _num(s, "sparse_weight", 0, 100, "Keyword weight")
    if s["dense_weight"] == 0 and s["sparse_weight"] == 0:
        raise SearchConfigError("At least one weight must be above zero.")
    s["candidate_pool"] = int(_num(s, "candidate_pool", 1, MAX_CANDIDATE_POOL, "Candidate pool"))
    s["keyword_timeout_ms"] = int(_num(s, "keyword_timeout_ms", 100, 120000, "Keyword timeout"))
    # None = the server default (DATA_API_VECTOR_TIMEOUT_MS).
    if s["semantic_timeout_ms"] in ("", None):
        s["semantic_timeout_ms"] = None
    else:
        s["semantic_timeout_ms"] = int(_num(s, "semantic_timeout_ms", 100, 120000, "Semantic timeout"))
    if s["contract_id"] in ("", None):
        s["contract_id"] = None
    elif not isinstance(s["contract_id"], str):
        raise SearchConfigError("contract_id must be a string.")
    return s


class SearchProfile:
    """Everything one request needs, assembled from validated metadata rows:
    the alias settings, the model registered for its embedding column, and
    its response contract (if any)."""

    def __init__(self, settings: dict, embeddings: Dict[str, EmbeddingModel],
                 contract: Optional[dict] = None, contract_label: Optional[str] = None):
        s = validate_settings(settings)
        self.settings = s
        self.enabled = s["enabled"]
        self.embeddings = embeddings
        self.vector_column = s["vector_column"]
        self.metric = s["metric"]
        self.text_column = s["text_column"]
        self.text_search_config = s["text_search_config"]
        self.modes = s["modes"]
        self.default_mode = s["default_mode"]
        self.rrf_k = s["rrf_k"]
        self.dense_weight = s["dense_weight"]
        self.sparse_weight = s["sparse_weight"]
        self.candidate_pool = s["candidate_pool"]
        self.keyword_timeout_ms = s["keyword_timeout_ms"]
        self.semantic_timeout_ms = s["semantic_timeout_ms"]
        self.allow_exact = s["allow_exact"]
        self.contract_label = contract_label
        self.lookups: Dict[str, Lookup] = {}
        self.response = None
        if contract:
            normalised, self.lookups = validate_contract(contract)
            self.response = _parse_response(normalised, self.lookups, "contract")

    def base_columns(self) -> List[str]:
        """Base-table columns the response mapping reads (plus lookup join columns)."""
        cols = []
        if self.response:
            for ref in _iter_refs(self.response):
                if ref[0] is None:
                    cols.append(ref[1])
        cols += [lk.on for lk in self.lookups.values()]
        return list(dict.fromkeys(cols))

    def describe(self) -> dict:
        """What consumers may see (no model endpoints, deployments or secrets)."""
        return {
            "modes": self.modes,
            "default_mode": self.default_mode,
            "vector_column": self.vector_column,
            "text_query_columns": sorted(self.embeddings),
            "metric": self.metric,
            "keyword_column": self.text_column,
            "fusion": "reciprocal_rank_fusion",
            "exact_search": self.allow_exact,
            "response_contract": self.contract_label,
        }


def _parse_response(raw, lookups: Dict[str, Lookup], where: str) -> dict:
    """{"matches_key", "fields": {out: ref}, "objects": {name: {out: ref}}}
    where ref is "column" (base table) or "<lookup>.column"."""
    matches_key = raw.get("matches_key") or "matches"
    if not isinstance(matches_key, str) or not _IDENT.match(matches_key):
        raise SearchConfigError(f"{where}: invalid 'matches_key'.")
    fields = raw.get("fields") or {}
    objects = raw.get("objects") or {}
    if not isinstance(fields, dict) or not isinstance(objects, dict):
        raise SearchConfigError(f"{where}: 'fields' and 'objects' must be objects.")
    parsed = {"matches_key": matches_key, "fields": {}, "objects": {}}
    seen = set(SCORE_FIELDS)
    for lk in lookups.values():
        lk.columns = []
    for out, ref in fields.items():
        if out in seen or not _IDENT.match(str(out)):
            raise SearchConfigError(f"{where}: response field {out!r} is invalid, reserved or duplicated.")
        seen.add(out)
        parsed["fields"][out] = _parse_ref(ref, lookups, where)
    for obj_name, members in objects.items():
        if obj_name in seen or not _IDENT.match(str(obj_name)) or not isinstance(members, dict) or not members:
            raise SearchConfigError(f"{where}: response object {obj_name!r} is invalid, reserved, duplicated or empty.")
        seen.add(obj_name)
        for out in members:
            if not _IDENT.match(str(out)):
                raise SearchConfigError(f"{where}: invalid field name {out!r} in {obj_name}.")
        parsed["objects"][obj_name] = {out: _parse_ref(ref, lookups, where) for out, ref in members.items()}
    for ref in _iter_refs(parsed):
        if ref[0] is not None:
            lk = lookups[ref[0]]
            if ref[1] not in lk.columns:
                lk.columns.append(ref[1])
    return parsed


def _parse_ref(ref, lookups: Dict[str, Lookup], where: str) -> Tuple[Optional[str], str]:
    if not isinstance(ref, str):
        raise SearchConfigError(f"{where}: response mapping values must be strings.")
    if "." in ref:
        name, column = ref.split(".", 1)
        if name not in lookups:
            raise SearchConfigError(f"{where}: {ref!r} names an undefined lookup '{name}'.")
    else:
        name, column = None, ref
    if not _IDENT.match(column):
        raise SearchConfigError(f"{where}: invalid column in {ref!r}.")
    return (name, column)


def _iter_refs(response: dict):
    yield from response["fields"].values()
    for members in response["objects"].values():
        yield from members.values()


def _req_ident(raw: dict, key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not _IDENT.match(value.strip()):
        raise SearchConfigError(f"{where}: '{key}' must be a valid identifier.")
    return value.strip()


def _num(raw: dict, key: str, lo, hi, label):
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise SearchConfigError(f"{label} must be a number between {lo} and {hi}.")
    return value


# ============================================================================
# 2. QUERY EMBEDDING
# ============================================================================

EMBED_TIMEOUT_SECONDS = float(os.environ.get("DATA_API_EMBED_TIMEOUT_SECONDS", "10"))
EMBED_MAX_RETRIES = int(os.environ.get("DATA_API_EMBED_MAX_RETRIES", "2"))
MAX_QUERY_CHARS = int(os.environ.get("DATA_API_MAX_QUERY_CHARS", "4000"))
_EMBED_CACHE_SIZE = int(os.environ.get("DATA_API_EMBED_CACHE_ENTRIES", "2048"))


class _EmbeddingCache:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._data: "OrderedDict[tuple, List[float]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            value = self._data.get(key)
            if value is not None:
                self._data.move_to_end(key)
            return value

    def put(self, key, value):
        if self.capacity <= 0:
            return
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self.capacity:
                self._data.popitem(last=False)


_cache = _EmbeddingCache(_EMBED_CACHE_SIZE)
_clients: Dict[tuple, Any] = {}
_clients_lock = threading.Lock()


_DEFAULT_FALLBACKS = {
    # Same convention as assistant/router.py and .env.example: the EMBED_*
    # variables fall back to the main Azure OpenAI ones when left blank.
    "AZURE_OPENAI_EMBED_ENDPOINT": "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_EMBED_API_KEY": "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_EMBED_API_VERSION": "AZURE_OPENAI_API_VERSION",
}


# Keys of the "assistant" section of dq_config.json that the assistant reads
# for embeddings (assistant/router.py _settings); same names, same precedence.
_CONFIG_KEYS = {
    "AZURE_OPENAI_EMBED_ENDPOINT": "embed_endpoint",
    "AZURE_OPENAI_EMBED_API_KEY": "embed_api_key",
    "AZURE_OPENAI_EMBED_API_VERSION": "embed_api_version",
}
_dotenv_loaded = False


def _assistant_config() -> dict:
    """dq_config.json's "assistant" section, from the already-loaded app
    module. Never imports app (that would start a second portal)."""
    for name in ("app", "__main__"):
        config = getattr(sys.modules.get(name), "CONFIG", None)
        if isinstance(config, dict):
            section = config.get("assistant") or {}
            return section if isinstance(section, dict) else {}
    return {}


def _env(name: str) -> str:
    """Resolve an embedding setting exactly like the portal's assistant does:
    dq_config.json "assistant" section first (default names only), then the
    environment (backend/.env is loaded into it), then, for the EMBED_*
    names, the main AZURE_OPENAI_* variables."""
    global _dotenv_loaded
    if not _dotenv_loaded:
        _dotenv_loaded = True
        try:
            import assistant.env  # noqa: F401  loads backend/.env; never overrides real env vars
        except Exception:
            pass
    config_key = _CONFIG_KEYS.get(name)
    if config_key:
        value = _assistant_config().get(config_key)
        if value:
            return str(value)
    value = os.environ.get(name, "")
    if not value and name in _DEFAULT_FALLBACKS:
        value = os.environ.get(_DEFAULT_FALLBACKS[name], "")
    return value


def credential_configured(api_key_env: str) -> bool:
    """Whether the named key resolves to a value on this server (never returns it)."""
    return bool(_env(api_key_env))


def _client(model: EmbeddingModel):
    if model.endpoint:
        # Re-checked at use: the allowlist is server config and may have changed.
        host = (urlparse(model.endpoint).hostname or "").lower()
        if host not in allowed_embedding_hosts():
            raise EmbeddingUnavailable(f"Embedding model '{model.name}' points at a host this server no longer allows.")
    endpoint = model.endpoint or _env("AZURE_OPENAI_EMBED_ENDPOINT")
    api_key = _env(model.api_key_env)
    api_version = model.api_version or _env("AZURE_OPENAI_EMBED_API_VERSION") or "2024-08-01-preview"
    if not endpoint or not api_key:
        raise EmbeddingUnavailable(f"Embedding model '{model.name}' is not configured on this server.")
    identity = (endpoint, api_key, api_version)
    with _clients_lock:
        client = _clients.get(identity)
        if client is None:
            from openai import AzureOpenAI  # already a backend dependency
            # The SDK's own retry (exponential backoff, honours Retry-After)
            # covers 429/5xx/connection errors; no second retry loop here.
            client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version,
                                 timeout=EMBED_TIMEOUT_SECONDS, max_retries=EMBED_MAX_RETRIES)
            _clients[identity] = client
        return client


def clean_query(text: str) -> str:
    text = " ".join((text or "").split())
    if not text:
        raise ValueError("Query text is empty.")
    if len(text) > MAX_QUERY_CHARS:
        raise ValueError(f"Query text exceeds {MAX_QUERY_CHARS} characters.")
    return text


def embed_queries(model: EmbeddingModel, texts: List[str], embedder=None) -> List[List[float]]:
    """Embed query texts in one provider call (cache hits skipped).

    `embedder(model, texts) -> list of vectors` is injectable for tests.
    Query text and vectors are never logged.
    """
    out: List[Optional[List[float]]] = [None] * len(texts)
    missing: Dict[str, List[int]] = {}
    for i, text in enumerate(texts):
        cached = _cache.get(model.cache_identity() + (text,))
        if cached is not None:
            out[i] = cached
        else:
            missing.setdefault(text, []).append(i)
    if missing:
        unique = list(missing)
        vectors = (embedder or _azure_embed)(model, [model.query_prefix + t for t in unique])
        if len(vectors) != len(unique):
            raise EmbeddingUnavailable("Embedding provider returned an unexpected number of vectors.")
        for text, vector in zip(unique, vectors):
            vector = [float(v) for v in vector]
            if len(vector) != model.dimensions or not all(math.isfinite(v) for v in vector):
                # Wrong deployment behind the configured name: fail loudly
                # rather than search with an incompatible vector.
                raise EmbeddingUnavailable(
                    f"Embedding model '{model.name}' returned {len(vector)} dimensions; profile expects {model.dimensions}.")
            _cache.put(model.cache_identity() + (text,), vector)
            for i in missing[text]:
                out[i] = vector
    return out  # type: ignore[return-value]


def _azure_embed(model: EmbeddingModel, inputs: List[str]) -> List[List[float]]:
    client = _client(model)
    kwargs = {"model": model.deployment, "input": inputs}
    if model.send_dimensions:
        kwargs["dimensions"] = model.dimensions
    try:
        response = client.embeddings.create(**kwargs)
    except Exception as e:  # SDK already retried; classify, never echo inputs
        status = getattr(e, "status_code", None)
        retryable = status in (408, 409, 429) or (status or 0) >= 500 or status is None
        logger.warning("Embedding call failed for model %s (%s, status=%s)", model.name, type(e).__name__, status)
        raise EmbeddingUnavailable("The embedding service is unavailable. Retry with backoff.", retryable=retryable) from None
    data = sorted(response.data, key=lambda d: d.index)
    return [d.embedding for d in data]


# ============================================================================
# 3. FUSION
# ============================================================================

def reciprocal_rank_fusion(dense: List[tuple], sparse: List[tuple], k: int, rrf_k: float = 60,
                           dense_weight: float = 1.0, sparse_weight: float = 1.0) -> List[dict]:
    """dense: [(key, distance)] nearest first; sparse: [(key, score)] best first.

    score = sum(weight / (rrf_k + rank)). Ties break on best single-leg rank,
    then on the key, so equal inputs always give equal output.
    """
    fused: Dict[tuple, dict] = {}
    for rank, (key, distance) in enumerate(dense, 1):
        item = fused.setdefault(key, {"key": key, "score": 0.0, "dense_rank": None, "sparse_rank": None,
                                      "distance": None, "sparse_score": None})
        item["score"] += dense_weight / (rrf_k + rank)
        item["dense_rank"], item["distance"] = rank, distance
    for rank, (key, score) in enumerate(sparse, 1):
        item = fused.setdefault(key, {"key": key, "score": 0.0, "dense_rank": None, "sparse_rank": None,
                                      "distance": None, "sparse_score": None})
        item["score"] += sparse_weight / (rrf_k + rank)
        item["sparse_rank"], item["sparse_score"] = rank, score
    best = lambda i: min(r for r in (i["dense_rank"], i["sparse_rank"]) if r is not None)
    ordered = sorted(fused.values(), key=lambda i: (-i["score"], best(i), _sortable(i["key"])))
    return ordered[:k]


def _sortable(key: tuple) -> tuple:
    return tuple((v is None, str(v)) for v in key)


def dense_similarity(distance: Optional[float], metric: str) -> Optional[float]:
    if distance is None:
        return None
    return 1.0 - distance if metric == "cosine" else -distance


# ============================================================================
# 4. RESPONSE MAPPING
# ============================================================================

def build_match(item: dict, rank: int, row: dict, lookup_rows: Dict[str, Optional[dict]],
                profile: SearchProfile, default_columns: List[str]) -> dict:
    """One match. Contract fields are always present; missing values are null."""
    scores = {
        "rank": rank,
        "score": item["score"],
        "dense_score": dense_similarity(item["distance"], profile.metric),
        "sparse_score": item["sparse_score"],
    }
    if not profile.response:
        return {**scores, "row": {c: row.get(c) for c in default_columns}}

    def value(ref):
        name, column = ref
        source = row if name is None else (lookup_rows.get(name) or {})
        return source.get(column)

    out = dict(scores)
    for field, ref in profile.response["fields"].items():
        out[field] = value(ref)
    for obj_name, members in profile.response["objects"].items():
        out[obj_name] = {field: value(ref) for field, ref in members.items()}
    return out


# ============================================================================
# REQUEST MODEL (/retrieve)
# ============================================================================

class _Strict(BaseModel):
    if hasattr(BaseModel, "model_validate"):
        model_config = {"extra": "forbid"}
    else:  # Pydantic 1.x compatibility with older existing deployments
        class Config:
            extra = "forbid"


class RetrievalQuery(_Strict):
    query: StrictStr
    k: StrictInt = 10
    where: List[StrictStr] = Field(default_factory=list)
    mode: Optional[Literal["hybrid", "dense", "sparse"]] = None


class RetrievalRequest(_Strict):
    retrieval_queries: List[RetrievalQuery]
    vector_column: Optional[StrictStr] = None
    search_mode: Literal["auto", "exact"] = "auto"