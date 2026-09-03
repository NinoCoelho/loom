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
        hits = self._vectors.search(
            embeds[0],
            top_k=5,
            source_filter=_PROTOTYPE_SOURCE,
        )
        for hit in hits:
            try:
                ent_id = int(hit.id.split(":", 1)[1])
            except (IndexError, ValueError):
                continue
            ent = self._graph.get_entity(ent_id)
            if ent is None:
                continue
            if ent.type != type:
                continue
            return ent_id, hit.score
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
