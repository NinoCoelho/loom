"""Entity-relationship graph stored in SQLite.

Supports:

* Entities with a canonical ``(name, type)`` key and optional description.
* Directed, typed triples ``(head, relation, tail)`` backed by evidence
  from specific text chunks.
* Entity-to-chunk mention tracking for graph-augmented retrieval.
* Multi-hop neighbour traversal.
* Optional alias table for entity resolution (e.g. ``"Postgres"`` →
  ``"PostgreSQL"``).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Entity:
    id: int
    name: str
    type: str
    canonical: str
    description: str = ""


@dataclass
class Triple:
    id: int
    head_id: int
    relation: str
    tail_id: int
    chunk_id: str
    description: str = ""
    strength: float = 5.0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT,
    canonical TEXT NOT NULL,
    description TEXT DEFAULT '',
    UNIQUE(canonical, type)
);

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

CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities(canonical);
CREATE INDEX IF NOT EXISTS idx_triples_head ON triples(head_id);
CREATE INDEX IF NOT EXISTS idx_triples_tail ON triples(tail_id);
CREATE INDEX IF NOT EXISTS idx_triples_chunk ON triples(chunk_id);
CREATE INDEX IF NOT EXISTS idx_mentions_chunk ON entity_mentions(chunk_id);
"""


class EntityGraph:
    """SQLite-backed entity-relationship graph."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._closed = False
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        if self._closed:
            return
        self._db.close()
        self._closed = True

    def __enter__(self) -> EntityGraph:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

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
            "INSERT INTO entities (name, type, canonical, description) "
            "VALUES (?, ?, ?, '')",
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
        return Entity(
            id=row[0], name=row[1], type=row[2], canonical=row[3], description=row[4]
        )

    def find_entity(self, name: str, type: str) -> Entity | None:
        canonical = name.strip().lower()
        row = self._db.execute(
            "SELECT id, name, type, canonical, description FROM entities "
            "WHERE canonical = ? AND type = ?",
            (canonical, type),
        ).fetchone()
        if row is None:
            return None
        return Entity(
            id=row[0], name=row[1], type=row[2], canonical=row[3], description=row[4]
        )

    def add_triple(
        self,
        head_id: int,
        relation: str,
        tail_id: int,
        chunk_id: str,
        description: str = "",
        strength: float = 5.0,
    ) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO triples "
            "(head_id, relation, tail_id, chunk_id, description, strength) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (head_id, relation, tail_id, chunk_id, description, strength),
        )
        self._db.commit()

    def add_mention(self, entity_id: int, chunk_id: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO entity_mentions (entity_id, chunk_id) VALUES (?, ?)",
            (entity_id, chunk_id),
        )
        self._db.commit()

    def entities_for_chunk(self, chunk_id: str) -> list[Entity]:
        rows = self._db.execute(
            "SELECT e.id, e.name, e.type, e.canonical, e.description "
            "FROM entities e JOIN entity_mentions em ON e.id = em.entity_id "
            "WHERE em.chunk_id = ?",
            (chunk_id,),
        ).fetchall()
        return [
            Entity(id=r[0], name=r[1], type=r[2], canonical=r[3], description=r[4])
            for r in rows
        ]

    def chunks_for_entity(self, entity_id: int) -> list[str]:
        rows = self._db.execute(
            "SELECT chunk_id FROM entity_mentions WHERE entity_id = ?",
            (entity_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def neighbors(self, entity_id: int, max_hops: int = 2) -> list[Entity]:
        visited: set[int] = {entity_id}
        frontier: set[int] = {entity_id}
        for _ in range(max_hops):
            next_frontier: set[int] = set()
            for eid in frontier:
                rows = self._db.execute(
                    "SELECT head_id, tail_id FROM triples "
                    "WHERE head_id = ? OR tail_id = ?",
                    (eid, eid),
                ).fetchall()
                for head_id, tail_id in rows:
                    other = tail_id if head_id == eid else head_id
                    if other not in visited:
                        visited.add(other)
                        next_frontier.add(other)
            frontier = next_frontier
            if not frontier:
                break
        visited.discard(entity_id)
        if not visited:
            return []
        placeholders = ",".join("?" for _ in visited)
        rows = self._db.execute(
            f"SELECT id, name, type, canonical, description FROM entities "
            f"WHERE id IN ({placeholders})",
            list(visited),
        ).fetchall()
        return [
            Entity(id=r[0], name=r[1], type=r[2], canonical=r[3], description=r[4])
            for r in rows
        ]

    def remove_for_chunks(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        placeholders = ",".join("?" for _ in chunk_ids)
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
        self._db.commit()

    def remove_for_source(self, source: str, chunk_ids: list[str]) -> None:
        self.remove_for_chunks(chunk_ids)

    def count_entities(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM entities").fetchone()
        return row[0] if row else 0

    def count_triples(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM triples").fetchone()
        return row[0] if row else 0
