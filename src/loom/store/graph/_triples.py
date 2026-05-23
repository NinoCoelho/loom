from __future__ import annotations

import sqlite3

from loom.store.graph._types import Triple


_TRIPLE_DDL = """
CREATE TABLE IF NOT EXISTS triples (
    id INTEGER PRIMARY KEY,
    head_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    tail_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    description TEXT DEFAULT '',
    strength REAL DEFAULT 5.0,
    UNIQUE(head_id, relation, tail_id, chunk_id)
);

CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    PRIMARY KEY(entity_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_triples_head ON triples(head_id);
CREATE INDEX IF NOT EXISTS idx_triples_tail ON triples(tail_id);
CREATE INDEX IF NOT EXISTS idx_triples_chunk ON triples(chunk_id);
CREATE INDEX IF NOT EXISTS idx_mentions_chunk ON entity_mentions(chunk_id);
"""


class TripleRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.executescript(_TRIPLE_DDL)

    def add_triple(
        self,
        head_id: int,
        relation: str,
        tail_id: int,
        chunk_id: str,
        description: str = "",
        strength: float = 5.0,
    ) -> None:
        cur = self._db.execute(
            "INSERT OR IGNORE INTO triples "
            "(head_id, relation, tail_id, chunk_id, description, strength) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (head_id, relation, tail_id, chunk_id, description, strength),
        )
        if cur.rowcount > 0:
            self._db.execute(
                "UPDATE entities SET degree = degree + 1 WHERE id IN (?, ?)",
                (head_id, tail_id),
            )
        self._db.commit()

    def add_mention(self, entity_id: int, chunk_id: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO entity_mentions (entity_id, chunk_id) VALUES (?, ?)",
            (entity_id, chunk_id),
        )
        self._db.commit()

    def remove_for_chunks(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        placeholders = ",".join("?" for _ in chunk_ids)
        affected_rows = self._db.execute(
            f"SELECT DISTINCT head_id FROM triples WHERE chunk_id IN ({placeholders}) "
            f"UNION SELECT DISTINCT tail_id FROM triples WHERE chunk_id IN ({placeholders})",
            chunk_ids + chunk_ids,
        ).fetchall()
        affected_ids = [r[0] for r in affected_rows]

        self._db.execute(
            f"DELETE FROM entity_mentions WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        )
        self._db.execute(
            f"DELETE FROM triples WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        )
        self._db.execute(
            "DELETE FROM entities WHERE id NOT IN "
            "(SELECT head_id FROM triples UNION SELECT tail_id FROM triples) "
            "AND id NOT IN (SELECT entity_id FROM entity_mentions)"
        )
        if affected_ids:
            ph = ",".join("?" for _ in affected_ids)
            self._db.execute(
                f"UPDATE entities SET degree = ("
                f"  SELECT COUNT(*) FROM triples t "
                f"  WHERE t.head_id = entities.id OR t.tail_id = entities.id"
                f") WHERE id IN ({ph})",
                affected_ids,
            )
        self._db.commit()

    def remove_for_source(self, source: str, chunk_ids: list[str]) -> None:
        self.remove_for_chunks(chunk_ids)
