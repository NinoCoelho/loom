"""Concrete :class:`EmbeddingProvider` implementations.

Two providers are shipped:

* :class:`OllamaEmbeddingProvider` — calls ``/api/embed`` on a local Ollama
  instance.  Zero additional dependencies (just ``httpx``).
* :class:`OpenAIEmbeddingProvider` — calls ``/v1/embeddings`` on any
  OpenAI-compatible endpoint (OpenAI, Azure, local proxy).

Both satisfy the :class:`~loom.store.memory.EmbeddingProvider` protocol and
can be passed to :class:`~loom.store.memory.MemoryStore` or
:class:`~loom.store.graphrag.GraphRAGEngine`.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BATCH_SIZE = 64


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    if mag_a == 0.0 or mag_b == 0.0:
        return 0.0
    return dot / (mag_a * mag_b)


try:
    import numpy as np

    def _batch_cosine(query: list[float], vectors: list[list[float]]) -> list[float]:
        """Cosine similarity of ``query`` against each row in ``vectors``.

        Defensive against mismatched dimensions — caused by mixed embedding
        models in the same index (e.g. switching providers without
        re-indexing). Vectors whose length differs from the query score
        0.0 instead of raising NumPy's "inhomogeneous shape" error.
        Output order matches input order so callers can zip with their
        ``ids`` list.
        """
        if not vectors:
            return []
        qlen = len(query)
        # Fast path: every vector matches the query dimension.
        if all(len(v) == qlen for v in vectors):
            q = np.array(query, dtype=np.float32)
            mat = np.array(vectors, dtype=np.float32)
            dots = mat @ q
            mags = np.linalg.norm(mat, axis=1) * np.linalg.norm(q)
            mags = np.where(mags == 0, 1.0, mags)
            return (dots / mags).tolist()
        # Slow path: skip mismatched vectors, score the rest, preserve order.
        good_idx: list[int] = []
        good_vecs: list[list[float]] = []
        for i, v in enumerate(vectors):
            if len(v) == qlen:
                good_idx.append(i)
                good_vecs.append(v)
        scores = [0.0] * len(vectors)
        if good_vecs:
            q = np.array(query, dtype=np.float32)
            mat = np.array(good_vecs, dtype=np.float32)
            dots = mat @ q
            mags = np.linalg.norm(mat, axis=1) * np.linalg.norm(q)
            mags = np.where(mags == 0, 1.0, mags)
            good_scores = (dots / mags).tolist()
            for idx, sc in zip(good_idx, good_scores):
                scores[idx] = sc
        return scores

except ImportError:

    def _batch_cosine(query: list[float], vectors: list[list[float]]) -> list[float]:
        qlen = len(query)
        return [
            _cosine_similarity(query, v) if len(v) == qlen else 0.0
            for v in vectors
        ]


class OllamaEmbeddingProvider:
    """Embedding provider backed by an Ollama instance.

    Calls ``POST {base_url}/api/embed`` with the configured ``model``.
    """

    def __init__(
        self,
        model: str = "nomic-embed-text",
        base_url: str = "http://localhost:11434",
        dim: int = 768,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.dim = dim
        self._timeout = timeout

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        all_embeddings: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for i in range(0, len(texts), _BATCH_SIZE):
                batch = texts[i : i + _BATCH_SIZE]
                resp = await client.post(
                    f"{self.base_url}/api/embed",
                    json={"model": self.model, "input": batch},
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                all_embeddings.extend(data.get("embeddings", []))
        return all_embeddings


class OpenAIEmbeddingProvider:
    """Embedding provider for OpenAI-compatible ``/v1/embeddings`` endpoints.

    The API key is resolved from the environment variable named by
    ``key_env``.  If ``key_env`` is empty the request is made without
    authentication.
    """

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        base_url: str = "https://api.openai.com/v1",
        key_env: str = "OPENAI_API_KEY",
        dim: int = 1536,
        timeout: float = 60.0,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.key_env = key_env
        self.dim = dim
        self._timeout = timeout

    def _api_key(self) -> str | None:
        if not self.key_env:
            return None
        return os.environ.get(self.key_env)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        all_embeddings: list[list[float]] = []
        headers: dict[str, str] = {"Content-Type": "application/json"}
        api_key = self._api_key()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for i in range(0, len(texts), _BATCH_SIZE):
                batch = texts[i : i + _BATCH_SIZE]
                body: dict[str, Any] = {
                    "model": self.model,
                    "input": batch,
                }
                resp = await client.post(
                    f"{self.base_url}/embeddings",
                    json=body,
                    headers=headers,
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                sorted_data = sorted(data["data"], key=lambda d: d["index"])
                all_embeddings.extend(d["embedding"] for d in sorted_data)
        return all_embeddings
