"""Tests for loom.store.vector — SQLite vector store."""

from __future__ import annotations

import pytest

from loom.store.vector import VectorStore, _pack_vector, _unpack_vector


@pytest.fixture
def store(tmp_path):
    s = VectorStore(tmp_path / "test_vectors.sqlite", dim=3)
    yield s
    s.close()


class TestPackUnpack:
    def test_roundtrip(self):
        vec = [1.0, 2.0, 3.0]
        blob = _pack_vector(vec)
        result = _unpack_vector(blob)
        assert len(result) == 3
        for a, b in zip(vec, result):
            assert abs(a - b) < 1e-6


class TestVectorStoreCRUD:
    async def test_upsert_and_count(self, store):
        vec = [0.1, 0.2, 0.3]
        store.upsert("c1", vec, source="file1.md")
        assert store.count() == 1

    async def test_upsert_overwrites(self, store):
        store.upsert("c1", [0.1, 0.2, 0.3], source="file1.md")
        store.upsert("c1", [0.4, 0.5, 0.6], source="file1.md")
        assert store.count() == 1

    async def test_remove(self, store):
        store.upsert("c1", [0.1, 0.2, 0.3])
        store.remove("c1")
        assert store.count() == 0

    async def test_remove_nonexistent(self, store):
        store.remove("nope")
        assert store.count() == 0

    async def test_remove_for_source(self, store):
        store.upsert("c1", [0.1, 0.2, 0.3], source="a.md")
        store.upsert("c2", [0.4, 0.5, 0.6], source="a.md")
        store.upsert("c3", [0.7, 0.8, 0.9], source="b.md")
        deleted = store.remove_for_source("a.md")
        assert deleted == 2
        assert store.count() == 1

    async def test_get_existing(self, store):
        store.upsert("c1", [0.1, 0.2, 0.3], source="file.md", metadata={"heading": "Intro"})
        hit = store.get("c1")
        assert hit is not None
        assert hit.source == "file.md"
        assert hit.metadata["heading"] == "Intro"

    async def test_get_nonexistent(self, store):
        assert store.get("nope") is None

    async def test_get_embedding(self, store):
        vec = [0.1, 0.2, 0.3]
        store.upsert("c1", vec, source="file.md")
        result = store.get_embedding("c1")
        assert result is not None
        for a, b in zip(vec, result):
            assert abs(a - b) < 1e-6

    async def test_get_embedding_nonexistent(self, store):
        assert store.get_embedding("nope") is None

    async def test_sources(self, store):
        store.upsert("c1", [0.1, 0.2, 0.3], source="a.md")
        store.upsert("c2", [0.4, 0.5, 0.6], source="b.md")
        assert store.sources() == ["a.md", "b.md"]


class TestVectorStoreSearch:
    async def test_search_empty_store(self, store):
        results = store.search([0.1, 0.2, 0.3])
        assert results == []

    async def test_search_finds_most_similar(self, store):
        store.upsert("c1", [1.0, 0.0, 0.0], source="a.md")
        store.upsert("c2", [0.0, 1.0, 0.0], source="b.md")
        store.upsert("c3", [0.9, 0.1, 0.0], source="c.md")

        results = store.search([1.0, 0.0, 0.0], top_k=2)
        assert len(results) == 2
        assert results[0].id == "c1"
        assert results[0].score > 0.99
        assert results[1].id == "c3"
        assert results[1].score > 0.9

    async def test_search_source_filter(self, store):
        store.upsert("c1", [1.0, 0.0, 0.0], source="a.md")
        store.upsert("c2", [0.0, 1.0, 0.0], source="b.md")

        results = store.search([1.0, 0.0, 0.0], source_filter="b.md")
        assert len(results) == 1
        assert results[0].id == "c2"

    async def test_search_top_k_limits(self, store):
        for i in range(10):
            vec = [float(i) / 10, 0.0, 0.0]
            store.upsert(f"c{i}", vec, source="f.md")

        results = store.search([1.0, 0.0, 0.0], top_k=3)
        assert len(results) == 3


class TestVectorStorePersistence:
    async def test_persists_across_instances(self, tmp_path):
        db_path = tmp_path / "persist.sqlite"
        s1 = VectorStore(db_path, dim=3)
        s1.upsert("c1", [0.1, 0.2, 0.3], source="test.md")
        s1.close()

        s2 = VectorStore(db_path, dim=3)
        assert s2.count() == 1
        hit = s2.get("c1")
        assert hit is not None
        assert hit.source == "test.md"
        s2.close()
