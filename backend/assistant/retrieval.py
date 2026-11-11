"""
Retrieval over the portal knowledge base.

Two backends behind the same retrieve() call:

  - numpy (default)   Loads knowledge/portal_kb.jsonl into memory, cosine sim
                       via numpy. Fine for a few thousand chunks, which is all
                       a single portal's docs will ever be. No setup needed.

  - azure_search       Delegates to an Azure AI Search index instead of the
                       local file. Only turns on if ASSISTANT_KB_BACKEND=
                       azure_search AND the AZURE_SEARCH_* vars are set; if
                       either is missing this backend logs once and returns no
                       hits rather than erroring the whole assistant out.

Pick with the ASSISTANT_KB_BACKEND env var ("numpy" | "azure_search"), default
numpy. Everything else in the codebase only ever calls retrieve() — swapping
backends (or adding a third) doesn't touch router.py or tools.py at all.
"""

import json
import logging
import os
from collections import OrderedDict
from pathlib import Path

import numpy as np
import requests

logger = logging.getLogger(__name__)

_HERE = Path(__file__).parent
KB_FILE = _HERE / "knowledge" / "portal_kb.jsonl"

# Same question gets asked a lot ("how are we doing overall", "what's on this
# screen"). Embedding is a network round trip for basically no benefit if we
# just asked the same thing a minute ago, so cache it. Small and bounded —
# this is a convenience cache, not a knowledge store. Shared by both backends.
_EMBED_CACHE_SIZE = 200
_embed_cache: "OrderedDict[tuple, list]" = OrderedDict()


def _cached_embedding(client, question: str, embed_model: str) -> np.ndarray:
    key = (embed_model, question.strip().lower())
    cached = _embed_cache.get(key)
    if cached is not None:
        _embed_cache.move_to_end(key)
        return np.array(cached, dtype=np.float32)

    vec = client.embeddings.create(model=embed_model, input=question).data[0].embedding
    _embed_cache[key] = vec
    _embed_cache.move_to_end(key)
    if len(_embed_cache) > _EMBED_CACHE_SIZE:
        _embed_cache.popitem(last=False)
    return np.array(vec, dtype=np.float32)


class _NumpyBackend:
    """The original in-memory implementation. Unchanged behavior."""

    def __init__(self):
        self._chunks: list[dict] = []
        self._matrix: np.ndarray | None = None

    def load(self, force: bool = False):
        if self._matrix is not None and not force:
            return
        if not KB_FILE.exists():
            logger.warning("knowledge base not built yet (%s missing). Run build_kb.py.", KB_FILE)
            self._chunks, self._matrix = [], None
            return
        rows = [json.loads(l) for l in KB_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]
        self._chunks = rows
        vecs = np.array([r["embedding"] for r in rows], dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms[norms == 0] = 1
        self._matrix = vecs / norms
        logger.info("assistant KB loaded (numpy backend): %d chunks", len(rows))

    def retrieve(self, client, question, embed_model, k, min_score):
        self.load()
        if self._matrix is None or not self._chunks:
            return []
        q = _cached_embedding(client, question, embed_model)
        q /= (np.linalg.norm(q) or 1)
        scores = self._matrix @ q
        top = np.argsort(scores)[::-1][:k]
        hits = []
        for i in top:
            if scores[i] < min_score:
                continue
            c = self._chunks[i]
            hits.append({
                "title": c.get("title", ""),
                "source": c.get("source", ""),
                "text": c["text"],
                "score": round(float(scores[i]), 3),
            })
        return hits


class _AzureSearchBackend:
    """Delegates to an Azure AI Search index using vector search. Swap target
    for when the numpy backend's "load the whole KB into memory" stops being
    fine (the comment above put that around a few thousand chunks).

    Needs an index with at least: a "content_vector" (Collection(Edm.Single))
    field built from the same embedding model as ASSISTANT_EMBED_DEPLOYMENT,
    plus "title", "source", "text" retrievable fields. Build/sync the index
    however you like (build_kb.py's chunking is a reasonable starting point);
    this class only queries it.
    """

    def __init__(self):
        self.endpoint = (os.getenv("AZURE_SEARCH_ENDPOINT") or "").rstrip("/")
        self.api_key = os.getenv("AZURE_SEARCH_API_KEY")
        self.index = os.getenv("AZURE_SEARCH_INDEX", "portal-kb")
        self.api_version = os.getenv("AZURE_SEARCH_API_VERSION", "2023-11-01")
        self._warned = False

    def _ready(self) -> bool:
        if self.endpoint and self.api_key:
            return True
        if not self._warned:
            logger.warning(
                "ASSISTANT_KB_BACKEND=azure_search but AZURE_SEARCH_ENDPOINT/API_KEY "
                "aren't set — falling back to no retrieval results (not an error, but "
                "the assistant will lose grounding until this is configured)."
            )
            self._warned = True
        return False

    def load(self, force: bool = False):
        pass  # nothing to preload — each query hits the managed index directly

    def retrieve(self, client, question, embed_model, k, min_score):
        if not self._ready():
            return []
        try:
            q = _cached_embedding(client, question, embed_model)
            url = f"{self.endpoint}/indexes/{self.index}/docs/search"
            body = {
                "vectorQueries": [{
                    "kind": "vector",
                    "vector": q.tolist(),
                    "fields": "content_vector",
                    "k": k,
                }],
                "select": "title,source,text",
            }
            r = requests.post(
                url, params={"api-version": self.api_version},
                headers={"api-key": self.api_key, "Content-Type": "application/json"},
                json=body, timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("value", [])
            hits = []
            for doc in results:
                score = doc.get("@search.score", 0.0)
                if score < min_score:
                    continue
                hits.append({
                    "title": doc.get("title", ""),
                    "source": doc.get("source", ""),
                    "text": doc.get("text", ""),
                    "score": round(float(score), 3),
                })
            return hits
        except requests.RequestException as e:
            logger.warning("azure_search retrieval failed: %s", e)
            return []


_backend = None


def _get_backend():
    global _backend
    if _backend is not None:
        return _backend
    choice = os.getenv("ASSISTANT_KB_BACKEND", "numpy").strip().lower()
    if choice == "azure_search":
        _backend = _AzureSearchBackend()
    else:
        _backend = _NumpyBackend()
    return _backend


def load(force: bool = False):
    _get_backend().load(force=force)


def retrieve(client, question: str, embed_model: str, k: int = 8, min_score: float = 0.25) -> list[dict]:
    """Return up to k relevant chunks as {title, source, text, score}.
    Chunks below min_score are dropped so weak matches don't get cited as fact."""
    if not question:
        return []
    return _get_backend().retrieve(client, question, embed_model, k, min_score)