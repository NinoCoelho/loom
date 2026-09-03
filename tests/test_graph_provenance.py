"""Tests for bitemporal-lite provenance, conflict detection, and merges."""

from __future__ import annotations

import sqlite3

import pytest

from loom.store.graph import EntityGraph
from loom.store.graphrag._extraction import parse_temporal


@pytest.fixture
def graph(tmp_path):
    g = EntityGraph(tmp_path / "test_graph.sqlite")
    yield g
    g.close()


def _fact(graph, head, relation, tail, chunk, source, **kw):
    head_id = graph.resolve_entity(head, "technology")
    tail_id = graph.resolve_entity(tail, "technology")
    return graph.add_triple(
        head_id, relation, tail_id, chunk, source_path=source, **kw
    )


class TestProvenanceSchema:
    async def test_add_triple_returns_id_and_fields(self, graph):
        new_id, conflict_id = _fact(
            graph, "Nexus", "uses", "FastAPI", "c1", "notes/nexus.md",
            valid_from="2025-01", valid_to="2026-03",
        )
        assert new_id is not None and conflict_id is None
        t = graph.get_triple(new_id)
        assert t.source_path == "notes/nexus.md"
        assert t.valid_from == "2025-01"
        assert t.valid_to == "2026-03"
        assert t.asserted_at is not None
        assert t.status == "active"

    async def test_duplicate_ignored(self, graph):
        id1, _ = _fact(graph, "Nexus", "uses", "FastAPI", "c1", "notes/a.md")
        id2, _ = _fact(graph, "Nexus", "uses", "FastAPI", "c1", "notes/a.md")
        assert id1 is not None and id2 is None

    async def test_migration_from_legacy_schema(self, tmp_path):
        db_path = tmp_path / "legacy.sqlite"
        db = sqlite3.connect(str(db_path))
        db.executescript(
            """
            CREATE TABLE entities (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT,
                canonical TEXT NOT NULL, description TEXT DEFAULT '',
                UNIQUE(canonical, type)
            );
            CREATE TABLE triples (
                id INTEGER PRIMARY KEY,
                head_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                relation TEXT NOT NULL,
                tail_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                chunk_id TEXT NOT NULL, description TEXT DEFAULT '',
                strength REAL DEFAULT 5.0,
                UNIQUE(head_id, relation, tail_id, chunk_id)
            );
            INSERT INTO entities (name, type, canonical) VALUES ('Old', 'concept', 'old');
            INSERT INTO triples (head_id, relation, tail_id, chunk_id)
                VALUES (1, 'related_to', 1, 'c1');
            """
        )
        db.commit()
        db.close()

        g = EntityGraph(db_path)
        try:
            triple = g.get_triple(1)
            assert triple is not None
            assert triple.status == "active"
            assert triple.source_path == ""
        finally:
            g.close()


class TestConflictDetection:
    async def test_contradiction_from_other_file_flags_conflict(self, graph):
        _fact(graph, "Nexus", "uses", "FastAPI", "c1", "notes/a.md")
        new_id, conflict_id = _fact(graph, "Nexus", "uses", "Flask", "c2", "notes/b.md")
        assert conflict_id is not None
        t = graph.get_triple(new_id)
        assert t.status == "pending"
        conflicts = graph.list_conflicts()
        assert len(conflicts) == 1
        assert conflicts[0]["old_tail"] == "FastAPI"
        assert conflicts[0]["new_tail"] == "Flask"

    async def test_same_source_no_conflict(self, graph):
        _fact(graph, "Nexus", "uses", "FastAPI", "c1", "notes/a.md")
        _, conflict_id = _fact(graph, "Nexus", "uses", "Flask", "c2", "notes/a.md")
        assert conflict_id is None

    async def test_disjoint_validity_no_conflict(self, graph):
        _fact(
            graph, "Nexus", "uses", "FastAPI", "c1", "notes/a.md",
            valid_from="2024", valid_to="2024-12",
        )
        _, conflict_id = _fact(
            graph, "Nexus", "uses", "Flask", "c2", "notes/b.md",
            valid_from="2025-01",
        )
        assert conflict_id is None

    async def test_resolve_approve_new_supersedes_old(self, graph):
        old_id, _ = _fact(graph, "Nexus", "uses", "FastAPI", "c1", "notes/a.md")
        new_id, conflict_id = _fact(graph, "Nexus", "uses", "Flask", "c2", "notes/b.md")
        assert graph.resolve_conflict(conflict_id, "approve_new")
        old = graph.get_triple(old_id)
        new = graph.get_triple(new_id)
        assert old.status == "superseded"
        assert old.superseded_by == new_id
        assert new.status == "active"
        assert graph.list_conflicts() == []
        assert len(graph.list_conflicts(resolved=True)) == 1

    async def test_resolve_reject_new(self, graph):
        _fact(graph, "Nexus", "uses", "FastAPI", "c1", "notes/a.md")
        new_id, conflict_id = _fact(graph, "Nexus", "uses", "Flask", "c2", "notes/b.md")
        assert graph.resolve_conflict(conflict_id, "reject_new")
        assert graph.get_triple(new_id).status == "rejected"

    async def test_pending_triple_hidden_from_traversal(self, graph):
        eid = graph.resolve_entity("Nexus", "technology")
        _fact(graph, "Nexus", "uses", "FastAPI", "c1", "notes/a.md")
        _fact(graph, "Nexus", "uses", "Flask", "c2", "notes/b.md")
        tails = {t.tail_id for t in graph.get_entity_triples(eid)}
        assert len(tails) == 1

    async def test_remove_for_chunks_cleans_conflicts(self, graph):
        _fact(graph, "Nexus", "uses", "FastAPI", "c1", "notes/a.md")
        _, conflict_id = _fact(graph, "Nexus", "uses", "Flask", "c2", "notes/b.md")
        graph.remove_for_chunks(["c2"])
        assert graph.list_conflicts() == []

    async def test_invalid_resolution_rejected(self, graph):
        _, conflict_id = _fact(graph, "Nexus", "uses", "FastAPI", "c1", "notes/a.md")
        _fact(graph, "Nexus", "uses", "Flask", "c2", "notes/b.md")
        assert graph.resolve_conflict(conflict_id, "nonsense") is False


class TestEntityMerges:
    async def test_merge_and_unmerge_roundtrip(self, graph):
        a = graph.resolve_entity("PostgreSQL", "technology")
        b = graph.resolve_entity("Postgres", "technology")
        c = graph.resolve_entity("Nexus", "project")
        graph.add_triple(c, "uses", b, "c1", source_path="notes/a.md")
        graph.add_mention(b, "c1")

        merge_id = graph.merge_entities(a, b)
        assert merge_id is not None
        assert graph.get_entity(b) is None
        tails = {t.tail_id for t in graph.get_entity_triples(c)}
        assert tails == {a}

        assert graph.unmerge(merge_id)
        restored = graph.get_entity(b)
        assert restored is not None and restored.name == "Postgres"
        tails = {t.tail_id for t in graph.get_entity_triples(c)}
        assert tails == {b}
        merges = graph.list_merges()
        assert merges == []

    async def test_merge_snapshot_survives_duplicate_facts(self, graph):
        a = graph.resolve_entity("PostgreSQL", "technology")
        b = graph.resolve_entity("Postgres", "technology")
        graph.add_triple(a, "related_to", b, "c1", source_path="notes/a.md")
        merge_id = graph.merge_entities(a, b)
        assert merge_id is not None
        assert graph.unmerge(merge_id)
        assert graph.get_entity(b) is not None


class TestTemporalParsing:
    def test_year(self):
        assert parse_temporal("2024") == "2024"

    def test_year_month(self):
        assert parse_temporal("2024-03") == "2024-03"

    def test_full_date_truncates_time(self):
        assert parse_temporal("2024-03-15T10:00:00Z") == "2024-03-15"

    def test_garbage_dropped(self):
        assert parse_temporal("since forever") is None
        assert parse_temporal(None) is None
        assert parse_temporal("") is None


class _MapEmbedder:
    """Deterministic embedder — same text → same vector; aliases can share one."""

    dim = 8

    def __init__(self, aliases: dict[str, str] | None = None) -> None:
        self._aliases = {k.lower(): v.lower() for k, v in (aliases or {}).items()}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib

        out = []
        for t in texts:
            key = self._aliases.get(t.strip().lower(), t.strip().lower())
            h = hashlib.sha256(key.encode()).digest()
            vec = [(b / 255.0) * 2 - 1 for b in h[:8]]
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out


class _ScriptedLLM:
    """Returns a fixed extraction payload, then gleaning misses (empty)."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls = 0

    async def chat(self, messages, **kwargs):
        import json

        from loom.types import ChatMessage, ChatResponse, Role, StopReason, Usage

        self.calls += 1
        empty = '{"entities": [], "relations": []}'
        content = json.dumps(self._payload) if self.calls == 1 else empty
        return ChatResponse(
            message=ChatMessage(role=Role.ASSISTANT, content=content),
            usage=Usage(),
            stop_reason=StopReason.STOP,
            model="test",
        )


def _fact_payload(tail: str, valid_from: str | None = None) -> dict:
    rel = {
        "head": "Nexus", "relation": "uses", "tail": tail,
        "description": f"uses {tail}", "strength": 8,
    }
    if valid_from:
        rel["valid_from"] = valid_from
    return {
        "entities": [
            {"name": "Nexus", "type": "project", "description": "platform"},
            {"name": tail, "type": "technology", "description": tail},
        ],
        "relations": [rel],
    }


class TestEngineConflictFlow:
    async def test_contradiction_across_files_lands_in_review(self, tmp_path):
        from loom.store.graphrag import GraphRAGConfig, GraphRAGEngine

        db_dir = tmp_path / "graphrag"
        cfg = GraphRAGConfig(chunk_size=500)
        cfg.extraction.max_gleanings = 0

        engine1 = GraphRAGEngine(
            cfg, _MapEmbedder(), db_dir=db_dir,
            llm_provider=_ScriptedLLM(_fact_payload("FastAPI", "2025-01")),
        )
        await engine1.index_source("notes/a.md", "# A\nNexus uses FastAPI.")
        engine1.close()

        engine2 = GraphRAGEngine(
            cfg, _MapEmbedder(), db_dir=db_dir,
            llm_provider=_ScriptedLLM(_fact_payload("Flask")),
        )
        await engine2.index_source("notes/b.md", "# B\nNexus uses Flask now.")

        conflicts = engine2.list_conflicts()
        assert len(conflicts) == 1
        assert conflicts[0]["old_tail"] == "FastAPI"
        assert conflicts[0]["new_tail"] == "Flask"
        assert conflicts[0]["triple_a"]["valid_from"] == "2025-01"
        assert conflicts[0]["triple_b"]["source_path"] == "notes/b.md"

        assert engine2.resolve_conflict(conflicts[0]["id"], "approve_new")
        assert engine2.list_conflicts() == []
        old = conflicts[0]["triple_a"]["id"]
        assert engine2.get_triple(old).status == "superseded"

        edges = engine2.export_graph()["edges"]
        assert any(e["source_path"] == "notes/b.md" for e in edges)
        engine2.close()

    async def test_resolver_merges_alias_entities(self, tmp_path):
        from loom.store.graphrag import GraphRAGConfig, GraphRAGEngine

        embedder = _MapEmbedder(aliases={"pg": "postgresql"})
        cfg = GraphRAGConfig(chunk_size=500)
        cfg.extraction.max_gleanings = 0
        engine = GraphRAGEngine(
            cfg, embedder, db_dir=tmp_path / "graphrag",
            llm_provider=_ScriptedLLM({
                "entities": [{"name": "PostgreSQL", "type": "technology", "description": "db"}],
                "relations": [],
            }),
        )
        await engine.index_source("notes/pg1.md", "# PG\nPostgreSQL described.")
        engine.close()

        engine2 = GraphRAGEngine(
            cfg, embedder, db_dir=tmp_path / "graphrag",
            llm_provider=_ScriptedLLM({
                "entities": [{"name": "PG", "type": "technology", "description": "db alias"}],
                "relations": [],
            }),
        )
        await engine2.index_source("notes/pg2.md", "# PG2\nPG mentioned.")
        ents = {e.name for e in engine2._entity_graph.list_all_entities()}
        assert ents == {"PostgreSQL"}
        engine2.close()
