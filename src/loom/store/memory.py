from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from loom.store.atomic import atomic_write


class MemoryEntry:
    __slots__ = ("key", "category", "tags", "content", "created", "updated", "path")

    def __init__(
        self,
        key: str,
        category: str = "notes",
        tags: list[str] | None = None,
        content: str = "",
        created: str | None = None,
        updated: str | None = None,
        path: Path | None = None,
    ) -> None:
        self.key = key
        self.category = category
        self.tags = tags or []
        self.content = content
        self.created = created or datetime.utcnow().isoformat()
        self.updated = updated or self.created
        self.path = path


class SearchHit:
    __slots__ = ("key", "category", "snippet", "score")

    def __init__(self, key: str, category: str, snippet: str, score: float) -> None:
        self.key = key
        self.category = category
        self.snippet = snippet
        self.score = score


class MemoryStore:
    def __init__(self, memory_dir: Path, index_db: Path | None = None) -> None:
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

    def _key_path(self, key: str) -> Path:
        if ".." in key or key.startswith("/") or "\\" in key:
            raise ValueError(f"invalid memory key: {key!r}")
        return self._dir / f"{key}.md"

    def _write_file(self, key: str, content: str, category: str, tags: list[str]) -> None:
        path = self._key_path(key)
        now = datetime.now().isoformat()
        fm = {
            "category": category,
            "tags": tags,
            "updated": now,
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
            "INSERT OR REPLACE INTO memory_meta (key, category, tags, created, updated) VALUES (?, ?, ?, ?, ?)",
            (key, category, json.dumps(tags), fm["created"], fm["updated"]),
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
        )

    async def write(self, key: str, content: str, category: str = "notes", tags: list[str] | None = None) -> None:
        self._write_file(key, content, category, tags or [])

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

    async def list_entries(self, category: str | None = None, limit: int = 50) -> list[MemoryEntry]:
        if category:
            rows = self._db.execute(
                "SELECT key, category, tags, created, updated FROM memory_meta WHERE category = ? ORDER BY updated DESC LIMIT ?",
                (category, limit),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT key, category, tags, created, updated FROM memory_meta ORDER BY updated DESC LIMIT ?",
                (limit,),
            ).fetchall()
        entries: list[MemoryEntry] = []
        for r in rows:
            entries.append(MemoryEntry(
                key=r[0],
                category=r[1],
                tags=json.loads(r[2]) if r[2] else [],
                created=r[3],
                updated=r[4],
            ))
        return entries

    def recent(self, limit: int = 5, budget: int = 1500) -> list[tuple[str, str]]:
        rows = self._db.execute(
            "SELECT key, category, updated FROM memory_meta ORDER BY updated DESC LIMIT ?",
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

    def reindex_all(self) -> None:
        self._db.execute("DELETE FROM memory_fts")
        self._db.execute("DELETE FROM memory_meta")
        self._db.commit()
        for p in sorted(self._dir.rglob("*.md")):
            if p.name.startswith("_"):
                continue
            key = p.stem
            try:
                self._read_file(key)
                entry = self._read_file(key)
                if entry:
                    self._write_file(key, entry.content, entry.category, entry.tags)
            except Exception:
                continue
