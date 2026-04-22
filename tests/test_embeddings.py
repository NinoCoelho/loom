"""Tests for loom.store.embeddings — embedding provider implementations."""

from __future__ import annotations

import json

import httpx


def _mock_ollama_response(embeddings: list[list[float]]) -> httpx.Response:
    body = json.dumps({"embeddings": embeddings})
    return httpx.Response(200, request=httpx.Request("POST", "http://test/api/embed"), text=body)


def _mock_openai_response(embeddings: list[list[float]]) -> httpx.Response:
    data = [
        {"object": "embedding", "index": i, "embedding": emb}
        for i, emb in enumerate(embeddings)
    ]
    body = json.dumps({
        "object": "list", "data": data, "model": "test",
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    })
    return httpx.Response(
        200, request=httpx.Request("POST", "http://test/v1/embeddings"),
        text=body,
    )


class TestOllamaEmbeddingProvider:
    async def test_embed_single_text(self, tmp_path):
        from loom.store.embeddings import OllamaEmbeddingProvider

        provider = OllamaEmbeddingProvider(model="test-model", dim=3)

        emb = [0.1, 0.2, 0.3]
        provider._timeout = 5.0

        class FakeClient:
            def __init__(self, **kwargs):
                self._transport = kwargs.get("transport")

            async def post(self, url, **kwargs):
                return _mock_ollama_response([emb])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        import loom.store.embeddings as emb_mod

        orig = emb_mod.httpx.AsyncClient
        emb_mod.httpx.AsyncClient = lambda **kw: FakeClient(**kw)
        try:
            result = await provider.embed(["hello world"])
        finally:
            emb_mod.httpx.AsyncClient = orig

        assert result == [emb]

    async def test_embed_empty_list(self, tmp_path):
        from loom.store.embeddings import OllamaEmbeddingProvider

        provider = OllamaEmbeddingProvider(dim=3)
        result = await provider.embed([])
        assert result == []

    async def test_embed_batches(self, tmp_path):
        from loom.store.embeddings import OllamaEmbeddingProvider

        provider = OllamaEmbeddingProvider(dim=2)
        call_count = 0

        class FakeClient:
            async def post(self, url, **kwargs):
                nonlocal call_count
                call_count += 1
                payload = kwargs.get("json", {})
                n = len(payload.get("input", []))
                embs = [[float(i), float(i + 1)] for i in range(n)]
                return _mock_ollama_response(embs)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        import loom.store.embeddings as emb_mod

        orig = emb_mod.httpx.AsyncClient
        emb_mod.httpx.AsyncClient = lambda **kw: FakeClient()
        try:
            texts = ["t" + str(i) for i in range(70)]
            result = await provider.embed(texts)
        finally:
            emb_mod.httpx.AsyncClient = orig

        assert call_count == 2
        assert len(result) == 70


class TestOpenAIEmbeddingProvider:
    async def test_embed_with_api_key(self, tmp_path, monkeypatch):
        from loom.store.embeddings import OpenAIEmbeddingProvider

        monkeypatch.setenv("TEST_KEY", "sk-test-123")
        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small", key_env="TEST_KEY", dim=3,
        )

        emb = [0.4, 0.5, 0.6]

        class FakeClient:
            def __init__(self, **kwargs):
                self.last_headers = None

            async def post(self, url, **kwargs):
                self.last_headers = kwargs.get("headers", {})
                return _mock_openai_response([emb])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        import loom.store.embeddings as emb_mod

        fc = FakeClient()
        orig = emb_mod.httpx.AsyncClient
        emb_mod.httpx.AsyncClient = lambda **kw: fc
        try:
            result = await provider.embed(["test"])
        finally:
            emb_mod.httpx.AsyncClient = orig

        assert result == [emb]
        assert fc.last_headers["Authorization"] == "Bearer sk-test-123"

    async def test_embed_no_key_env(self, tmp_path):
        from loom.store.embeddings import OpenAIEmbeddingProvider

        provider = OpenAIEmbeddingProvider(key_env="", dim=3)

        class FakeClient:
            def __init__(self, **kwargs):
                self.last_headers = None

            async def post(self, url, **kwargs):
                self.last_headers = kwargs.get("headers", {})
                return _mock_openai_response([[0.0, 0.0, 0.0]])

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        import loom.store.embeddings as emb_mod

        fc = FakeClient()
        orig = emb_mod.httpx.AsyncClient
        emb_mod.httpx.AsyncClient = lambda **kw: fc
        try:
            await provider.embed(["test"])
        finally:
            emb_mod.httpx.AsyncClient = orig

        assert "Authorization" not in fc.last_headers


class TestCosineSimilarity:
    def test_identical_vectors(self):
        from loom.store.embeddings import _cosine_similarity

        v = [1.0, 0.0, 0.0]
        assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        from loom.store.embeddings import _cosine_similarity

        assert abs(_cosine_similarity([1.0, 0.0], [0.0, 1.0])) < 1e-6

    def test_zero_vector(self):
        from loom.store.embeddings import _cosine_similarity

        assert _cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0
