"""Agent memory store — markdown-on-disk + SQLite FTS5 + salience signals.

When a :class:`VaultProvider` is supplied, the store delegates file I/O and
FTS5 search to the vault (reads/writes land under ``<vault_prefix>/``).
The standalone path (no vault) retains the original local-disk + SQLite
behaviour and is the default.

Retrieval layers
----------------
* :meth:`MemoryStore.search` — thin FTS5/LIKE keyword search (legacy).
* :meth:`MemoryStore.recall` — hybrid retrieval that blends BM25 with
  salience (pinned / importance / access) and recency, producing a
  single score per hit. Use this when enriching a prompt with relevant
  memories — it's what "never forget" is built on.

Salience is stored both in the YAML frontmatter of each ``.md`` file and
in the ``memory_meta`` table so recall can rank without re-reading every
file. Embeddings are intentionally pluggable (see
:class:`EmbeddingProvider`) but default to ``None`` — pure BM25+salience
is the baseline; a vector model can be wired in later without touching
callers.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import yaml  # noqa: F401 — kept for backward compat re-exports

from loom.store.db import SqliteResource
from loom.store.memory._backend import FileStorageBackend, StorageBackend
from loom.store.memory._schema import MemorySchema
from loom.store.memory._search import MemorySearchEngine
from loom.store.memory._types import MemoryEntry, RecallHit, SearchHit
from loom.store.memory._vault_backend import VaultStorageBackend
from loom.store.vector import _pack_vector

if TYPE_CHECKING:
    from loom.store.graphrag import GraphRAGEngine
    from loom.store.vault import VaultProvider

logger = logging.getLogger(__name__)


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Pluggable vector-embedding backend for hybrid recall.

    Implementations should batch-embed text. ``dim`` lets the store
    pre-allocate / validate vector storage. Kept optional — pure
    BM25+salience is the MemoryStore default when no provider is set.
    """

    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class MemoryStore(SqliteResource):
    def __init__(
        self,
        memory_dir: Path,
        index_db: Path | None = None,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        vault_provider: VaultProvider | None = None,
        vault_prefix: str = "memory",
        graphrag: GraphRAGEngine | None = None,
    ) -> None:
        self._dir = memory_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = index_db or memory_dir / "_index.sqlite"
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = self._init_db(self._index_path)
        self._embedder = embedding_provider
        self._vault = vault_provider
        self._vault_prefix = vault_prefix
        self._graphrag = graphrag

        schema = MemorySchema(self._db)
        self._has_fts5 = schema.has_fts5

        if vault_provider is not None:
            self._backend: StorageBackend = VaultStorageBackend(
                vault_provider, vault_prefix, self._db
            )
        else:
            self._backend = FileStorageBackend(
                self._dir, self._db, self._has_fts5
            )

        self._search = MemorySearchEngine(
            self._db, self._has_fts5, embedding_provider, self._backend
        )

    @property
    def vault_backend(self) -> VaultStorageBackend | None:
        if isinstance(self._backend, VaultStorageBackend):
            return self._backend
        return None

    async def write(
        self,
        key: str,
        content: str,
        category: str = "notes",
        tags: list[str] | None = None,
        *,
        pinned: bool = False,
        importance: int = 1,
    ) -> None:
        source_path: str | None = None
        if isinstance(self._backend, VaultStorageBackend):
            source_path = await self._backend.write(
                key, content, category, tags or [], pinned=pinned, importance=importance
            )
        else:
            source_path = self._backend.write(
                key, content, category, tags or [], pinned=pinned, importance=importance
            )
        if self._embedder is not None:
            try:
                embeds = await self._embedder.embed([content[:2000]])
                if embeds:
                    blob = _pack_vector(embeds[0])
                    self._db.execute(
                        "INSERT OR REPLACE INTO memory_vectors (key, embedding) VALUES (?, ?)",
                        (key, blob),
                    )
                    self._db.commit()
            except Exception:
                logger.warning("embedding failed for %s", key, exc_info=True)
        if self._graphrag is not None and source_path is not None:
            try:
                await self._graphrag.index_source(source_path, content)
            except Exception:
                logger.warning("graphrag index_source failed for %s", key, exc_info=True)

    async def read(self, key: str) -> MemoryEntry | None:
        if isinstance(self._backend, VaultStorageBackend):
            return await self._backend.read(key)
        return self._backend.read(key)

    async def delete(self, key: str) -> bool:
        removed_source: str | None = None
        if isinstance(self._backend, VaultStorageBackend):
            removed_source = await self._backend.delete(key)
            if removed_source is None:
                return False
        else:
            removed_source = self._backend.delete(key)
            if removed_source is None:
                return False
        if self._graphrag is not None and removed_source is not None:
            try:
                self._graphrag.remove_source(removed_source)
            except Exception:
                logger.warning("graphrag remove_source failed for %s", key, exc_info=True)
        return True

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        if isinstance(self._backend, VaultStorageBackend):
            return await self._backend.search(query, limit)
        return self._backend.search(query, limit)

    async def list_entries(
        self, category: str | None = None, limit: int = 50
    ) -> list[MemoryEntry]:
        if isinstance(self._backend, VaultStorageBackend):
            return await self._backend.list_entries(category, limit)
        return self._backend.list_entries(category, limit)

    def recent(self, limit: int = 5, budget: int = 1500) -> list[tuple[str, str]]:
        return self._backend.recent(limit, budget)

    def pin(self, key: str, pinned: bool = True) -> None:
        self._backend.update_frontmatter(key, {"pinned": pinned})

    def set_importance(self, key: str, level: int) -> None:
        level = max(0, min(3, level))
        self._backend.update_frontmatter(key, {"importance": level})

    def touch(self, key: str) -> None:
        self._backend.touch(key)

    async def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        candidate_pool: int = 30,
        budget: int | None = None,
        touch: bool = True,
    ) -> list[RecallHit]:
        if isinstance(self._backend, VaultStorageBackend):
            return await self._backend.recall(
                query, limit=limit, touch=touch, touch_fn=self.touch
            )
        return await self._search.recall(
            query,
            limit=limit,
            candidate_pool=candidate_pool,
            budget=budget,
            touch=touch,
            touch_fn=self.touch,
        )

    def reindex_all(self) -> None:
        self._db.execute("DELETE FROM memory_fts")
        self._db.execute("DELETE FROM memory_meta")
        self._db.commit()
        self._backend.reindex(self._has_fts5)
