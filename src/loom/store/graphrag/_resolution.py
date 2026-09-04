"""Staged entity resolution: exact alias match → embedding similarity → LLM.

Inspired by Utopia's three-stage entity resolution pipeline: cheap exact
matches first, prototype-embedding similarity second, and an LLM arbiter for
the gray zone. Merges performed here are recorded so they stay reversible.
"""

from __future__ import annotations

import logging
from typing import Any

from loom.store.graphrag._types import GraphRAGConfig

logger = logging.getLogger(__name__)

_PROTOTYPE_SOURCE = "graphrag_entity"

_SAME_ENTITY_PROMPT = """\
Decide whether two extracted entities refer to the same real-world thing.

Candidate 1: name="{new_name}" type="{new_type}"
Candidate 2: name="{old_name}" type="{old_type}"
Semantic similarity of names: {score:.2f}

Consider naming conventions, abbreviations, and context. Same type is a
strong signal; different types usually mean different entities.

Respond with ONLY valid JSON: {{"same": true}} or {{"same": false}}\
"""


class EntityResolver:
    def __init__(
        self,
        entity_graph: Any,
        vector_store: Any,
        embedder: Any,
        config: GraphRAGConfig,
        llm_provider: Any = None,
    ) -> None:
        self._graph = entity_graph
        self._vectors = vector_store
        self._embedder = embedder
        self._config = config
        self._llm = llm_provider
        self._cache: dict[str, int] = {}
        # In-memory prototype matrix. Without it every _nearest_candidate call
        # re-unpacked EVERY prototype vector from SQLite and ran a full
        # matrix multiply — O(N) per resolve, O(N²) per bulk reindex: at
        # ~15k entities a full reindex collapsed to a few files/minute.
        # The cache refreshes lazily (bounded staleness of
        # _CACHE_REFRESH_EVERY new entities) so per-resolve cost is a single
        # cached matvec.
        self._proto_ids: list[str] | None = None
        self._proto_matrix: Any = None
        self._proto_count_at_build = -1
        self._resolves_since_build = 0

    _CACHE_REFRESH_EVERY = 256

    def _prototype_matrix(self) -> tuple[list[str], Any] | None:
        """Return (vector_ids, matrix) for all prototypes, rebuilding the
        cache at most once per _CACHE_REFRESH_EVERY new entities."""
        self._resolves_since_build += 1
        current = self._graph.count_entities()
        stale = current != self._proto_count_at_build
        due = self._resolves_since_build >= self._CACHE_REFRESH_EVERY
        if self._proto_matrix is None or (stale and due):
            try:
                rows = self._vectors._db.execute(  # noqa: SLF001 - same package
                    "SELECT id, embedding FROM vectors WHERE source = ?",
                    (_PROTOTYPE_SOURCE,),
                ).fetchall()
                self._proto_ids = [r[0] for r in rows]
                matrix: Any = None
                try:
                    import numpy as np

                    if rows:
                        # Bulk little-endian float32 read — one frombuffer
                        # over the concatenated BLOBs instead of a Python
                        # struct.unpack per row (13k rows × 384 dims made
                        # rebuilds the dominant cost on large graphs).
                        blob = b"".join(r[1] for r in rows)
                        dim = len(rows[0][1]) // 4
                        matrix = np.frombuffer(blob, dtype="<f4").reshape(len(rows), dim)
                except ImportError:
                    from loom.store.vector import _unpack_vector

                    vectors = [_unpack_vector(r[1]) for r in rows]
                    matrix = vectors or None
                self._proto_matrix = matrix
                self._proto_count_at_build = current
                self._resolves_since_build = 0
            except Exception:
                logger.debug("prototype matrix build failed", exc_info=True)
                return None
        if not self._proto_ids or self._proto_matrix is None:
            return None
        return self._proto_ids, self._proto_matrix

    async def resolve(
        self, name: str, type: str, aliases: dict[str, list[str]] | None = None
    ) -> int:
        key = f"{name.strip().lower()}::{type}"
        cached = self._cache.get(key)
        if cached is not None and self._graph.get_entity(cached) is not None:
            return cached

        # Stage 1: exact canonical / alias match (no creation).
        existing = self._graph.find_entity(name, type)
        if existing is not None:
            self._cache[key] = existing.id
            return existing.id
        canonical = name.strip().lower()
        if aliases:
            for canon, alts in aliases.items():
                if canonical in [a.lower() for a in alts]:
                    found = self._graph.find_entity(canon, type)
                    if found is not None:
                        self._cache[key] = found.id
                        return found.id

        # Stage 2 + 3: prototype embedding similarity with LLM tie-break.
        if self._config.resolution.enabled:
            try:
                candidate = await self._nearest_candidate(name, type)
                if candidate is not None:
                    ent_id, score = candidate
                    if score >= self._config.resolution.auto_merge_threshold:
                        self._cache[key] = ent_id
                        return ent_id
                    if (
                        score >= self._config.resolution.llm_threshold
                        and self._llm is not None
                        and await self._llm_agrees_same(name, type, ent_id, score)
                    ):
                        self._cache[key] = ent_id
                        return ent_id
            except Exception:
                logger.debug("Entity resolution fallback for %r", name, exc_info=True)

        new_id = self._graph.resolve_entity(name, type, aliases)
        try:
            embeds = await self._embedder.embed([name])
            if embeds:
                self._vectors.upsert(
                    f"entity:{new_id}",
                    embeds[0],
                    source=_PROTOTYPE_SOURCE,
                    metadata={"name": name, "type": type},
                )
        except Exception:
            logger.debug("Prototype embed failed for %r", name, exc_info=True)
        self._cache[key] = new_id
        return new_id

    async def _nearest_candidate(self, name: str, type: str) -> tuple[int, float] | None:
        embeds = await self._embedder.embed([name])
        if not embeds:
            return None

        cached = self._prototype_matrix()
        if cached is not None:
            ids, matrix = cached
            try:
                import numpy as np

                q = np.asarray(embeds[0], dtype=np.float32)
                q_norm = float(np.linalg.norm(q)) or 1.0
                m_norm = np.linalg.norm(matrix, axis=1)
                denom = np.maximum(m_norm * q_norm, 1e-12)
                scores = (matrix @ q) / denom
                # Wider than the old top-5 so the type filter has room when
                # the nearest hits are all of a different type.
                top = np.argsort(-scores)[:25]
                for i in top:
                    ent_id = self._entity_id_for(ids[int(i)])
                    if ent_id is None:
                        continue
                    ent = self._graph.get_entity(ent_id)
                    if ent is None or ent.type != type:
                        continue
                    return ent_id, float(scores[int(i)])
                return None
            except ImportError:
                pass  # no numpy — fall through to the store search path

        hits = self._vectors.search(
            embeds[0],
            top_k=5,
            source_filter=_PROTOTYPE_SOURCE,
        )
        for hit in hits:
            ent_id = self._entity_id_for(hit.id)
            if ent_id is None:
                continue
            ent = self._graph.get_entity(ent_id)
            if ent is None:
                continue
            if ent.type != type:
                continue
            return ent_id, hit.score
        return None

    @staticmethod
    def _entity_id_for(vector_id: str) -> int | None:
        try:
            return int(vector_id.split(":", 1)[1])
        except (IndexError, ValueError):
            return None

    async def _llm_agrees_same(
        self, new_name: str, new_type: str, entity_id: int, score: float
    ) -> bool:
        from loom.types import ChatMessage, Role

        ent = self._graph.get_entity(entity_id)
        if ent is None:
            return False
        prompt = _SAME_ENTITY_PROMPT.format(
            new_name=new_name,
            new_type=new_type,
            old_name=ent.name,
            old_type=ent.type or "concept",
            score=score,
        )
        try:
            resp = await self._llm.chat([ChatMessage(role=Role.USER, content=prompt)])
        except Exception:
            logger.debug("LLM entity-match call failed", exc_info=True)
            return False
        content = (resp.message.content or "").strip().lower()
        return '"same": true' in content or '"same":true' in content
