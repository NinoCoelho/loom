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

import json
import logging
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import yaml  # noqa: F401 — kept for backward compat re-exports

from loom.store.atomic import atomic_write
from loom.store.frontmatter import parse_frontmatter, build_frontmatter, rewrite_frontmatter as _rewrite_fm
from loom.store.db import SqliteResource, ensure_columns
from loom.store.embeddings import _cosine_similarity
from loom.store.memory._vault_backend import VaultMemoryBackend
from loom.store.vector import _pack_vector, _unpack_vector

if TYPE_CHECKING:
    from loom.store.graphrag import GraphRAGEngine
    from loom.store.vault import VaultProvider

logger = logging.getLogger(__name__)

# ── Embedding provider protocol (optional) ──────────────────────────────


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Pluggable vector-embedding backend for hybrid recall.

    Implementations should batch-embed text. ``dim`` lets the store
    pre-allocate / validate vector storage. Kept optional — pure
    BM25+salience is the MemoryStore default when no provider is set.
    """

    dim: int

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


# ── Data classes ────────────────────────────────────────────────────────


class MemoryEntry:
    __slots__ = (
        "key",
        "category",
        "tags",
        "content",
        "created",
        "updated",
        "path",
        "pinned",
        "importance",
        "access_count",
        "last_recalled_at",
    )

    def __init__(
        self,
        key: str,
        category: str = "notes",
        tags: list[str] | None = None,
        content: str = "",
        created: str | None = None,
        updated: str | None = None,
        path: Path | None = None,
        pinned: bool = False,
        importance: int = 1,
        access_count: int = 0,
        last_recalled_at: str | None = None,
    ) -> None:
        self.key = key
        self.category = category
        self.tags = tags or []
        self.content = content
        self.created = created or _utc_now_iso()
        self.updated = updated or self.created
        self.path = path
        self.pinned = pinned
        self.importance = max(0, min(3, importance))
        self.access_count = access_count
        self.last_recalled_at = last_recalled_at


class SearchHit:
    __slots__ = ("key", "category", "snippet", "score")

    def __init__(self, key: str, category: str, snippet: str, score: float) -> None:
        self.key = key
        self.category = category
        self.snippet = snippet
        self.score = score


class RecallHit:
    """Hybrid-retrieval result.

    ``score`` is the combined ranking value; the component breakdown is
    kept for debugging / tuning.
    """

    __slots__ = ("key", "category", "preview", "score", "components")

    def __init__(
        self,
        key: str,
        category: str,
        preview: str,
        score: float,
        components: dict[str, float],
    ) -> None:
        self.key = key
        self.category = category
        self.preview = preview
        self.score = score
        self.components = components


# ── Store ───────────────────────────────────────────────────────────────


_SALIENCE_COLUMNS: dict[str, str] = {
    "pinned": "INTEGER DEFAULT 0",
    "importance": "INTEGER DEFAULT 1",
    "access_count": "INTEGER DEFAULT 0",
    "last_recalled_at": "TEXT",
}

# Weights for the hybrid recall score. Stay inside [0, 1]; they don't
# have to sum to 1 but it makes the score interpretable.
_W_BM25 = 0.35
_W_SALIENCE = 0.25
_W_RECENCY = 0.10
_W_VECTOR = 0.30

_W_BM25_NOVEC = 0.55
_W_SALIENCE_NOVEC = 0.30
_W_RECENCY_NOVEC = 0.15

# Recency half-life — older entries decay exponentially with this
# characteristic time (days). 14 days matches Nexus's working-memory
# intuition: this week and last week are sharp, older than that fades.
_RECENCY_TAU_DAYS = 14.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _parse_iso_utc(ts: str) -> datetime:
    when = datetime.fromisoformat(ts)
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


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
        self._has_fts5 = self._init_fts5()
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS memory_meta (
                key TEXT PRIMARY KEY,
                category TEXT DEFAULT 'notes',
                tags TEXT DEFAULT '[]',
                created TEXT,
                updated TEXT
            )
        """)
        self._db.commit()
        self._migrate_salience_columns()
        self._migrate_vault_path_column()
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS memory_vectors (
                key TEXT PRIMARY KEY,
                embedding BLOB NOT NULL
            )
        """)
        self._db.commit()
        self._embedder = embedding_provider
        self._vault = vault_provider
        self._vault_prefix = vault_prefix
        self._graphrag = graphrag
        self._vault_backend: VaultMemoryBackend | None = None
        if vault_provider is not None:
            self._vault_backend = VaultMemoryBackend(
                vault_provider, vault_prefix, self._db
            )

    # ── schema ──────────────────────────────────────────────────────

    def _init_fts5(self) -> bool:
        has_fts5_table = self._table_exists("memory_fts")
        has_content_table = self._table_exists("memory_content")

        if not has_fts5_table:
            try:
                self._db.execute("""
                    CREATE VIRTUAL TABLE memory_fts USING fts5(
                        key, category, content,
                        tokenize='porter unicode61'
                    )
                """)
                self._db.commit()
                has_fts5_table = True
            except sqlite3.OperationalError:
                pass

        if has_fts5_table and has_content_table:
            self._migrate_content_to_fts5()
        elif not has_fts5_table and not has_content_table:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS memory_content (
                    key TEXT,
                    category TEXT,
                    content TEXT
                )
            """)
            self._db.commit()

        return has_fts5_table

    def _table_exists(self, name: str) -> bool:
        rows = self._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchall()
        return len(rows) > 0

    def _migrate_content_to_fts5(self) -> None:
        rows = self._db.execute("SELECT key, category, content FROM memory_content").fetchall()
        if not rows:
            return
        for key, category, content in rows:
            self._db.execute(
                "INSERT OR REPLACE INTO memory_fts (key, category, content) VALUES (?, ?, ?)",
                (key, category, content[:5000] if content else ""),
            )
        self._db.commit()

    def _migrate_salience_columns(self) -> None:
        """Idempotently add salience columns to pre-existing DBs."""
        ensure_columns(self._db, "memory_meta", _SALIENCE_COLUMNS)

    def _migrate_vault_path_column(self) -> None:
        ensure_columns(self._db, "memory_meta", {"vault_path": "TEXT"})

    # ── paths / IO ──────────────────────────────────────────────────

    def _key_path(self, key: str) -> Path:
        if ".." in key or key.startswith("/") or "\\" in key:
            raise ValueError(f"invalid memory key: {key!r}")
        return self._dir / f"{key}.md"

    def _write_file(
        self,
        key: str,
        content: str,
        category: str,
        tags: list[str],
        *,
        pinned: bool = False,
        importance: int = 1,
    ) -> None:
        path = self._key_path(key)
        now = _utc_now_iso()
        fm: dict[str, Any] = {
            "category": category,
            "tags": tags,
            "updated": now,
            "pinned": bool(pinned),
            "importance": max(0, min(3, importance)),
        }
        path_exists = path.exists()
        if path_exists:
            try:
                existing = path.read_text(encoding="utf-8")
                old_fm, _ = parse_frontmatter(existing)
                if "created" in old_fm:
                    fm["created"] = old_fm["created"]
                # Preserve counters across rewrites.
                fm.setdefault("access_count", old_fm.get("access_count", 0))
                last = old_fm.get("last_recalled_at")
                if last is not None:
                    fm["last_recalled_at"] = last
            except Exception:
                pass
        if "created" not in fm:
            fm["created"] = now

        full = build_frontmatter(fm, content)
        atomic_write(path, full)

        if self._has_fts5:
            self._db.execute("DELETE FROM memory_fts WHERE key = ?", (key,))
            self._db.execute(
                "INSERT INTO memory_fts (key, category, content) VALUES (?, ?, ?)",
                (key, category, content[:5000]),
            )
        else:
            self._db.execute("DELETE FROM memory_content WHERE key = ?", (key,))
            self._db.execute(
                "INSERT INTO memory_content (key, category, content) VALUES (?, ?, ?)",
                (key, category, content[:5000]),
            )
        self._db.execute(
            """
            INSERT INTO memory_meta
                (key, category, tags, created, updated,
                 pinned, importance, access_count, last_recalled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                category = excluded.category,
                tags = excluded.tags,
                updated = excluded.updated,
                pinned = excluded.pinned,
                importance = excluded.importance
            """,
            (
                key,
                category,
                json.dumps(tags),
                fm["created"],
                fm["updated"],
                int(bool(pinned)),
                fm["importance"],
                fm.get("access_count", 0),
                fm.get("last_recalled_at"),
            ),
        )
        self._db.commit()

    def _read_file(self, key: str) -> MemoryEntry | None:
        path = self._key_path(key)
        if not path.exists():
            return None
        raw = path.read_text(encoding="utf-8")
        fm, body = parse_frontmatter(raw)

        return MemoryEntry(
            key=key,
            category=fm.get("category", "notes"),
            tags=fm.get("tags", []),
            content=body,
            created=fm.get("created"),
            updated=fm.get("updated"),
            path=path,
            pinned=bool(fm.get("pinned", False)),
            importance=int(fm.get("importance", 1)),
            access_count=int(fm.get("access_count", 0)),
            last_recalled_at=fm.get("last_recalled_at"),
        )

    # ── vault backend accessor (for tests) ────────────────────────

    @property
    def vault_backend(self) -> VaultMemoryBackend | None:
        return self._vault_backend

    # ── public CRUD ─────────────────────────────────────────────────

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
        if self._vault_backend is not None:
            source_path = await self._vault_backend.write(
                key, content, category, tags or [], pinned=pinned, importance=importance
            )
        else:
            self._write_file(
                key,
                content,
                category,
                tags or [],
                pinned=pinned,
                importance=importance,
            )
            source_path = key
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
                pass
        if self._graphrag is not None and source_path is not None:
            try:
                await self._graphrag.index_source(source_path, content)
            except Exception:
                logger.warning("graphrag index_source failed for %s", key, exc_info=True)

    async def read(self, key: str) -> MemoryEntry | None:
        if self._vault_backend is not None:
            return await self._vault_backend.read(key)
        return self._read_file(key)

    async def delete(self, key: str) -> bool:
        removed_source: str | None = None
        if self._vault_backend is not None:
            removed_source = await self._vault_backend.delete(key)
            if removed_source is None:
                return False
        else:
            path = self._key_path(key)
            if not path.exists():
                return False
            path.unlink()
            if self._has_fts5:
                self._db.execute("DELETE FROM memory_fts WHERE key = ?", (key,))
            else:
                self._db.execute("DELETE FROM memory_content WHERE key = ?", (key,))
            self._db.execute("DELETE FROM memory_meta WHERE key = ?", (key,))
            self._db.execute("DELETE FROM memory_vectors WHERE key = ?", (key,))
            self._db.commit()
            removed_source = key
        if self._graphrag is not None and removed_source is not None:
            try:
                self._graphrag.remove_source(removed_source)
            except Exception:
                logger.warning("graphrag remove_source failed for %s", key, exc_info=True)
        return True

    # ── keyword search (legacy / exact) ─────────────────────────────

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        if self._vault_backend is not None:
            return await self._vault_backend.search(query, limit)
        if self._has_fts5:
            rows = self._db.execute(
                "SELECT key, category, "
                "snippet(memory_fts, 2, '<<', '>>', '...', 30) as snippet, rank "
                "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return [SearchHit(key=r[0], category=r[1], snippet=r[2], score=r[3]) for r in rows]
        rows = self._db.execute(
            "SELECT key, category, content FROM memory_content WHERE content LIKE ? LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [SearchHit(key=r[0], category=r[1], snippet=r[2][:100], score=0.0) for r in rows]

    async def list_entries(
        self, category: str | None = None, limit: int = 50
    ) -> list[MemoryEntry]:
        if self._vault_backend is not None:
            return await self._vault_backend.list_entries(category, limit)
        cols = (
            "key, category, tags, created, updated, "
            "COALESCE(pinned,0), COALESCE(importance,1), "
            "COALESCE(access_count,0), last_recalled_at"
        )
        if category:
            rows = self._db.execute(
                f"SELECT {cols} FROM memory_meta WHERE category = ? "
                "ORDER BY pinned DESC, updated DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                f"SELECT {cols} FROM memory_meta ORDER BY pinned DESC, updated DESC LIMIT ?",
                (limit,),
            ).fetchall()
        entries: list[MemoryEntry] = []
        for r in rows:
            entries.append(
                MemoryEntry(
                    key=r[0],
                    category=r[1],
                    tags=json.loads(r[2]) if r[2] else [],
                    created=r[3],
                    updated=r[4],
                    pinned=bool(r[5]),
                    importance=int(r[6]),
                    access_count=int(r[7]),
                    last_recalled_at=r[8],
                )
            )
        return entries

    def recent(self, limit: int = 5, budget: int = 1500) -> list[tuple[str, str]]:
        if self._vault_backend is not None:
            return self._vault_backend.recent(limit, budget)
        rows = self._db.execute(
            "SELECT key, category, updated FROM memory_meta "
            "ORDER BY COALESCE(pinned,0) DESC, updated DESC LIMIT ?",
            (limit,),
        ).fetchall()
        results: list[tuple[str, str]] = []
        total = 0
        for r in rows:
            entry = self._read_file(r[0])
            if not entry:
                continue
            preview = entry.content[:300]
            if total + len(preview) > budget:
                break
            results.append((r[0], preview))
            total += len(preview)
        return results

    # ── salience mutators ───────────────────────────────────────────

    def pin(self, key: str, pinned: bool = True) -> None:
        if self._vault_backend is not None:
            self._vault_backend.update_frontmatter(key, {"pinned": pinned})
            self._vault_backend.sync_meta(key, pinned=int(pinned))
            return
        self._db.execute(
            "UPDATE memory_meta SET pinned = ? WHERE key = ?",
            (int(pinned), key),
        )
        self._db.commit()
        self._rewrite_frontmatter(key, {"pinned": pinned})

    def set_importance(self, key: str, level: int) -> None:
        level = max(0, min(3, level))
        if self._vault_backend is not None:
            self._vault_backend.update_frontmatter(key, {"importance": level})
            self._vault_backend.sync_meta(key, importance=level)
            return
        self._db.execute(
            "UPDATE memory_meta SET importance = ? WHERE key = ?",
            (level, key),
        )
        self._db.commit()
        self._rewrite_frontmatter(key, {"importance": level})

    def touch(self, key: str) -> None:
        """Mark an entry as just-recalled — bumps access_count + timestamp."""
        now = _utc_now_iso()
        if self._vault_backend is not None:
            vpath = self._vault_backend.vault_path_for_existing(key)
            if vpath is None:
                return
            try:
                fm = self._vault.read_frontmatter(vpath)
            except FileNotFoundError:
                return
            count = int(fm.get("access_count", 0))
            self._vault_backend.update_frontmatter(
                key,
                {
                    "access_count": count + 1,
                    "last_recalled_at": now,
                },
            )
            self._vault_backend.sync_meta(
                key,
                access_count=count + 1,
                last_recalled_at=now,
            )
            return
        self._db.execute(
            "UPDATE memory_meta SET access_count = COALESCE(access_count,0)+1, "
            "last_recalled_at = ? WHERE key = ?",
            (now, key),
        )
        self._db.commit()
        # Persist to frontmatter too so the markdown file stays canonical.
        entry = self._read_file(key)
        if entry is None:
            return
        self._rewrite_frontmatter(
            key,
            {
                "access_count": entry.access_count + 1,
                "last_recalled_at": now,
            },
        )

    def _rewrite_frontmatter(self, key: str, updates: dict[str, Any]) -> None:
        """Merge ``updates`` into the ``.md`` file's YAML frontmatter."""
        _rewrite_fm(self._key_path(key), updates)

    # ── hybrid recall ───────────────────────────────────────────────

    async def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        candidate_pool: int = 30,
        budget: int | None = None,
        touch: bool = True,
    ) -> list[RecallHit]:
        """Retrieve memories most relevant to ``query``.

        When a vault_provider is set, delegates to
        ``vault_provider.search_scoped()`` for FTS5 retrieval across the
        vault prefix. Otherwise uses local BM25 + salience + recency.

        When ``touch=True`` (default), every returned entry gets its
        ``access_count`` / ``last_recalled_at`` bumped so future recalls
        naturally favor memories the agent actually uses.
        """
        if self._vault_backend is not None:
            return await self._vault_backend.recall(query, limit=limit, touch_fn=self.touch)

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
        if touch:
            for h in top:
                self.touch(h.key)
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
        # FTS5 rank is negative, lower=better. Turn it into a positive
        # score in [0, 1] normalised against the worst rank in the pool.
        ranks = [c["bm25"] for c in candidates]
        worst = min(ranks) if ranks else 0.0
        best = max(ranks) if ranks else 0.0
        span = (best - worst) or 1.0

        now = _utc_now()
        hits: list[RecallHit] = []
        for cand in candidates:
            entry = self._read_file(cand["key"])
            if entry is None:
                continue
            bm25_norm = (cand["bm25"] - worst) / span if span else 0.0
            # Highest raw rank (closest to 0) maps to 1.0.
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
        # log scale — first few accesses matter more than the hundredth.
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

    # ── maintenance ─────────────────────────────────────────────────

    def reindex_all(self) -> None:
        self._db.execute("DELETE FROM memory_fts")
        self._db.execute("DELETE FROM memory_meta")
        self._db.commit()
        if self._vault_backend is not None:
            self._vault_backend.reindex(self._has_fts5)
            return
        for p in sorted(self._dir.rglob("*.md")):
            if p.name.startswith("_"):
                continue
            key = p.stem
            try:
                entry = self._read_file(key)
                if entry:
                    self._write_file(
                        key,
                        entry.content,
                        entry.category,
                        entry.tags,
                        pinned=entry.pinned,
                        importance=entry.importance,
                    )
            except Exception:
                continue
