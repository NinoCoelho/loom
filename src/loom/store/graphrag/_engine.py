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
import re
import time
from pathlib import Path
from typing import Any

from loom.store.db import SqliteResource
from loom.store.embeddings import _cosine_similarity
from loom.store.graph import EntityGraph
from loom.store.graphrag._chunking import chunk_markdown
from loom.store.graphrag._extraction import (
    _EXTRACTION_PROMPT,
    _GLEAN_PROMPT,
    parse_extraction_response,
)
from loom.store.graphrag._types import (
    Chunk,
    EnrichedRetrieval,
    GraphRAGConfig,
    HopRecord,
    RetrievalResult,
    RetrievalTrace,
)
from loom.store.vector import VectorStore

logger = logging.getLogger(__name__)

_NAME_MAX_LEN = 80
_NAME_REJECT_SUBSTRINGS = ("](", "://", "```")
_NAME_MERMAID_TOKENS = re.compile(r"\b(?:PK|FK|pk|fk)\b")
_NAME_HAS_LETTER = re.compile(r"[A-Za-zÀ-ÿ]")


def _sanitize_entity_name(raw: str) -> str | None:
    """Return a cleaned entity name, or ``None`` if it should be rejected.

    Rejects multi-line names, markdown table/link/URL fragments, mermaid
    field declarations, and over-long or letter-less strings. The LLM
    extractor is the trust boundary here: garbage that lands in the DB
    becomes orphan nodes and noisy edges in the knowledge graph.
    """
    if not raw:
        return None
    s = raw.strip()
    s = s.strip("*_`> -")
    if not s or len(s) > _NAME_MAX_LEN or len(s) < 2:
        return None
    if any(c in s for c in "\n\r\t|"):
        return None
    if any(sub in s for sub in _NAME_REJECT_SUBSTRINGS):
        return None
    if not _NAME_HAS_LETTER.search(s):
        return None
    if _NAME_MERMAID_TOKENS.search(s):
        return None
    return s


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
        self._embedder = embedding_provider
        self._llm = llm_provider
        db_dir.mkdir(parents=True, exist_ok=True)
        self._vector_store = VectorStore(
            db_dir / "graphrag_vectors.sqlite", dim=embedding_provider.dim
        )
        self._entity_graph = EntityGraph(db_dir / "graphrag_entities.sqlite")
        self._chunk_db = self._init_db(db_dir / "graphrag_chunks.sqlite")
        self._chunk_db.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                heading TEXT DEFAULT '',
                content TEXT NOT NULL,
                char_offset INTEGER DEFAULT 0,
                indexed_at REAL
            )
        """)
        self._chunk_db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_path)"
        )
        self._chunk_db.commit()

    def _close_db(self) -> None:
        self._vector_store.close()
        self._entity_graph.close()

    # Alias for backward compat
    def close(self) -> None:
        SqliteResource.close(self)

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



    def chunk_text(self, text: str, source_path: str) -> list[Chunk]:
        return chunk_markdown(
            text,
            source_path,
            max_size=self._config.chunk_size,
            overlap=self._config.chunk_overlap,
        )

    async def index_source(self, path: str, content: str) -> None:
        chunks = self.chunk_text(content, path)
        if not chunks:
            return

        old_ids = self._chunk_ids_for_source(path)
        if old_ids:
            self._entity_graph.remove_for_chunks(old_ids)
            for cid in old_ids:
                self._vector_store.remove(cid)
            placeholders = ",".join("?" for _ in old_ids)
            self._chunk_db.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", old_ids)
            self._chunk_db.commit()

        texts = [c.content for c in chunks]
        embeddings = await self._embedder.embed(texts)

        for chunk, emb in zip(chunks, embeddings):
            self._chunk_db.execute(
                "INSERT OR REPLACE INTO chunks "
                "(id, source_path, heading, content, char_offset, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk.id,
                    chunk.source_path,
                    chunk.heading,
                    chunk.content,
                    chunk.char_offset,
                    time.time(),
                ),
            )
            self._vector_store.upsert(
                chunk.id,
                emb,
                source=chunk.source_path,
                metadata={"heading": chunk.heading, "offset": chunk.char_offset},
            )
        self._chunk_db.commit()

        if self._llm is not None:
            await self._extract_entities(chunks)

    async def index_vault(self, vault: Any) -> None:
        paths = await vault.list()
        for p in paths:
            try:
                content = await vault.read(p)
                await self.index_source(p, content)
            except Exception:
                logger.warning("Failed to index vault file: %s", p, exc_info=True)

    def remove_source(self, source_path: str) -> None:
        old_ids = self._chunk_ids_for_source(source_path)
        if not old_ids:
            return
        self._entity_graph.remove_for_chunks(old_ids)
        for cid in old_ids:
            self._vector_store.remove(cid)
        placeholders = ",".join("?" for _ in old_ids)
        self._chunk_db.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", old_ids)
        self._chunk_db.commit()

    async def _extract_entities(self, chunks: list[Chunk]) -> None:
        from loom.types import ChatMessage, Role

        ontology = self._config.ontology
        entity_types = ", ".join(ontology.entity_types)
        core_relations = ", ".join(ontology.core_relations)

        for chunk in chunks:
            prompt = _EXTRACTION_PROMPT.format(
                entity_types=entity_types,
                core_relations=core_relations,
                text=chunk.content[:3000],
            )
            messages = [ChatMessage(role=Role.USER, content=prompt)]
            try:
                resp = await self._llm.chat(messages)
            except Exception:
                logger.warning("Entity extraction LLM call failed", exc_info=True)
                continue

            parsed = parse_extraction_response(resp.message.content or "")
            self._store_extraction(parsed, chunk.id)

            for _ in range(self._config.extraction.max_gleanings):
                glean_prompt = _GLEAN_PROMPT.format(text=chunk.content[:3000])
                glean_messages = [
                    ChatMessage(role=Role.USER, content=prompt),
                    resp.message,
                    ChatMessage(role=Role.USER, content=glean_prompt),
                ]
                try:
                    glean_resp = await self._llm.chat(glean_messages)
                except Exception:
                    break
                glean_parsed = parse_extraction_response(glean_resp.message.content or "")
                self._store_extraction(glean_parsed, chunk.id)

    def _store_extraction(self, parsed: dict[str, Any], chunk_id: str) -> None:
        aliases = self._config.ontology.aliases
        entity_name_to_id: dict[str, int] = {}

        for ent in parsed.get("entities", []):
            name = _sanitize_entity_name(ent.get("name", ""))
            etype = ent.get("type", "concept").strip().lower()
            if not name:
                continue
            if etype not in self._config.ontology.entity_types:
                etype = "concept"
            eid = self._entity_graph.resolve_entity(name, etype, aliases)
            entity_name_to_id[name.lower()] = eid
            if ent.get("description"):
                existing = self._entity_graph.get_entity(eid)
                if existing and not existing.description:
                    self._entity_graph.set_entity_description(eid, ent["description"][:500])
            self._entity_graph.add_mention(eid, chunk_id)

        for rel in parsed.get("relations", []):
            head = _sanitize_entity_name(rel.get("head", ""))
            tail = _sanitize_entity_name(rel.get("tail", ""))
            relation = rel.get("relation", "related_to").strip()
            desc = rel.get("description", "").strip()
            strength = float(rel.get("strength", 5))
            if not head or not tail:
                continue

            head_id = entity_name_to_id.get(head.lower())
            tail_id = entity_name_to_id.get(tail.lower())

            if head_id is None:
                head_id = self._entity_graph.resolve_entity(head, "concept", aliases)
                entity_name_to_id[head.lower()] = head_id
                self._entity_graph.add_mention(head_id, chunk_id)
            if tail_id is None:
                tail_id = self._entity_graph.resolve_entity(tail, "concept", aliases)
                entity_name_to_id[tail.lower()] = tail_id
                self._entity_graph.add_mention(tail_id, chunk_id)

            if not ontology_relation_ok(
                relation,
                self._config.ontology.core_relations,
                self._config.ontology.allow_custom_relations,
            ):
                relation = "related_to"

            self._entity_graph.add_triple(head_id, relation, tail_id, chunk_id, desc, strength)

    async def retrieve(
        self,
        query: str,
        *,
        top_k: int | None = None,
        max_hops: int | None = None,
    ) -> list[RetrievalResult]:
        enriched = await self.retrieve_enriched(query, top_k=top_k, max_hops=max_hops)
        return enriched.results

    async def retrieve_enriched(
        self,
        query: str,
        *,
        top_k: int | None = None,
        max_hops: int | None = None,
    ) -> EnrichedRetrieval:
        top_k = top_k or self._config.top_k
        max_hops = max_hops if max_hops is not None else self._config.max_hops

        trace = RetrievalTrace()
        results: list[RetrievalResult] = []

        query_embeds = await self._embedder.embed([query])
        if not query_embeds:
            return EnrichedRetrieval(results=results, trace=trace)
        query_vec = query_embeds[0]

        vector_hits = self._vector_store.search(query_vec, top_k=top_k * 2)

        seen_chunk_ids: set[str] = set()
        seed_entity_ids: set[int] = set()

        for hit in vector_hits:
            chunk_data = self._get_chunk(hit.id)
            if chunk_data is None:
                continue
            entities = self._entity_graph.entities_for_chunk(hit.id)
            entity_names = [e.name for e in entities]
            for e in entities:
                seed_entity_ids.add(e.id)
            seen_chunk_ids.add(hit.id)
            results.append(
                RetrievalResult(
                    chunk_id=hit.id,
                    source_path=hit.source,
                    heading=chunk_data.get("heading", ""),
                    content=chunk_data.get("content", ""),
                    score=hit.score,
                    source="vector",
                    related_entities=entity_names,
                )
            )

        trace.seed_entities = [
            e.name
            for e in (self._entity_graph.get_entity(eid) for eid in seed_entity_ids)
            if e is not None
        ]

        if max_hops > 0 and results:
            graph_ids: set[str] = set()
            expanded_ids: set[int] = set()
            for r in results[:5]:
                entities = self._entity_graph.entities_for_chunk(r.chunk_id)
                for ent in entities:
                    neighbors = self._entity_graph.neighbors(ent.id, max_hops=1)
                    for nb in neighbors:
                        expanded_ids.add(nb.id)
                        trace.hops.append(
                            HopRecord(
                                from_entity=ent.name,
                                to_entity=nb.name,
                                relation="",
                                hop_depth=1,
                            )
                        )
                        for cid in self._entity_graph.chunks_for_entity(nb.id):
                            if cid not in seen_chunk_ids:
                                graph_ids.add(cid)

            trace.expanded_entity_ids = list(expanded_ids)

            for cid in graph_ids:
                chunk_data = self._get_chunk(cid)
                if chunk_data is None:
                    continue
                seen_chunk_ids.add(cid)
                vec = self._vector_store.get(cid)
                score = 0.0
                if vec:
                    emb_vec = self._vector_store.get_embedding(cid)
                    if emb_vec:
                        score = _cosine_similarity(query_vec, emb_vec) * 0.7
                chunk_entities = self._entity_graph.entities_for_chunk(cid)
                results.append(
                    RetrievalResult(
                        chunk_id=cid,
                        source_path=chunk_data.get("source_path", ""),
                        heading=chunk_data.get("heading", ""),
                        content=chunk_data.get("content", ""),
                        score=score,
                        source="graph",
                        related_entities=[e.name for e in chunk_entities],
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        results = results[:top_k]

        subgraph_nodes: list[dict] = []
        subgraph_edges: list[dict] = []
        all_entity_ids = seed_entity_ids | set(trace.expanded_entity_ids)
        if all_entity_ids:
            for eid in all_entity_ids:
                ent = self._entity_graph.get_entity(eid)
                if ent:
                    degree = self._entity_graph.entity_degree(eid)
                    subgraph_nodes.append(
                        {
                            "id": ent.id,
                            "name": ent.name,
                            "type": ent.type,
                            "degree": degree,
                        }
                    )
            # Dedupe by (head, relation, tail) — same logical edge can
            # appear in multiple chunk-rows. Keep highest strength.
            edge_map: dict[tuple[int, str, int], dict] = {}
            for eid in all_entity_ids:
                for t in self._entity_graph.get_entity_triples(eid):
                    if t.head_id not in all_entity_ids or t.tail_id not in all_entity_ids:
                        continue
                    key = (t.head_id, t.relation, t.tail_id)
                    existing = edge_map.get(key)
                    if existing is None or t.strength > existing["strength"]:
                        edge_map[key] = {
                            "source": t.head_id,
                            "target": t.tail_id,
                            "relation": t.relation,
                            "strength": t.strength,
                        }
            subgraph_edges.extend(edge_map.values())

        return EnrichedRetrieval(
            results=results,
            trace=trace,
            subgraph_nodes=subgraph_nodes,
            subgraph_edges=subgraph_edges,
        )

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

    def _chunk_ids_for_source(self, source_path: str) -> list[str]:
        rows = self._chunk_db.execute(
            "SELECT id FROM chunks WHERE source_path = ?", (source_path,)
        ).fetchall()
        return [r[0] for r in rows]

    def _get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        row = self._chunk_db.execute(
            "SELECT source_path, heading, content, char_offset FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "source_path": row[0],
            "heading": row[1],
            "content": row[2],
            "char_offset": row[3],
        }


def ontology_relation_ok(relation: str, core_relations: list[str], allow_custom: bool) -> bool:
    if relation in core_relations:
        return True
    return allow_custom
