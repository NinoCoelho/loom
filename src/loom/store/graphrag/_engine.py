"""GraphRAG engine — chunking, entity extraction, hybrid retrieval, and
context injection.

This module orchestrates the other store components:

* :class:`~loom.store.vector.VectorStore` — embedding storage and
  cosine-similarity search.
* :class:`~loom.store.graph.EntityGraph` — entity-relationship graph with
  multi-hop traversal.
* :class:`~loom.store.embeddings` — concrete ``EmbeddingProvider``
  implementations.

Usage::

    from loom.store.graphrag import GraphRAGEngine, GraphRAGConfig
    from loom.store.embeddings import OllamaEmbeddingProvider

    embedder = OllamaEmbeddingProvider()
    engine = GraphRAGEngine(GraphRAGConfig(), embedder, db_dir=Path("~/.loom/graphrag"))
    await engine.index_source("notes/project.md", content)
    results = await engine.retrieve("how does auth work?")
    context = engine.format_context(results)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from loom.store.db import SqliteResource
from loom.store.graph import EntityGraph
from loom.store.graphrag._extraction import GraphRAGExtractor
from loom.store.graphrag._indexer import GraphRAGIndexer
from loom.store.graphrag._retriever import GraphRAGRetriever
from loom.store.graphrag._types import (
    EnrichedRetrieval,
    GraphRAGConfig,
    RetrievalResult,
)

logger = logging.getLogger(__name__)


class GraphRAGEngine(SqliteResource):
    """Core GraphRAG engine: chunking, indexing, entity extraction, retrieval."""

    def __init__(
        self,
        config: GraphRAGConfig,
        embedding_provider: Any,
        *,
        db_dir: Path,
        llm_provider: Any = None,
    ) -> None:
        self._config = config
        self._closed = False
        db_dir.mkdir(parents=True, exist_ok=True)
        self._entity_graph = EntityGraph(db_dir / "graphrag_entities.sqlite")
        self._indexer = GraphRAGIndexer(
            config,
            embedding_provider,
            db_dir=db_dir,
            entity_graph=self._entity_graph,
        )
        self._extractor = (
            GraphRAGExtractor(self._entity_graph, llm_provider, config)
            if llm_provider
            else None
        )
        self._retriever = GraphRAGRetriever(
            vector_store=self._indexer._vector_store,
            entity_graph=self._entity_graph,
            chunk_db=self._indexer._chunk_db,
            config=config,
            embedder=embedding_provider,
        )

    def _close_db(self) -> None:
        self._indexer._vector_store.close()
        self._entity_graph.close()

    def close(self) -> None:
        SqliteResource.close(self)

    @property
    def _vector_store(self):
        return self._indexer._vector_store

    @property
    def _chunk_db(self):
        return self._indexer._chunk_db

    def _chunk_ids_for_source(self, source_path: str) -> list[str]:
        return self._indexer._chunk_ids_for_source(source_path)

    def export_graph(self) -> dict[str, Any]:
        """Return a JSON-serialisable graph suitable for the UI knowledge view.

        Returns ``{"nodes": [...], "edges": [...], "enabled": True}`` where
        each node has ``id``, ``name``, ``type`` and each edge has
        ``source``, ``target``, ``relation``, ``strength``.
        """
        entities = self._entity_graph.list_all_entities()
        nodes = [{"id": e.id, "name": e.name, "type": e.type} for e in entities]
        triples = self._entity_graph.list_all_triples()
        edges = [
            {
                "source": t.head_id,
                "target": t.tail_id,
                "relation": t.relation,
                "strength": t.strength,
            }
            for t in triples
        ]
        return {"nodes": nodes, "edges": edges, "enabled": True}

    def chunk_text(self, text: str, source_path: str):
        return self._indexer.chunk_text(text, source_path)

    async def index_source(self, path: str, content: str) -> None:
        chunks = await self._indexer.index_source(path, content)
        if self._extractor:
            await self._extractor.extract(chunks)

    async def index_vault(self, vault: Any) -> None:
        paths = await vault.list()
        for p in paths:
            try:
                content = await vault.read(p)
                await self.index_source(p, content)
            except Exception:
                logger.warning("Failed to index vault file: %s", p, exc_info=True)

    def remove_source(self, source_path: str) -> None:
        self._indexer.remove_source(source_path)

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        max_hops: int | None = None,
    ) -> list[RetrievalResult]:
        return await self._retriever.retrieve(query, top_k=top_k, max_hops=max_hops)

    async def retrieve_enriched(
        self,
        query: str,
        *,
        top_k: int | None = None,
        max_hops: int | None = None,
    ) -> EnrichedRetrieval:
        return await self._retriever.retrieve_enriched(query, top_k=top_k, max_hops=max_hops)

    def format_context(
        self,
        results: list[RetrievalResult],
        budget: int | None = None,
    ) -> str:
        budget = budget or self._config.context_budget
        if not results:
            return ""

        parts: list[str] = []
        total = 0
        for r in results:
            entity_str = ""
            if r.related_entities:
                entity_str = f" (entities: {', '.join(r.related_entities[:5])})"
            source_str = f" [{r.source_path}"
            if r.heading:
                source_str += f" > {r.heading}"
            source_str += "]"
            block = f"---{source_str}{entity_str}\n{r.content}\n"
            if total + len(block) > budget:
                break
            parts.append(block)
            total += len(block)

        if not parts:
            return ""
        return "## Relevant Context\n\n" + "\n".join(parts)
