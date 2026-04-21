"""Agent memory store — markdown-on-disk + SQLite FTS5 + salience signals.

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
import math
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from loom.store.atomic import atomic_write


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
        self.created = created or datetime.utcnow().isoformat()
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
_W_BM25 = 0.55
_W_SALIENCE = 0.30
_W_RECENCY = 0.15

# Recency half-life — older entries decay exponentially with this
# characteristic time (days). 14 days matches Nexus's working-memory
# intuition: this week and last week are sharp, older than that fades.
_RECENCY_TAU_DAYS = 14.0


class MemoryStore:
    def __init__(
        self,
        memory_dir: Path,
        index_db: Path | None = None,
        *,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self._dir = memory_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = index_db or memory_dir / "_index.sqlite"
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._index_path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
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
        self._embedder = embedding_provider

    # ── schema ──────────────────────────────────────────────────────

    def _init_fts5(self) -> bool:
        try:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS memory_fts USING fts5(
                    key, category, content,
                    tokenize='porter unicode61'
                )
            """)
            self._db.commit()
            return True
        except sqlite3.OperationalError:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS memory_content (
                    key TEXT,
                    category TEXT,
                    content TEXT
                )
            """)
            self._db.commit()
            return False

    def _migrate_salience_columns(self) -> None:
        """Idempotently add salience columns to pre-existing DBs."""
        existing = {
            row[1]
            for row in self._db.execute("PRAGMA table_info(memory_meta)").fetchall()
        }
        for name, spec in _SALIENCE_COLUMNS.items():
            if name not in existing:
                self._db.execute(
                    f"ALTER TABLE memory_meta ADD COLUMN {name} {spec}"
                )
        self._db.commit()

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
        now = datetime.now().isoformat()
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
                if existing.startswith("---"):
                    end = existing.find("---", 3)
                    if end != -1:
                        old_fm = yaml.safe_load(existing[3:end]) or {}
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

        fm_str = yaml.dump(fm, default_flow_style=False).strip()
        full = f"---\n{fm_str}\n---\n{content}"
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
        fm: dict[str, Any] = {}
        body = raw
        if raw.startswith("---"):
            end = raw.find("---", 3)
            if end != -1:
                try:
                    fm = yaml.safe_load(raw[3:end]) or {}
                except Exception:
                    pass
                body = raw[end + 3:].strip()

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
        self._write_file(
            key, content, category, tags or [], pinned=pinned, importance=importance
        )

    async def read(self, key: str) -> MemoryEntry | None:
        return self._read_file(key)

    async def delete(self, key: str) -> bool:
        path = self._key_path(key)
        if not path.exists():
            return False
        path.unlink()
        if self._has_fts5:
            self._db.execute("DELETE FROM memory_fts WHERE key = ?", (key,))
        else:
            self._db.execute("DELETE FROM memory_content WHERE key = ?", (key,))
        self._db.execute("DELETE FROM memory_meta WHERE key = ?", (key,))
        self._db.commit()
        return True

    # ── keyword search (legacy / exact) ─────────────────────────────

    async def search(self, query: str, limit: int = 10) -> list[SearchHit]:
        if self._has_fts5:
            rows = self._db.execute(
                "SELECT key, category, snippet(memory_fts, 2, '<<', '>>', '...', 30) as snippet, rank "
                "FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            return [
                SearchHit(key=r[0], category=r[1], snippet=r[2], score=r[3])
                for r in rows
            ]
        rows = self._db.execute(
            "SELECT key, category, content FROM memory_content WHERE content LIKE ? LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [
            SearchHit(key=r[0], category=r[1], snippet=r[2][:100], score=0.0)
            for r in rows
        ]

    async def list_entries(
        self, category: str | None = None, limit: int = 50
    ) -> list[MemoryEntry]:
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
                f"SELECT {cols} FROM memory_meta "
                "ORDER BY pinned DESC, updated DESC LIMIT ?",
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
        self._db.execute(
            "UPDATE memory_meta SET pinned = ? WHERE key = ?",
            (int(pinned), key),
        )
        self._db.commit()
        self._rewrite_frontmatter(key, {"pinned": pinned})

    def set_importance(self, key: str, level: int) -> None:
        level = max(0, min(3, level))
        self._db.execute(
            "UPDATE memory_meta SET importance = ? WHERE key = ?",
            (level, key),
        )
        self._db.commit()
        self._rewrite_frontmatter(key, {"importance": level})

    def touch(self, key: str) -> None:
        """Mark an entry as just-recalled — bumps access_count + timestamp."""
        now = datetime.utcnow().isoformat()
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
        path = self._key_path(key)
        if not path.exists():
            return
        raw = path.read_text(encoding="utf-8")
        fm: dict[str, Any] = {}
        body = raw
        if raw.startswith("---"):
            end = raw.find("---", 3)
            if end != -1:
                try:
                    fm = yaml.safe_load(raw[3:end]) or {}
                except Exception:
                    pass
                body = raw[end + 3:].lstrip("\n")
        fm.update(updates)
        fm_str = yaml.dump(fm, default_flow_style=False).strip()
        atomic_write(path, f"---\n{fm_str}\n---\n{body}")

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
        """Retrieve memories most relevant to ``query`` via BM25 + salience + recency.

        Returns up to ``limit`` hits ranked by a blended score. The
        ``candidate_pool`` knob controls how many BM25 candidates feed
        into the rerank — larger pools reduce the chance salience /
        recency gets ignored because a relevant-but-not-recent entry
        fell off the first page.

        When ``touch=True`` (default), every returned entry gets its
        ``access_count`` / ``last_recalled_at`` bumped so future recalls
        naturally favor memories the agent actually uses.
        """
        candidates = await self._bm25_candidates(query, candidate_pool)
        if not candidates:
            return []
        hits = self._rerank(candidates)
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

    async def _bm25_candidates(
        self, query: str, pool: int
    ) -> list[dict[str, Any]]:
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
        return [
            {"key": r[0], "category": r[1], "snippet": r[2], "bm25": r[3]}
            for r in rows
        ]

    def _rerank(self, candidates: list[dict[str, Any]]) -> list[RecallHit]:
        # FTS5 rank is negative, lower=better. Turn it into a positive
        # score in [0, 1] normalised against the worst rank in the pool.
        ranks = [c["bm25"] for c in candidates]
        worst = min(ranks) if ranks else 0.0
        best = max(ranks) if ranks else 0.0
        span = (best - worst) or 1.0

        now = datetime.utcnow()
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
            score = (
                _W_BM25 * bm25_norm
                + _W_SALIENCE * salience
                + _W_RECENCY * recency
            )
            hits.append(
                RecallHit(
                    key=entry.key,
                    category=entry.category,
                    preview=entry.content[:300],
                    score=score,
                    components={
                        "bm25": bm25_norm,
                        "salience": salience,
                        "recency": recency,
                    },
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
            when = datetime.fromisoformat(ts)
        except ValueError:
            return 0.0
        age_days = max(0.0, (now - when).total_seconds() / 86400.0)
        return math.exp(-age_days / _RECENCY_TAU_DAYS)

    # ── maintenance ─────────────────────────────────────────────────

    def reindex_all(self) -> None:
        self._db.execute("DELETE FROM memory_fts")
        self._db.execute("DELETE FROM memory_meta")
        self._db.commit()
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
