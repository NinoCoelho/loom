from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from loom.store.graph._types import Entity


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


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

CREATE TABLE IF NOT EXISTS entity_merges (
    id INTEGER PRIMARY KEY,
    survivor_id INTEGER NOT NULL,
    merged_id INTEGER NOT NULL,
    merged_name TEXT,
    merged_type TEXT,
    merged_canonical TEXT,
    merged_description TEXT,
    triple_snapshot TEXT,
    mention_snapshot TEXT,
    merged_at TEXT,
    reverted_at TEXT
);
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

    def merge_entities(self, survivor_id: int, merged_id: int) -> int | None:
        """Merge ``merged_id`` into ``survivor_id``; returns the merge id.

        Full snapshots of the merged entity, its triples and mentions are
        stored in ``entity_merges`` so the merge is fully reversible via
        :meth:`unmerge`.
        """
        if survivor_id == merged_id:
            return None
        merged = self.get_entity(merged_id)
        survivor = self.get_entity(survivor_id)
        if merged is None or survivor is None:
            return None

        triple_rows = self._db.execute(
            "SELECT * FROM triples WHERE head_id = ? OR tail_id = ?",
            (merged_id, merged_id),
        ).fetchall()
        mention_rows = self._db.execute(
            "SELECT chunk_id FROM entity_mentions WHERE entity_id = ?",
            (merged_id,),
        ).fetchall()
        cols = [c[1] for c in self._db.execute("PRAGMA table_info(triples)").fetchall()]
        triple_snapshot = [dict(zip(cols, r)) for r in triple_rows]
        mention_snapshot = [r[0] for r in mention_rows]

        placeholders = ",".join("?" for _ in triple_rows) or "''"
        ids = [r[0] for r in triple_rows]
        self._db.execute(
            f"DELETE FROM conflicts WHERE triple_id_a IN ({placeholders}) "
            f"OR triple_id_b IN ({placeholders})",
            ids + ids,
        )
        self._db.execute(
            "DELETE FROM triples WHERE head_id = ? OR tail_id = ?",
            (merged_id, merged_id),
        )
        self._db.execute(
            "DELETE FROM entity_mentions WHERE entity_id = ?", (merged_id,)
        )
        self._db.execute("DELETE FROM entities WHERE id = ?", (merged_id,))

        for snap in triple_snapshot:
            head = survivor_id if snap["head_id"] == merged_id else snap["head_id"]
            tail = survivor_id if snap["tail_id"] == merged_id else snap["tail_id"]
            if head == tail:
                continue
            self._db.execute(
                "INSERT OR IGNORE INTO triples "
                "(head_id, relation, tail_id, chunk_id, description, strength, "
                " source_path, valid_from, valid_to, asserted_at, superseded_by, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    head,
                    snap["relation"],
                    tail,
                    snap["chunk_id"],
                    snap["description"],
                    snap["strength"],
                    snap["source_path"],
                    snap["valid_from"],
                    snap["valid_to"],
                    snap["asserted_at"],
                    snap["superseded_by"],
                    snap["status"],
                ),
            )
        for chunk_id in mention_snapshot:
            self._db.execute(
                "INSERT OR IGNORE INTO entity_mentions (entity_id, chunk_id) "
                "VALUES (?, ?)",
                (survivor_id, chunk_id),
            )

        self._recompute_degree(survivor_id)
        cur = self._db.execute(
            "INSERT INTO entity_merges "
            "(survivor_id, merged_id, merged_name, merged_type, merged_canonical, "
            " merged_description, triple_snapshot, mention_snapshot, merged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                survivor_id,
                merged_id,
                merged.name,
                merged.type,
                merged.canonical,
                merged.description,
                json.dumps(triple_snapshot),
                json.dumps(mention_snapshot),
                _now(),
            ),
        )
        self._db.commit()
        return cur.lastrowid

    def unmerge(self, merge_id: int) -> bool:
        """Undo a merge: restore the merged entity with its original facts.

        Facts that were remapped onto the survivor are moved back to the
        restored entity (the remapped copies are deleted), so a
        merge → unmerge round-trip leaves the graph as it was.
        """
        row = self._db.execute(
            "SELECT survivor_id, merged_id, merged_name, merged_type, merged_canonical, "
            "merged_description, triple_snapshot, mention_snapshot "
            "FROM entity_merges WHERE id = ? AND reverted_at IS NULL",
            (merge_id,),
        ).fetchone()
        if row is None:
            return False
        survivor_id, merged_id, name, etype, canonical, desc, tsnap, msnap = row
        if self.get_entity(merged_id) is not None:
            return False

        self._db.execute(
            "INSERT INTO entities (id, name, type, canonical, description, degree) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (merged_id, name, etype, canonical, desc),
        )
        for snap in json.loads(tsnap):
            head = merged_id if snap["head_id"] == merged_id else snap["head_id"]
            tail = merged_id if snap["tail_id"] == merged_id else snap["tail_id"]
            if head == tail:
                continue
            remap_head = survivor_id if snap["head_id"] == merged_id else snap["head_id"]
            remap_tail = survivor_id if snap["tail_id"] == merged_id else snap["tail_id"]
            self._db.execute(
                "DELETE FROM triples WHERE head_id = ? AND relation = ? AND tail_id = ? "
                "AND chunk_id = ?",
                (remap_head, snap["relation"], remap_tail, snap["chunk_id"]),
            )
            self._db.execute(
                "INSERT OR IGNORE INTO triples "
                "(head_id, relation, tail_id, chunk_id, description, strength, "
                " source_path, valid_from, valid_to, asserted_at, superseded_by, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    head,
                    snap["relation"],
                    tail,
                    snap["chunk_id"],
                    snap["description"],
                    snap["strength"],
                    snap["source_path"],
                    snap["valid_from"],
                    snap["valid_to"],
                    snap["asserted_at"],
                    snap["superseded_by"],
                    snap["status"],
                ),
            )
        for chunk_id in json.loads(msnap):
            self._db.execute(
                "INSERT OR IGNORE INTO entity_mentions (entity_id, chunk_id) VALUES (?, ?)",
                (merged_id, chunk_id),
            )
        self._db.execute(
            "UPDATE entity_merges SET reverted_at = ? WHERE id = ?",
            (_now(), merge_id),
        )
        self._recompute_degree(survivor_id)
        self._recompute_degree(merged_id)
        self._db.commit()
        return True

    def list_merges(self, reverted: bool = False) -> list[dict]:
        rows = self._db.execute(
            "SELECT m.id, m.survivor_id, m.merged_id, m.merged_name, "
            "m.merged_at, m.reverted_at, se.name, me.name IS NOT NULL "
            "FROM entity_merges m "
            "LEFT JOIN entities se ON se.id = m.survivor_id "
            "LEFT JOIN entities me ON me.id = m.merged_id "
            + (
                "WHERE m.reverted_at IS NOT NULL "
                if reverted
                else "WHERE m.reverted_at IS NULL "
            )
            + "ORDER BY m.merged_at DESC"
        ).fetchall()
        return [
            {
                "id": r[0],
                "survivor_id": r[1],
                "merged_id": r[2],
                "merged_name": r[3],
                "merged_at": r[4],
                "reverted_at": r[5],
                "survivor_name": r[6],
            }
            for r in rows
        ]

    def _recompute_degree(self, entity_id: int) -> None:
        self._db.execute(
            "UPDATE entities SET degree = ("
            "  SELECT COUNT(*) FROM triples t "
            "  WHERE t.status = 'active' "
            "  AND (t.head_id = entities.id OR t.tail_id = entities.id)"
            ") WHERE id = ?",
            (entity_id,),
        )
