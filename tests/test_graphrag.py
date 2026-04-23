"""Tests for loom.store.graphrag — GraphRAG engine, chunking, retrieval."""

from __future__ import annotations

import json

import pytest

from loom.store.graphrag import (
    GraphRAGConfig,
    GraphRAGEngine,
    RetrievalResult,
    parse_extraction_response as _parse_extraction_response,
    chunk_markdown,
)


class FakeEmbedder:
    dim = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[len(t) * 0.01, 0.5, 0.3, 0.2] for t in texts]


class FakeLLM:
    async def chat(self, messages, **kwargs):
        from loom.types import ChatMessage, ChatResponse, Role, StopReason, Usage

        response_json = json.dumps({
            "entities": [
                {"name": "Nexus", "type": "project", "description": "A platform"},
                {"name": "FastAPI", "type": "technology", "description": "Python web framework"},
            ],
            "relations": [
                {
                    "head": "Nexus", "relation": "uses",
                    "tail": "FastAPI",
                    "description": "Built on FastAPI",
                    "strength": 9,
                },
            ],
        })
        return ChatResponse(
            message=ChatMessage(role=Role.ASSISTANT, content=response_json),
            usage=Usage(),
            stop_reason=StopReason.STOP,
            model="test",
        )


@pytest.fixture
def engine(tmp_path):
    cfg = GraphRAGConfig(chunk_size=500, top_k=5, context_budget=2000)
    e = GraphRAGEngine(
        cfg,
        FakeEmbedder(),
        db_dir=tmp_path / "graphrag",
        llm_provider=FakeLLM(),
    )
    yield e
    e.close()


@pytest.fixture
def engine_no_llm(tmp_path):
    cfg = GraphRAGConfig(chunk_size=500, top_k=5)
    e = GraphRAGEngine(
        cfg,
        FakeEmbedder(),
        db_dir=tmp_path / "graphrag",
    )
    yield e
    e.close()


class TestChunkMarkdown:
    def test_empty_text(self):
        assert chunk_markdown("", "test.md") == []

    def test_single_section(self):
        text = "This is some content without headings."
        chunks = chunk_markdown(text, "test.md")
        assert len(chunks) == 1
        assert chunks[0].content == text
        assert chunks[0].heading == ""

    def test_splits_on_headings(self):
        text = (
            "# Title\n\nIntro text\n\n"
            "## Section A\n\nContent A with enough text to stand alone.\n\n"
            "## Section B\n\nContent B with enough text to stand alone."
        )
        chunks = chunk_markdown(text, "test.md", max_size=100)
        assert len(chunks) >= 3
        headings = [c.heading for c in chunks if c.heading]
        assert "Title" in headings
        assert "Section A" in headings
        assert "Section B" in headings

    def test_merges_small_non_heading_sections(self):
        text = "# Big\n\n" + "x" * 200 + "\n\nSome small trailing text without a heading"
        chunks = chunk_markdown(text, "test.md", max_size=500)
        assert len(chunks) == 1

    def test_splits_large_sections(self):
        long_para = "word " * 200
        text = f"# Big\n\n{long_para}"
        chunks = chunk_markdown(text, "test.md", max_size=300)
        assert len(chunks) > 1

    def test_chunk_ids_deterministic(self):
        text = "## A\n\nContent A\n\n## B\n\nContent B"
        chunks1 = chunk_markdown(text, "test.md")
        chunks2 = chunk_markdown(text, "test.md")
        assert [c.id for c in chunks1] == [c.id for c in chunks2]

    def test_source_path_preserved(self):
        chunks = chunk_markdown("Hello", "notes/project.md")
        assert chunks[0].source_path == "notes/project.md"


class TestParseExtractionResponse:
    def test_clean_json(self):
        text = '{"entities": [{"name": "A", "type": "concept"}], "relations": []}'
        result = _parse_extraction_response(text)
        assert len(result["entities"]) == 1
        assert result["entities"][0]["name"] == "A"

    def test_json_in_fences(self):
        text = '```json\n{"entities": [], "relations": []}\n```'
        result = _parse_extraction_response(text)
        assert result == {"entities": [], "relations": []}

    def test_json_with_surrounding_text(self):
        text = 'Here are the results:\n{"entities": [], "relations": []}\nDone.'
        result = _parse_extraction_response(text)
        assert result == {"entities": [], "relations": []}

    def test_invalid_json(self):
        result = _parse_extraction_response("not json at all")
        assert result == {"entities": [], "relations": []}


class TestGraphRAGIndexing:
    async def test_index_source_creates_chunks(self, engine_no_llm):
        content = "# Project\n\nThis is a test project about AI."
        await engine_no_llm.index_source("test.md", content)
        ids = engine_no_llm._chunk_ids_for_source("test.md")
        assert len(ids) == 1

    async def test_index_source_stores_vectors(self, engine_no_llm):
        content = "# Project\n\nSome content here."
        await engine_no_llm.index_source("test.md", content)
        assert engine_no_llm._vector_store.count() == 1

    async def test_index_source_replaces_existing(self, engine_no_llm):
        await engine_no_llm.index_source("test.md", "# V1\n\nOld content")
        await engine_no_llm.index_source("test.md", "# V2\n\nNew content")
        assert engine_no_llm._vector_store.count() == 1

    async def test_index_source_with_entity_extraction(self, engine):
        content = "# Project\n\nNexus is built on FastAPI."
        await engine.index_source("test.md", content)
        assert engine._entity_graph.count_entities() == 2
        assert engine._entity_graph.count_triples() == 1

    async def test_index_empty_content(self, engine_no_llm):
        await engine_no_llm.index_source("empty.md", "")
        assert engine_no_llm._vector_store.count() == 0


class TestGraphRAGRetrieval:
    async def test_retrieve_from_indexed_content(self, engine_no_llm):
        content = "# Auth\n\nWe use OAuth2 for authentication with JWT tokens."
        await engine_no_llm.index_source("auth.md", content)

        results = await engine_no_llm.retrieve("authentication")
        assert len(results) >= 1
        assert results[0].source_path == "auth.md"

    async def test_retrieve_empty_index(self, engine_no_llm):
        results = await engine_no_llm.retrieve("anything")
        assert results == []

    async def test_retrieve_multiple_sources(self, engine_no_llm):
        await engine_no_llm.index_source("a.md", "# Auth\n\nAuthentication docs")
        await engine_no_llm.index_source("b.md", "# Database\n\nPostgreSQL setup guide")

        results = await engine_no_llm.retrieve("authentication")
        assert any(r.source_path == "a.md" for r in results)


class TestFormatContext:
    async def test_format_with_results(self, engine_no_llm):
        results = [
            RetrievalResult(
                chunk_id="c1",
                source_path="notes.md",
                heading="Ideas",
                content="This is relevant content about AI.",
                score=0.9,
                source="vector",
                related_entities=["AI", "ML"],
            ),
        ]
        context = engine_no_llm.format_context(results, budget=500)
        assert "## Relevant Context" in context
        assert "notes.md" in context
        assert "AI" in context

    async def test_format_empty_results(self, engine_no_llm):
        assert engine_no_llm.format_context([]) == ""

    async def test_format_respects_budget(self, engine_no_llm):
        results = [
            RetrievalResult(
                chunk_id=f"c{i}",
                source_path="big.md",
                heading="",
                content="x" * 200,
                score=0.9,
                source="vector",
            )
            for i in range(10)
        ]
        context = engine_no_llm.format_context(results, budget=300)
        assert len(context) <= 400


class TestGraphRAGEndToEnd:
    async def test_index_retrieve_format(self, engine):
        content = """# Project Nexus

Nexus is a self-evolving agent platform. It uses FastAPI for the backend
and React for the frontend. The agent loop is driven by Loom framework.
"""
        await engine.index_source("nexus.md", content)
        results = await engine.retrieve("what does Nexus use?")
        context = engine.format_context(results)

        assert "## Relevant Context" in context
        assert engine._entity_graph.count_entities() >= 1
