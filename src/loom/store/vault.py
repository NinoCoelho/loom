from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from loom.store.atomic import atomic_write


@runtime_checkable
class VaultProvider(Protocol):
    """Pluggable vault backend.

    Loom ships a filesystem+FTS5 default (``FilesystemVaultProvider``). Projects
    with richer semantics (kanban, backlinks, tag graph, etc.) implement this
    protocol and register their own instance with the vault tool.
    """

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]: ...
    async def read(self, path: str) -> str: ...
    async def write(self, path: str, content: str, metadata: dict | None = None) -> None: ...
    async def list(self, prefix: str = "") -> list[str]: ...
    async def delete(self, path: str) -> None: ...


class FilesystemVaultProvider:
    """Default VaultProvider: markdown files on disk + SQLite FTS5 index."""

    def __init__(self, vault_dir: Path) -> None:
        self._dir = vault_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index_path = vault_dir / "_index.sqlite"
        self._db = sqlite3.connect(str(self._index_path), check_same_thread=False)
        self._closed = False
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS vault_index (
                path TEXT PRIMARY KEY,
                title TEXT,
                doc_type TEXT,
                tags TEXT,
                fts TEXT
            )
        """)
        self._db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vault_fts USING fts5(
                path, title, content,
                tokenize='porter unicode61'
            )
        """)
        self._db.commit()

    def close(self) -> None:
        if self._closed:
            return
        self._db.close()
        self._closed = True

    def __enter__(self) -> FilesystemVaultProvider:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _safe_resolve(self, path: str) -> Path:
        target = (self._dir / path).resolve()
        if not str(target).startswith(str(self._dir.resolve())):
            raise ValueError("path traversal outside vault")
        return target

    def _parse_frontmatter(self, content: str) -> tuple[dict[str, Any], str]:
        fm: dict[str, Any] = {}
        body = content
        if content.startswith("---"):
            end = content.find("---", 3)
            if end != -1:
                import yaml

                try:
                    fm = yaml.safe_load(content[3:end]) or {}
                except Exception:
                    pass
                body = content[end + 3 :].strip()
        return fm, body

    def _extract_tags(self, fm: dict[str, Any], body: str) -> list[str]:
        tags: list[str] = list(fm.get("tags", []))
        for m in re.finditer(r"#(\w+)", body):
            tags.append(m.group(1))
        return list(set(tags))

    def _reindex_doc(self, path: str) -> None:
        full = self._dir / path
        if not full.exists():
            self._db.execute("DELETE FROM vault_index WHERE path = ?", (path,))
            self._db.execute("DELETE FROM vault_fts WHERE path = ?", (path,))
            self._db.commit()
            return

        content = full.read_text(encoding="utf-8")
        fm, body = self._parse_frontmatter(content)
        title = fm.get("title", "")
        doc_type = fm.get("type", "doc")
        tags = json.dumps(self._extract_tags(fm, body))

        if not title:
            first_h = re.search(r"^#\s+(.+)$", body, re.M)
            title = first_h.group(1).strip() if first_h else path

        self._db.execute(
            "INSERT OR REPLACE INTO vault_index "
            "(path, title, doc_type, tags, fts) VALUES (?, ?, ?, ?, ?)",
            (path, title, doc_type, tags, body[:5000]),
        )
        self._db.execute(
            "INSERT OR REPLACE INTO vault_fts (path, title, content) VALUES (?, ?, ?)",
            (path, title, body),
        )
        self._db.commit()

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT path, title, "
            "snippet(vault_fts, 2, '<<', '>>', '...', 30) as snippet, rank "
            "FROM vault_fts WHERE vault_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        return [
            {"path": r[0], "title": r[1], "snippet": r[2], "score": r[3]}
            for r in rows
        ]

    async def read(self, path: str) -> str:
        target = self._safe_resolve(path)
        if not target.exists():
            raise FileNotFoundError(f"Vault document not found: {path}")
        return target.read_text(encoding="utf-8")

    async def write(self, path: str, content: str, metadata: dict | None = None) -> None:
        target = self._safe_resolve(path)
        if metadata:
            import yaml

            fm_str = yaml.dump(metadata, default_flow_style=False).strip()
            content = f"---\n{fm_str}\n---\n{content}"
        atomic_write(target, content)
        self._reindex_doc(path)

    async def list(self, prefix: str = "") -> list[str]:
        base = self._dir
        if prefix:
            base = self._safe_resolve(prefix)
        results: list[str] = []
        for p in sorted(base.rglob("*.md")):
            if p.name == "SKILL.md":
                continue
            rel = p.relative_to(self._dir)
            if not str(rel).startswith("_"):
                results.append(str(rel))
        return results

    async def delete(self, path: str) -> None:
        target = self._safe_resolve(path)
        if target.exists():
            target.unlink()
        self._reindex_doc(path)

    def reindex_all(self) -> None:
        for p in sorted(self._dir.rglob("*.md")):
            if p.name.startswith("_"):
                continue
            rel = p.relative_to(self._dir)
            self._reindex_doc(str(rel))


# Back-compat alias (deprecated; use FilesystemVaultProvider).
VaultStore = FilesystemVaultProvider
