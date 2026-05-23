from __future__ import annotations

import sqlite3

from loom.store.graph._types import Entity


_ENTITY_DDL = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,
    canonical TEXT NOT NULL,
    description TEXT DEFAULT '',
    degree INTEGER NOT NULL DEFAULT 0,
    UNIQUE(canonical, type)
);

CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical);
"""


class EntityRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.executescript(_ENTITY_DDL)

    def _migrate_degree_column(self) -> None:
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(entities)").fetchall()]
        if "degree" not in cols:
            self._db.execute(
                "ALTER TABLE entities ADD COLUMN degree INTEGER NOT NULL DEFAULT 0"
            )
            self._db.execute(
                "UPDATE entities SET degree = ("
                "  SELECT COUNT(*) FROM triples t "
                "  WHERE t.head_id = entities.id OR t.tail_id = entities.id"
                ")"
            )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_entities_degree ON entities(degree DESC)"
        )

    def resolve_entity(
        self,
        name: str,
        type: str,
        aliases: dict[str, list[str]] | None = None,
    ) -> int:
        canonical = name.strip().lower()
        row = self._db.execute(
            "SELECT id FROM entities WHERE canonical = ? AND type = ?",
            (canonical, type),
        ).fetchone()
        if row is not None:
            return row[0]

        if aliases:
            for canon, alts in aliases.items():
                low_alts = [a.lower() for a in alts]
                if canonical in low_alts:
                    row = self._db.execute(
                        "SELECT id FROM entities WHERE canonical = ?",
                        (canon.lower(),),
                    ).fetchone()
                    if row is not None:
                        return row[0]

        cur = self._db.execute(
            "INSERT INTO entities (name, type, canonical, description) VALUES (?, ?, ?, '')",
            (name.strip(), type, canonical),
        )
        self._db.commit()
        return cur.lastrowid

    def get_entity(self, entity_id: int) -> Entity | None:
        row = self._db.execute(
            "SELECT id, name, type, canonical, description FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        return Entity(id=row[0], name=row[1], type=row[2], canonical=row[3], description=row[4])

    def find_entity(self, name: str, type: str) -> Entity | None:
        canonical = name.strip().lower()
        row = self._db.execute(
            "SELECT id, name, type, canonical, description FROM entities "
            "WHERE canonical = ? AND type = ?",
            (canonical, type),
        ).fetchone()
        if row is None:
            return None
        return Entity(id=row[0], name=row[1], type=row[2], canonical=row[3], description=row[4])

    def set_entity_description(self, entity_id: int, description: str) -> None:
        self._db.execute(
            "UPDATE entities SET description = ? WHERE id = ?",
            (description, entity_id),
        )
        self._db.commit()
