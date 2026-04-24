"""Shared SQLite lifecycle management and schema migration helpers.

Provides:
- :class:`SqliteResource` — mixin for proper close / context-manager / ``__del__``
  lifecycle on SQLite-backed stores.
- :func:`ensure_columns` — idempotent ``ALTER TABLE … ADD COLUMN`` helper
  used by :class:`~loom.store.memory.MemoryStore` and
  :class:`~loom.store.session.SessionStore`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class SqliteResource:
    """Mixin for sqlite-backed resources with proper lifecycle management.

    Subclasses must set ``self._db`` and ``self._closed`` in ``__init__``.
    Override :meth:`_close_db` if cleanup beyond ``db.close()`` is needed
    (e.g. closing multiple connections).
    """

    _closed: bool

    def _init_db(self, db_path: Path) -> sqlite3.Connection:
        """Open a WAL-mode SQLite connection and mark the resource as open."""
        db = sqlite3.connect(str(db_path), check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        self._closed = False
        return db

    def _close_db(self) -> None:
        """Override to close additional resources. Base implementation does nothing."""
        pass

    def close(self) -> None:
        if self._closed:
            return
        self._close_db()
        db = getattr(self, "_db", None)
        if db is not None:
            db.close()
        self._closed = True

    def __enter__(self) -> SqliteResource:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def ensure_columns(
    db: sqlite3.Connection,
    table: str,
    columns: dict[str, str],
) -> None:
    """Idempotently add missing ``columns`` to ``table``.

    Args:
        db: Open SQLite connection.
        table: Table name to inspect.
        columns: Mapping of ``column_name -> column_spec``
            (e.g. ``"pinned": "INTEGER DEFAULT 0"``).

    .. note::
       ``table`` is interpolated directly into SQL.  This is safe as long
       as callers pass hardcoded table names (current usage), but must not
       be used with user-supplied input.
    """
    # table is hardcoded at all call-sites — not user-supplied.
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}
    for name, spec in columns.items():
        if name not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
    db.commit()
