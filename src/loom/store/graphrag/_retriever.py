"""GraphRAG retrieval — hybrid vector + graph expansion."""

from __future__ import annotations

from typing import Any

from loom.store.embeddings import _cosine_similarity
from loom.store.graphrag._types import (
    EnrichedRetrieval,
    GraphRAGConfig,
    HopRecord,
    RetrievalResult,
    RetrievalTrace,
)


class GraphRAGRetriever:
    def __init__(
        self,
        vector_store: Any,
        entity_graph: Any,
        chunk_db: Any,
        config: GraphRAGConfig,
        embedder: Any,
    ) -> None:
        self._vector_store = vector_store
        self._entity_graph = entity_graph
        self._chunk_db = chunk_db
        self._config = config
        self._embedder = embedder

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
