"""Tests for loom.store.graph — entity-relationship graph."""

from __future__ import annotations

import pytest

from loom.store.graph import EntityGraph


@pytest.fixture
def graph(tmp_path):
    g = EntityGraph(tmp_path / "test_graph.sqlite")
    yield g
    g.close()


class TestEntityCRUD:
    async def test_resolve_creates_new(self, graph):
        eid = graph.resolve_entity("PostgreSQL", "technology")
        assert eid > 0
        ent = graph.get_entity(eid)
        assert ent is not None
        assert ent.name == "PostgreSQL"
        assert ent.type == "technology"
        assert ent.canonical == "postgresql"

    async def test_resolve_returns_existing(self, graph):
        eid1 = graph.resolve_entity("PostgreSQL", "technology")
        eid2 = graph.resolve_entity("PostgreSQL", "technology")
        assert eid1 == eid2

    async def test_resolve_case_insensitive(self, graph):
        eid1 = graph.resolve_entity("PostgreSQL", "technology")
        eid2 = graph.resolve_entity("postgresql", "technology")
        assert eid1 == eid2

    async def test_find_entity(self, graph):
        eid = graph.resolve_entity("Nexus", "project")
        found = graph.find_entity("Nexus", "project")
        assert found is not None
        assert found.id == eid

    async def test_find_entity_missing(self, graph):
        assert graph.find_entity("Nope", "concept") is None

    async def test_get_entity(self, graph):
        eid = graph.resolve_entity("Test", "concept")
        ent = graph.get_entity(eid)
        assert ent is not None
        assert ent.name == "Test"

    async def test_get_entity_missing(self, graph):
        assert graph.get_entity(999) is None


class TestEntityAliases:
    async def test_alias_resolution(self, graph):
        aliases = {"PostgreSQL": ["Postgres", "postgres", "PG"]}
        eid = graph.resolve_entity("PostgreSQL", "technology")
        alias_eid = graph.resolve_entity("postgres", "technology", aliases=aliases)
        assert alias_eid == eid

    async def test_alias_unknown_still_creates(self, graph):
        aliases = {"Python": ["py", "python3"]}
        eid = graph.resolve_entity("Ruby", "technology", aliases=aliases)
        assert eid > 0
        assert graph.count_entities() == 1


class TestTriples:
    async def test_add_triple(self, graph):
        h = graph.resolve_entity("Nexus", "project")
        t = graph.resolve_entity("FastAPI", "technology")
        graph.add_triple(h, "uses", t, "chunk_1", "Nexus is built on FastAPI", 8)
        assert graph.count_triples() == 1

    async def test_triple_dedup(self, graph):
        h = graph.resolve_entity("A", "concept")
        t = graph.resolve_entity("B", "concept")
        graph.add_triple(h, "related_to", t, "c1")
        graph.add_triple(h, "related_to", t, "c1")
        assert graph.count_triples() == 1

    async def test_different_chunks_not_deduped(self, graph):
        h = graph.resolve_entity("A", "concept")
        t = graph.resolve_entity("B", "concept")
        graph.add_triple(h, "related_to", t, "c1")
        graph.add_triple(h, "related_to", t, "c2")
        assert graph.count_triples() == 2


class TestMentions:
    async def test_add_mention(self, graph):
        eid = graph.resolve_entity("Nexus", "project")
        graph.add_mention(eid, "chunk_1")
        chunks = graph.chunks_for_entity(eid)
        assert "chunk_1" in chunks

    async def test_entities_for_chunk(self, graph):
        e1 = graph.resolve_entity("Nexus", "project")
        e2 = graph.resolve_entity("FastAPI", "technology")
        graph.add_mention(e1, "chunk_1")
        graph.add_mention(e2, "chunk_1")
        entities = graph.entities_for_chunk("chunk_1")
        names = {e.name for e in entities}
        assert names == {"Nexus", "FastAPI"}


class TestNeighborTraversal:
    async def test_one_hop(self, graph):
        a = graph.resolve_entity("A", "concept")
        b = graph.resolve_entity("B", "concept")
        c = graph.resolve_entity("C", "concept")
        graph.add_triple(a, "related_to", b, "c1")
        graph.add_triple(b, "related_to", c, "c2")

        neighbors = graph.neighbors(a, max_hops=1)
        names = {e.name for e in neighbors}
        assert names == {"B"}

    async def test_two_hops(self, graph):
        a = graph.resolve_entity("A", "concept")
        b = graph.resolve_entity("B", "concept")
        c = graph.resolve_entity("C", "concept")
        graph.add_triple(a, "related_to", b, "c1")
        graph.add_triple(b, "related_to", c, "c2")

        neighbors = graph.neighbors(a, max_hops=2)
        names = {e.name for e in neighbors}
        assert names == {"B", "C"}

    async def test_no_neighbors(self, graph):
        a = graph.resolve_entity("Isolated", "concept")
        assert graph.neighbors(a, max_hops=2) == []

    async def test_bidirectional(self, graph):
        a = graph.resolve_entity("A", "concept")
        b = graph.resolve_entity("B", "concept")
        graph.add_triple(a, "uses", b, "c1")

        from_a = graph.neighbors(a, max_hops=1)
        from_b = graph.neighbors(b, max_hops=1)
        assert {e.name for e in from_a} == {"B"}
        assert {e.name for e in from_b} == {"A"}


class TestRemoveForChunks:
    async def test_removes_triples_and_mentions(self, graph):
        a = graph.resolve_entity("A", "concept")
        b = graph.resolve_entity("B", "concept")
        graph.add_triple(a, "uses", b, "chunk_1")
        graph.add_mention(a, "chunk_1")
        graph.add_mention(b, "chunk_1")

        graph.remove_for_chunks(["chunk_1"])
        assert graph.count_triples() == 0
        assert graph.chunks_for_entity(a) == []
        assert graph.chunks_for_entity(b) == []

    async def test_orphan_entities_cleaned(self, graph):
        a = graph.resolve_entity("A", "concept")
        b = graph.resolve_entity("B", "concept")
        graph.add_mention(a, "c1")
        graph.add_triple(a, "uses", b, "c1")

        graph.remove_for_chunks(["c1"])
        assert graph.count_entities() == 0


class TestEntityGraphPersistence:
    async def test_persists_across_instances(self, tmp_path):
        db = tmp_path / "persist.sqlite"
        g1 = EntityGraph(db)
        eid = g1.resolve_entity("Test", "concept")
        g1.add_mention(eid, "c1")
        g1.close()

        g2 = EntityGraph(db)
        assert g2.count_entities() == 1
        ent = g2.get_entity(eid)
        assert ent is not None
        assert ent.name == "Test"
        g2.close()
