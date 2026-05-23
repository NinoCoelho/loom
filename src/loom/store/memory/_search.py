"""Hybrid retrieval engine for the memory store.

Blends BM25 (via SQLite FTS5) with salience and recency signals, and
optionally with vector similarity when an embedding provider is
configured.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

from loom.store.embeddings import _cosine_similarity
from loom.store.memory._types import MemoryEntry, RecallHit
from loom.store.vector import _pack_vector, _unpack_vector

if TYPE_CHECKING:
    from loom.store.memory._backend import StorageBackend
    from loom.store.memory._core import EmbeddingProvider

logger = logging.getLogger(__name__)

_W_BM25 = 0.35
_W_SALIENCE = 0.25
_W_RECENCY = 0.10
_W_VECTOR = 0.30

_W_BM25_NOVEC = 0.55
_W_SALIENCE_NOVEC = 0.30
_W_RECENCY_NOVEC = 0.15

_RECENCY_TAU_DAYS = 14.0


def _parse_iso_utc(ts: str) -> datetime:
    when = datetime.fromisoformat(ts)
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


class MemorySearchEngine:
    def __init__(
        self,
        db: sqlite3.Connection,
        has_fts5: bool,
        embedder: EmbeddingProvider | None,
        backend: StorageBackend,
    ) -> None:
        self._db = db
        self._has_fts5 = has_fts5
        self._embedder = embedder
        self._backend = backend

    async def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        candidate_pool: int = 30,
        budget: int | None = None,
        touch: bool = True,
        touch_fn=None,
    ) -> list[RecallHit]:
        candidates = await self._bm25_candidates(query, candidate_pool)
        if not candidates:
            return []

        query_embedding: list[float] | None = None
        if self._embedder is not None:
            try:
                embeds = await self._embedder.embed([query])
                if embeds:
                    query_embedding = embeds[0]
            except Exception:
                pass

        hits = self._rerank(candidates, query_embedding=query_embedding)
        top = hits[:limit]
        if budget is not None:
            bounded: list[RecallHit] = []
            total = 0
            for h in top:
                if total + len(h.preview) > budget:
                    break
                bounded.append(h)
                total += len(h.preview)
            top = bounded
        if touch and touch_fn:
            for h in top:
                touch_fn(h.key)
        return top

    async def _bm25_candidates(self, query: str, pool: int) -> list[dict[str, Any]]:
        if self._has_fts5:
            rows = self._db.execute(
                "SELECT key, category, "
                "snippet(memory_fts, 2, '<<', '>>', '...', 30), rank "
                "FROM memory_fts WHERE memory_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (query, pool),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT key, category, substr(content,1,200), 0.0 "
                "FROM memory_content WHERE content LIKE ? LIMIT ?",
                (f"%{query}%", pool),
            ).fetchall()
        return [{"key": r[0], "category": r[1], "snippet": r[2], "bm25": r[3]} for r in rows]

    def _rerank(
        self,
        candidates: list[dict[str, Any]],
        *,
        query_embedding: list[float] | None = None,
    ) -> list[RecallHit]:
        ranks = [c["bm25"] for c in candidates]
        worst = min(ranks) if ranks else 0.0
        best = max(ranks) if ranks else 0.0
        span = (best - worst) or 1.0

        now = datetime.now(UTC)
        hits: list[RecallHit] = []
        for cand in candidates:
            entry = self._backend.read(cand["key"])
            if entry is None:
                continue
            bm25_norm = (cand["bm25"] - worst) / span if span else 0.0
            bm25_norm = 1.0 - bm25_norm

            salience = self._salience(entry)
            recency = self._recency(entry, now)

            vector_score = 0.0
            components: dict[str, float] = {
                "bm25": bm25_norm,
                "salience": salience,
                "recency": recency,
            }
            if query_embedding is not None and self._embedder is not None:
                vec_blob = self._db.execute(
                    "SELECT embedding FROM memory_vectors WHERE key = ?",
                    (cand["key"],),
                ).fetchone()
                if vec_blob:
                    stored_vec = _unpack_vector(vec_blob[0])
                    vector_score = _cosine_similarity(query_embedding, stored_vec)
                    components["vector"] = vector_score

            if query_embedding is not None and self._embedder is not None:
                score = (
                    _W_BM25 * bm25_norm
                    + _W_SALIENCE * salience
                    + _W_RECENCY * recency
                    + _W_VECTOR * vector_score
                )
            else:
                score = (
                    _W_BM25_NOVEC * bm25_norm
                    + _W_SALIENCE_NOVEC * salience
                    + _W_RECENCY_NOVEC * recency
                )

            hits.append(
                RecallHit(
                    key=entry.key,
                    category=entry.category,
                    preview=entry.content[:300],
                    score=score,
                    components=components,
                )
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits

    @staticmethod
    def _salience(entry: MemoryEntry) -> float:
        pin = 1.0 if entry.pinned else 0.0
        importance = entry.importance / 3.0
        access = math.log1p(entry.access_count) / math.log1p(100)
        return 0.40 * pin + 0.35 * min(importance, 1.0) + 0.25 * min(access, 1.0)

    @staticmethod
    def _recency(entry: MemoryEntry, now: datetime) -> float:
        ts = entry.last_recalled_at or entry.updated or entry.created
        if not ts:
            return 0.0
        try:
            when = _parse_iso_utc(ts)
        except ValueError:
            return 0.0
        age_days = max(0.0, (now - when).total_seconds() / 86400.0)
        return math.exp(-age_days / _RECENCY_TAU_DAYS)
