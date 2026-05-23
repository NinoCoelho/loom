"""Storage backend protocol and file-system implementation."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from loom.store.atomic import atomic_write
from loom.store.frontmatter import build_frontmatter, parse_frontmatter
from loom.store.frontmatter import rewrite_frontmatter as _rewrite_fm
from loom.store.memory._types import MemoryEntry, SearchHit


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class StorageBackend(Protocol):
    def write(
        self,
        key: str,
        content: str,
        category: str,
        tags: list[str],
        *,
        pinned: bool = False,
        importance: int = 1,
    ) -> str | None: ...

    def read(self, key: str) -> MemoryEntry | None: ...

    def delete(self, key: str) -> str | None: ...

    def search(self, query: str, limit: int) -> list[SearchHit]: ...

    def list_entries(
        self, category: str | None, limit: int
    ) -> list[MemoryEntry]: ...

    def recent(self, limit: int, budget: int) -> list[tuple[str, str]]: ...

    def update_frontmatter(self, key: str, updates: dict[str, Any]) -> None: ...

    def touch(self, key: str) -> None: ...

    def reindex(self, has_fts5: bool) -> None: ...


class FileStorageBackend:
    def __init__(
        self, dir: Path, db: sqlite3.Connection, has_fts5: bool
    ) -> None:
        self._dir = dir
        self._db = db
        self._has_fts5 = has_fts5

    def _key_path(self, key: str) -> Path:
        if ".." in key or key.startswith("/") or "\\" in key:
            raise ValueError(f"invalid memory key: {key!r}")
        return self._dir / f"{key}.md"

    def write(
        self,
        key: str,
        content: str,
        category: str,
        tags: list[str],
        *,
        pinned: bool = False,
        importance: int = 1,
    ) -> str | None:
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
        return key

    def read(self, key: str) -> MemoryEntry | None:
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

    def delete(self, key: str) -> str | None:
        path = self._key_path(key)
        if not path.exists():
            return None
        path.unlink()
        if self._has_fts5:
            self._db.execute("DELETE FROM memory_fts WHERE key = ?", (key,))
        else:
            self._db.execute("DELETE FROM memory_content WHERE key = ?", (key,))
        self._db.execute("DELETE FROM memory_meta WHERE key = ?", (key,))
        self._db.execute("DELETE FROM memory_vectors WHERE key = ?", (key,))
        self._db.commit()
        return key

    def search(self, query: str, limit: int) -> list[SearchHit]:
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

    def list_entries(
        self, category: str | None, limit: int
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

    def recent(self, limit: int, budget: int) -> list[tuple[str, str]]:
        rows = self._db.execute(
            "SELECT key, category, updated FROM memory_meta "
            "ORDER BY COALESCE(pinned,0) DESC, updated DESC LIMIT ?",
            (limit,),
        ).fetchall()
        results: list[tuple[str, str]] = []
        total = 0
        for r in rows:
            entry = self.read(r[0])
            if not entry:
                continue
            preview = entry.content[:300]
            if total + len(preview) > budget:
                break
            results.append((r[0], preview))
            total += len(preview)
        return results

    def update_frontmatter(self, key: str, updates: dict[str, Any]) -> None:
        if "pinned" in updates:
            self._db.execute(
                "UPDATE memory_meta SET pinned = ? WHERE key = ?",
                (int(updates["pinned"]), key),
            )
            self._db.commit()
        elif "importance" in updates:
            self._db.execute(
                "UPDATE memory_meta SET importance = ? WHERE key = ?",
                (updates["importance"], key),
            )
            self._db.commit()
        _rewrite_fm(self._key_path(key), updates)

    def touch(self, key: str) -> None:
        now = _utc_now_iso()
        self._db.execute(
            "UPDATE memory_meta SET access_count = COALESCE(access_count,0)+1, "
            "last_recalled_at = ? WHERE key = ?",
            (now, key),
        )
        self._db.commit()
        row = self._db.execute(
            "SELECT access_count FROM memory_meta WHERE key = ?", (key,)
        ).fetchone()
        new_count = row[0] if row else 0
        _rewrite_fm(
            self._key_path(key),
            {
                "access_count": new_count,
                "last_recalled_at": now,
            },
        )

    def reindex(self, has_fts5: bool) -> None:
        for p in sorted(self._dir.rglob("*.md")):
            if p.name.startswith("_"):
                continue
            key = p.stem
            try:
                entry = self.read(key)
                if entry:
                    self.write(
                        key,
                        entry.content,
                        entry.category,
                        entry.tags,
                        pinned=entry.pinned,
                        importance=entry.importance,
                    )
            except Exception:
                continue
