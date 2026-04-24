"""Vault-backed memory store backend.

Handles all vault-delegated I/O for :class:`~loom.store.memory.MemoryStore`.
When a ``VaultProvider`` is configured, the ``MemoryStore`` delegates all
reads, writes, searches, and recall operations through this backend
instead of using local-disk markdown files.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from loom.store.frontmatter import parse_frontmatter

if TYPE_CHECKING:
    from loom.store.vault import VaultProvider

_VAULT_KEY_RE = re.compile(r"^[^/]+/(?:\d{4}/\d{2}/\d{2}/)?(.+)\.md$")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class VaultMemoryBackend:
    """Handles all vault-delegated I/O for MemoryStore.

    Owns vault path resolution, key↔path mapping, and vault-scoped
    search/recall. The ``db`` (SQLite connection) is shared from
    MemoryStore for index updates.
    """

    def __init__(
        self,
        vault: VaultProvider,
        prefix: str,
        db: sqlite3.Connection,
    ) -> None:
        self._vault = vault
        self._prefix = prefix
        self._db = db

    @property
    def vault(self) -> VaultProvider:
        return self._vault

    @property
    def prefix(self) -> str:
        return self._prefix

    # ── key ↔ path mapping ──────────────────────────────────────────

    def key_from_vault_path(self, path: str) -> str:
        """Extract a memory key from a vault-relative path."""
        m = _VAULT_KEY_RE.match(path)
        if m:
            return m.group(1)
        key = path
        if key.endswith(".md"):
            key = key[:-3]
        prefix = f"{self._prefix}/"
        if key.startswith(prefix):
            key = key[len(prefix):]
        parts = key.split("/")
        if len(parts) >= 4 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
            key = "/".join(parts[3:])
        return key

    def vault_path_for_new(self, key: str, now_iso: str) -> str:
        dt = datetime.fromisoformat(now_iso)
        date_dir = f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}"
        return f"{self._prefix}/{date_dir}/{key}.md"

    def vault_path_for_existing(self, key: str) -> str | None:
        row = self._db.execute(
            "SELECT vault_path FROM memory_meta WHERE key = ?", (key,)
        ).fetchone()
        if row and row[0]:
            return row[0]
        flat = f"{self._prefix}/{key}.md"
        target = self._vault.root / flat
        if target.exists():
            return flat
        paths = self._scan_vault_for_key(key)
        return paths[0] if paths else None

    def _scan_vault_for_key(self, key: str) -> list[str]:
        base = self._vault.root / self._prefix
        if not base.exists():
            return []
        results: list[str] = []
        suffix = f"{key}.md"
        for p in base.rglob("*.md"):
            if p.name == suffix:
                rel = p.resolve().relative_to(self._vault.root.resolve())
                results.append(str(rel))
        return results

    # ── meta sync ───────────────────────────────────────────────────

    def sync_meta(self, key: str, **updates: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [key]
        self._db.execute(f"UPDATE memory_meta SET {sets} WHERE key = ?", vals)
        self._db.commit()

    # ── CRUD ────────────────────────────────────────────────────────

    async def write(
        self,
        key: str,
        content: str,
        category: str,
        tags: list[str],
        *,
        pinned: bool = False,
        importance: int = 1,
    ) -> str:
        now = _utc_now_iso()
        metadata: dict[str, Any] = {
            "category": category,
            "tags": tags,
            "updated": now,
            "pinned": bool(pinned),
            "importance": max(0, min(3, importance)),
        }
        existing_vpath = self.vault_path_for_existing(key)
        vpath = existing_vpath or self.vault_path_for_new(key, now)
        try:
            existing_raw = await self._vault.read(vpath)
            old_fm, _ = parse_frontmatter(existing_raw)
            if "created" in old_fm:
                metadata["created"] = old_fm["created"]
            metadata.setdefault("access_count", old_fm.get("access_count", 0))
            last = old_fm.get("last_recalled_at")
            if last is not None:
                metadata["last_recalled_at"] = last
        except (FileNotFoundError, Exception):
            pass
        if "created" not in metadata:
            metadata["created"] = now
        await self._vault.write(vpath, content, metadata=metadata)
        self._db.execute(
            "INSERT INTO memory_meta "
            "(key, category, tags, created, updated, pinned, importance, "
            "access_count, last_recalled_at, vault_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "category=excluded.category, tags=excluded.tags, "
            "updated=excluded.updated, pinned=excluded.pinned, "
            "importance=excluded.importance, "
            "access_count=excluded.access_count, "
            "last_recalled_at=excluded.last_recalled_at, "
            "vault_path=excluded.vault_path",
            (
                key,
                category,
                json.dumps(tags),
                metadata["created"],
                metadata["updated"],
                int(bool(pinned)),
                metadata["importance"],
                metadata.get("access_count", 0),
                metadata.get("last_recalled_at"),
                vpath,
            ),
        )
        self._db.commit()
        return vpath

    async def read(
        self,
        key: str,
        *,
        entry_factory: Any = None,
    ) -> Any | None:
        from loom.store.memory import MemoryEntry

        vpath = self.vault_path_for_existing(key)
        if vpath is None:
            return None
        try:
            raw = await self._vault.read(vpath)
        except FileNotFoundError:
            return None
        fm, body = parse_frontmatter(raw)
        return MemoryEntry(
            key=key,
            category=fm.get("category", "notes"),
            tags=fm.get("tags", []),
            content=body,
            created=fm.get("created"),
            updated=fm.get("updated"),
            pinned=bool(fm.get("pinned", False)),
            importance=int(fm.get("importance", 1)),
            access_count=int(fm.get("access_count", 0)),
            last_recalled_at=fm.get("last_recalled_at"),
        )

    async def delete(self, key: str) -> str | None:
        vpath = self.vault_path_for_existing(key)
        if vpath is None:
            return None
        target = self._vault.root / vpath
        if not target.exists():
            return None
        await self._vault.delete(vpath)
        self._db.execute("DELETE FROM memory_meta WHERE key = ?", (key,))
        self._db.execute("DELETE FROM memory_vectors WHERE key = ?", (key,))
        self._db.commit()
        return vpath

    # ── search / recall ─────────────────────────────────────────────

    async def search(self, query: str, limit: int) -> list:
        from loom.store.memory import SearchHit

        results = await self._vault.search_scoped(query, self._prefix, limit=limit)
        hits: list[SearchHit] = []
        for r in results:
            key = self.key_from_vault_path(r.get("path", ""))
            hits.append(
                SearchHit(
                    key=key,
                    category="notes",
                    snippet=r.get("snippet", ""),
                    score=r.get("score", 0.0),
                )
            )
        return hits

    async def recall(
        self,
        query: str,
        *,
        limit: int = 5,
        touch_fn=None,
    ) -> list:
        from loom.store.memory import RecallHit

        results = await self._vault.search_scoped(query, self._prefix, limit=limit)
        raw_scores: list[float] = []
        for r in results:
            s = r.get("score", 0.0)
            raw_scores.append(float(s) if isinstance(s, (int, float)) else 0.0)
        worst = min(raw_scores) if raw_scores else 0.0
        best = max(raw_scores) if raw_scores else 0.0
        span = (best - worst) or 1.0

        hits: list[RecallHit] = []
        for r, raw_s in zip(results, raw_scores):
            key = self.key_from_vault_path(r.get("path", ""))
            snippet = r.get("snippet", "")
            bm25_norm = (raw_s - worst) / span
            bm25_norm = 1.0 - bm25_norm
            hits.append(
                RecallHit(
                    key=key,
                    category="notes",
                    preview=snippet[:300],
                    score=bm25_norm,
                    components={"bm25": bm25_norm},
                )
            )
        if touch_fn:
            for h in hits:
                touch_fn(h.key)
        return hits

    async def list_entries(self, category: str | None, limit: int) -> list:
        from loom.store.memory import MemoryEntry

        paths = await self._vault.list(prefix=self._prefix)
        entries: list[MemoryEntry] = []
        for p in paths[:limit]:
            key = self.key_from_vault_path(p)
            entry = await self.read(key)
            if entry is None:
                continue
            if category and entry.category != category:
                continue
            entries.append(entry)
        return entries

    def recent(self, limit: int, budget: int) -> list[tuple[str, str]]:
        memory_dir = self._vault.root / self._prefix
        if not memory_dir.exists():
            return []
        files = sorted(
            memory_dir.rglob("*.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        results: list[tuple[str, str]] = []
        total = 0
        resolved_root = self._vault.root.resolve()
        for f in files[:limit]:
            vpath = str(f.resolve().relative_to(resolved_root))
            key = self.key_from_vault_path(vpath)
            try:
                raw = f.read_text(encoding="utf-8")
                _, body = parse_frontmatter(raw)
                preview = body[:300]
            except Exception:
                continue
            if total + len(preview) > budget:
                break
            results.append((key, preview))
            total += len(preview)
        return results

    # ── frontmatter update ──────────────────────────────────────────

    def update_frontmatter(self, key: str, updates: dict[str, Any]) -> None:
        vpath = self.vault_path_for_existing(key)
        if vpath is None:
            return
        self._vault.update_frontmatter(vpath, updates)

    # ── reindex ─────────────────────────────────────────────────────

    def reindex(self, has_fts5: bool) -> None:
        base = self._vault.root / self._prefix
        if not base.exists():
            return
        for p in sorted(base.rglob("*.md")):
            key = self.key_from_vault_path(
                str(p.resolve().relative_to(self._vault.root.resolve()))
            )
            try:
                raw = p.read_text(encoding="utf-8")
                fm, body = parse_frontmatter(raw)
                if has_fts5:
                    self._db.execute(
                        "INSERT INTO memory_fts (key, category, content) VALUES (?, ?, ?)",
                        (key, fm.get("category", "notes"), body[:5000]),
                    )
                vpath = str(p.resolve().relative_to(self._vault.root.resolve()))
                self._db.execute(
                    "INSERT INTO memory_meta "
                    "(key, category, tags, created, updated, pinned, importance, "
                    "access_count, last_recalled_at, vault_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key,
                        fm.get("category", "notes"),
                        json.dumps(fm.get("tags", [])),
                        fm.get("created"),
                        fm.get("updated"),
                        int(bool(fm.get("pinned", False))),
                        int(fm.get("importance", 1)),
                        int(fm.get("access_count", 0)),
                        fm.get("last_recalled_at"),
                        vpath,
                    ),
                )
            except Exception:
                continue
        self._db.commit()
