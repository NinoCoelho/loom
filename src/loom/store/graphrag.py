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

import hashlib
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loom.store.embeddings import _cosine_similarity
from loom.store.graph import EntityGraph
from loom.store.vector import VectorStore

logger = logging.getLogger(__name__)

_DEFAULT_ENTITY_TYPES = [
    "person",
    "project",
    "concept",
    "technology",
    "decision",
    "resource",
]

_DEFAULT_CORE_RELATIONS = [
    "uses",
    "depends_on",
    "part_of",
    "created_by",
    "related_to",
]

_EXTRACTION_PROMPT = """\
Extract entities and relationships from the following text.

Entity types to look for: {entity_types}

For each entity provide:
- name: the canonical name, capitalized
- type: one of the entity types above
- description: brief description based on context

For relationships use one of these relation types when they fit: {core_relations}
Otherwise use the relation type that best describes the relationship and set "custom" to true.

For each relationship provide:
- head: name of the source entity
- relation: the relation type
- tail: name of the target entity
- description: natural language description of the relationship
- strength: integer 1-10 indicating relationship strength

Text:
{text}

Respond with ONLY valid JSON in this exact format (no markdown fences):
{{"entities": [{{"name": "...", "type": "...", "description": "..."}}],
 "relations": [{{"head": "...", "relation": "...", "tail": "..."
                 , "description": "...", "strength": 5, "custom": false}}]}}\
"""

_GLEAN_PROMPT = """\
Many entities and relationships were missed in the previous extraction.
Review the text again and extract any additional entities and relationships
that were missed. Use the same JSON format.

Text:
{text}

Respond with ONLY valid JSON:\
"""


@dataclass
class OntologyConfig:
    entity_types: list[str] = field(default_factory=lambda: list(_DEFAULT_ENTITY_TYPES))
    core_relations: list[str] = field(
        default_factory=lambda: list(_DEFAULT_CORE_RELATIONS)
    )
    allow_custom_relations: bool = True
    aliases: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class EmbeddingConfig:
    provider: str = "ollama"
    model: str = "nomic-embed-text"
    base_url: str = "http://localhost:11434"
    key_env: str = ""
    dimensions: int = 768


@dataclass
class ExtractionConfig:
    model: str | None = None
    max_gleanings: int = 1


@dataclass
class GraphRAGConfig:
    enabled: bool = False
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    ontology: OntologyConfig = field(default_factory=OntologyConfig)
    max_hops: int = 2
    context_budget: int = 3000
    top_k: int = 10
    chunk_size: int = 1000
    chunk_overlap: int = 100


@dataclass
class Chunk:
    id: str
    source_path: str
    heading: str
    content: str
    char_offset: int


@dataclass
class RetrievalResult:
    chunk_id: str
    source_path: str
    heading: str
    content: str
    score: float
    source: str
    related_entities: list[str] = field(default_factory=list)


@dataclass
class HopRecord:
    from_entity: str
    to_entity: str
    relation: str
    hop_depth: int


@dataclass
class RetrievalTrace:
    seed_entities: list[str] = field(default_factory=list)
    hops: list[HopRecord] = field(default_factory=list)
    expanded_entity_ids: list[int] = field(default_factory=list)


@dataclass
class EnrichedRetrieval:
    results: list[RetrievalResult] = field(default_factory=list)
    trace: RetrievalTrace = field(default_factory=RetrievalTrace)
    subgraph_nodes: list[dict] = field(default_factory=list)
    subgraph_edges: list[dict] = field(default_factory=list)


def _make_chunk_id(source_path: str, offset: int) -> str:
    raw = f"{source_path}:{offset}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def chunk_markdown(
    text: str,
    source_path: str,
    *,
    max_size: int = 1000,
    overlap: int = 100,
) -> list[Chunk]:
    if not text.strip():
        return []
    heading_re = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    splits: list[tuple[str, int]] = []
    last = 0
    for m in heading_re.finditer(text):
        if m.start() > last:
            splits.append((text[last : m.start()], last))
        last = m.start()
    if last < len(text):
        splits.append((text[last:], last))

    if not splits:
        splits = [(text, 0)]

    merged: list[tuple[str, int]] = []
    for content, offset in splits:
        starts_with_heading = bool(re.match(r"^#{1,6}\s+", content))
        if merged and len(content) < 100 and not starts_with_heading:
            prev_content, prev_offset = merged[-1]
            merged[-1] = (prev_content + "\n" + content, prev_offset)
        elif len(content) > max_size:
            paragraphs = re.split(r"\n\n+", content)
            buf = ""
            buf_offset = offset
            for para in paragraphs:
                if buf and len(buf) + len(para) + 2 > max_size:
                    merged.append((buf, buf_offset))
                    buf_offset = max(offset, buf_offset + len(buf) - overlap)
                    buf = para
                else:
                    buf = buf + "\n\n" + para if buf else para
            if buf:
                merged.append((buf, buf_offset))
        else:
            merged.append((content, offset))

    chunks: list[Chunk] = []
    for content, offset in merged:
        content = content.strip()
        if not content:
            continue
        heading_match = re.match(r"^#{1,6}\s+(.+)$", content, re.MULTILINE)
        heading = heading_match.group(1).strip() if heading_match else ""
        chunks.append(
            Chunk(
                id=_make_chunk_id(source_path, offset),
                source_path=source_path,
                heading=heading,
                content=content,
                char_offset=offset,
            )
        )
    return chunks


def _parse_extraction_response(text: str) -> dict[str, Any]:
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
    return {"entities": [], "relations": []}


class GraphRAGEngine:
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
        self._chunk_db = sqlite3.connect(
            str(db_dir / "graphrag_chunks.sqlite"), check_same_thread=False
        )
        self._chunk_db.execute("PRAGMA journal_mode=WAL")
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

    def close(self) -> None:
        self._vector_store.close()
        self._entity_graph.close()
        self._chunk_db.close()

    def export_graph(self) -> dict[str, Any]:
        """Return a JSON-serialisable graph suitable for the UI knowledge view.

        Returns ``{"nodes": [...], "edges": [...], "enabled": True}`` where
        each node has ``id``, ``name``, ``type`` and each edge has
        ``source``, ``target``, ``relation``, ``strength``.
        """
        entities = self._entity_graph.list_all_entities()
        nodes = [
            {"id": e.id, "name": e.name, "type": e.type}
            for e in entities
        ]
        triples = self._entity_graph.list_all_triples()
        edges = [
            {
                "source": t.head_id, "target": t.tail_id,
                "relation": t.relation, "strength": t.strength,
            }
            for t in triples
        ]
        return {"nodes": nodes, "edges": edges, "enabled": True}

    def __enter__(self) -> GraphRAGEngine:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

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
            self._chunk_db.execute(
                f"DELETE FROM chunks WHERE id IN ({placeholders})", old_ids
            )
            self._chunk_db.commit()

        texts = [c.content for c in chunks]
        embeddings = await self._embedder.embed(texts)

        for chunk, emb in zip(chunks, embeddings):
            self._chunk_db.execute(
                "INSERT OR REPLACE INTO chunks "
                "(id, source_path, heading, content, char_offset, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk.id, chunk.source_path, chunk.heading,
                    chunk.content, chunk.char_offset, time.time(),
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
        self._chunk_db.execute(
            f"DELETE FROM chunks WHERE id IN ({placeholders})", old_ids
        )
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

            parsed = _parse_extraction_response(resp.message.content or "")
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
                glean_parsed = _parse_extraction_response(
                    glean_resp.message.content or ""
                )
                self._store_extraction(glean_parsed, chunk.id)

    def _store_extraction(self, parsed: dict[str, Any], chunk_id: str) -> None:
        aliases = self._config.ontology.aliases
        entity_name_to_id: dict[str, int] = {}

        for ent in parsed.get("entities", []):
            name = ent.get("name", "").strip()
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
            head = rel.get("head", "").strip()
            tail = rel.get("tail", "").strip()
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

            self._entity_graph.add_triple(
                head_id, relation, tail_id, chunk_id, desc, strength
            )

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
            e.name for e in (
                self._entity_graph.get_entity(eid) for eid in seed_entity_ids
            ) if e is not None
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
                        trace.hops.append(HopRecord(
                            from_entity=ent.name,
                            to_entity=nb.name,
                            relation="",
                            hop_depth=1,
                        ))
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
                    subgraph_nodes.append({
                        "id": ent.id, "name": ent.name, "type": ent.type,
                        "degree": degree,
                    })
            seen_triple_ids: set[int] = set()
            for eid in all_entity_ids:
                for t in self._entity_graph.get_entity_triples(eid):
                    if t.id in seen_triple_ids:
                        continue
                    seen_triple_ids.add(t.id)
                    if t.head_id in all_entity_ids and t.tail_id in all_entity_ids:
                        subgraph_edges.append({
                            "source": t.head_id, "target": t.tail_id,
                            "relation": t.relation, "strength": t.strength,
                        })

        return EnrichedRetrieval(
            results=results, trace=trace,
            subgraph_nodes=subgraph_nodes, subgraph_edges=subgraph_edges,
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


def ontology_relation_ok(
    relation: str, core_relations: list[str], allow_custom: bool
) -> bool:
    if relation in core_relations:
        return True
    return allow_custom
