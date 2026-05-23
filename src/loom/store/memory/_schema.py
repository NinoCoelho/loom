"""One-shot schema creation and migration for the memory store."""

from __future__ import annotations

import sqlite3

from loom.store.db import ensure_columns

_SALIENCE_COLUMNS: dict[str, str] = {
    "pinned": "INTEGER DEFAULT 0",
    "importance": "INTEGER DEFAULT 1",
    "access_count": "INTEGER DEFAULT 0",
    "last_recalled_at": "TEXT",
}


class MemorySchema:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._has_fts5 = self._init_fts5()
        self._create_tables()
        self._migrate_salience_columns()
        self._migrate_vault_path_column()

    @property
    def has_fts5(self) -> bool:
        return self._has_fts5

    def _create_tables(self) -> None:
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS memory_meta (
                key TEXT PRIMARY KEY,
                category TEXT DEFAULT 'notes',
                tags TEXT DEFAULT '[]',
                created TEXT,
                updated TEXT
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS memory_vectors (
                key TEXT PRIMARY KEY,
                embedding BLOB NOT NULL
            )
        """)
        self._db.commit()

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
        ensure_columns(self._db, "memory_meta", _SALIENCE_COLUMNS)

    def _migrate_vault_path_column(self) -> None:
        ensure_columns(self._db, "memory_meta", {"vault_path": "TEXT"})
