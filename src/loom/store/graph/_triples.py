from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from loom.store.graph._types import Triple


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_TRIPLE_DDL = """
CREATE TABLE IF NOT EXISTS triples (
    id INTEGER PRIMARY KEY,
    head_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    relation TEXT NOT NULL,
    tail_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    description TEXT DEFAULT '',
    strength REAL DEFAULT 5.0,
    source_path TEXT DEFAULT '',
    valid_from TEXT,
    valid_to TEXT,
    asserted_at TEXT,
    superseded_by INTEGER,
    status TEXT DEFAULT 'active',
    UNIQUE(head_id, relation, tail_id, chunk_id)
);

CREATE TABLE IF NOT EXISTS entity_mentions (
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    chunk_id TEXT NOT NULL,
    PRIMARY KEY(entity_id, chunk_id)
);

CREATE TABLE IF NOT EXISTS conflicts (
    id INTEGER PRIMARY KEY,
    triple_id_a INTEGER NOT NULL REFERENCES triples(id) ON DELETE CASCADE,
    triple_id_b INTEGER NOT NULL REFERENCES triples(id) ON DELETE CASCADE,
    head_id INTEGER NOT NULL,
    relation TEXT NOT NULL,
    kind TEXT DEFAULT 'fact_contradiction',
    detected_at TEXT,
    resolved_at TEXT,
    resolution TEXT
);

CREATE INDEX IF NOT EXISTS idx_triples_head ON triples(head_id);
CREATE INDEX IF NOT EXISTS idx_triples_tail ON triples(tail_id);
CREATE INDEX IF NOT EXISTS idx_triples_chunk ON triples(chunk_id);
CREATE INDEX IF NOT EXISTS idx_mentions_chunk ON entity_mentions(chunk_id);
"""

_TRIPLE_COLUMNS: dict[str, str] = {
    "source_path": "TEXT DEFAULT ''",
    "valid_from": "TEXT",
    "valid_to": "TEXT",
    "asserted_at": "TEXT",
    "superseded_by": "INTEGER",
    "status": "TEXT DEFAULT 'active'",
}

_TRIPLE_SELECT = (
    "id, head_id, relation, tail_id, chunk_id, description, strength, "
    "source_path, valid_from, valid_to, asserted_at, superseded_by, status "
    "FROM triples"
)


def _row_to_triple(r: tuple) -> Triple:
    return Triple(
        id=r[0],
        head_id=r[1],
        relation=r[2],
        tail_id=r[3],
        chunk_id=r[4],
        description=r[5],
        strength=r[6],
        source_path=r[7] or "",
        valid_from=r[8],
        valid_to=r[9],
        asserted_at=r[10],
        superseded_by=r[11],
        status=r[12] or "active",
    )


class TripleRepository:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db
        self._db.executescript(_TRIPLE_DDL)
        self._migrate_triple_columns()

    def _migrate_triple_columns(self) -> None:
        cols = [r[1] for r in self._db.execute("PRAGMA table_info(triples)").fetchall()]
        for col, decl in _TRIPLE_COLUMNS.items():
            if col not in cols:
                self._db.execute(f"ALTER TABLE triples ADD COLUMN {col} {decl}")
        self._db.execute("UPDATE triples SET status = 'active' WHERE status IS NULL")
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_triples_status ON triples(status)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_conflicts_open ON conflicts(resolved_at)"
        )
        self._db.commit()

    def _validity_overlaps(self, a: Triple, valid_from: str | None, valid_to: str | None) -> bool:
        """True when two fact versions may hold at the same time (unknown = overlaps)."""
        if a.valid_from is None and a.valid_to is None and valid_from is None and valid_to is None:
            return True
        a_from, a_to = a.valid_from or "", a.valid_to or "9999"
        b_from, b_to = valid_from or "", valid_to or "9999"
        if not b_from:
            b_from = "0000"
        if not a_from:
            a_from = "0000"
        return a_from <= b_to and b_from <= a_to

    def add_triple(
        self,
        head_id: int,
        relation: str,
        tail_id: int,
        chunk_id: str,
        description: str = "",
        strength: float = 5.0,
        source_path: str = "",
        valid_from: str | None = None,
        valid_to: str | None = None,
        conflict_detection: bool = True,
    ) -> tuple[int | None, int | None]:
        """Insert a fact. Returns ``(triple_id, conflict_id)``.

        ``triple_id`` is None when the fact is an exact duplicate (ignored).
        ``conflict_id`` is set when the new fact contradicts an active fact
        from a different source file; the new fact is then stored with
        status ``pending`` for human review instead of going live.
        """
        cur = self._db.execute(
            "INSERT OR IGNORE INTO triples "
            "(head_id, relation, tail_id, chunk_id, description, strength, "
            " source_path, valid_from, valid_to, asserted_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
            (
                head_id,
                relation,
                tail_id,
                chunk_id,
                description,
                strength,
                source_path,
                valid_from,
                valid_to,
                _now(),
            ),
        )
        new_id = cur.lastrowid if cur.rowcount > 0 else None
        if new_id is None:
            return None, None

        conflict_id: int | None = None
        if conflict_detection and source_path:
            rows = self._db.execute(
                f"SELECT {_TRIPLE_SELECT} WHERE head_id = ? AND relation = ? "
                "AND id != ? AND status IN ('active', 'pending') AND tail_id != ? "
                "AND source_path != '' AND source_path != ?",
                (head_id, relation, new_id, tail_id, source_path),
            ).fetchall()
            for r in rows:
                existing = _row_to_triple(r)
                if not self._validity_overlaps(existing, valid_from, valid_to):
                    continue
                self._db.execute(
                    "UPDATE triples SET status = 'pending' WHERE id = ?",
                    (new_id,),
                )
                ccur = self._db.execute(
                    "INSERT INTO conflicts "
                    "(triple_id_a, triple_id_b, head_id, relation, kind, detected_at) "
                    "VALUES (?, ?, ?, ?, 'fact_contradiction', ?)",
                    (existing.id, new_id, head_id, relation, _now()),
                )
                conflict_id = ccur.lastrowid
                break

        status_row = self._db.execute(
            "SELECT status FROM triples WHERE id = ?", (new_id,)
        ).fetchone()
        if status_row and status_row[0] == "active":
            self._db.execute(
                "UPDATE entities SET degree = degree + 1 WHERE id IN (?, ?)",
                (head_id, tail_id),
            )
        self._db.commit()
        return new_id, conflict_id

    def get_triple(self, triple_id: int) -> Triple | None:
        row = self._db.execute(
            f"SELECT {_TRIPLE_SELECT} WHERE id = ?", (triple_id,)
        ).fetchone()
        return _row_to_triple(row) if row else None

    def set_triple_status(self, triple_id: int, status: str) -> None:
        self._db.execute(
            "UPDATE triples SET status = ? WHERE id = ?", (status, triple_id)
        )
        head_tail = self._db.execute(
            "SELECT head_id, tail_id FROM triples WHERE id = ?", (triple_id,)
        ).fetchone()
        if head_tail:
            self._recompute_degrees([head_tail[0], head_tail[1]])
        self._db.commit()

    def _recompute_degrees(self, entity_ids: list[int]) -> None:
        for eid in entity_ids:
            self._db.execute(
                "UPDATE entities SET degree = ("
                "  SELECT COUNT(*) FROM triples t "
                "  WHERE t.status = 'active' "
                "  AND (t.head_id = entities.id OR t.tail_id = entities.id)"
                ") WHERE id = ?",
                (eid,),
            )

    def list_conflicts(self, resolved: bool = False) -> list[dict]:
        filter_clause = (
            "WHERE c.resolved_at IS NOT NULL " if resolved else "WHERE c.resolved_at IS NULL "
        )
        rows = self._db.execute(
            "SELECT c.id, c.triple_id_a, c.triple_id_b, c.head_id, c.relation, "
            "c.kind, c.detected_at, c.resolved_at, c.resolution, "
            "eh.name, et_old.name, et_new.name "
            "FROM conflicts c "
            "JOIN triples ta ON ta.id = c.triple_id_a "
            "JOIN triples tb ON tb.id = c.triple_id_b "
            "JOIN entities eh ON eh.id = c.head_id "
            "JOIN entities et_old ON et_old.id = ta.tail_id "
            "JOIN entities et_new ON et_new.id = tb.tail_id "
            + filter_clause
            + "ORDER BY c.detected_at DESC"
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            a = self.get_triple(r[1])
            b = self.get_triple(r[2])
            if a is None or b is None:
                continue
            out.append(
                {
                    "id": r[0],
                    "triple_a": a.__dict__,
                    "triple_b": b.__dict__,
                    "head": r[9],
                    "old_tail": r[10],
                    "new_tail": r[11],
                    "relation": r[4],
                    "kind": r[5],
                    "detected_at": r[6],
                    "resolved_at": r[7],
                    "resolution": r[8],
                }
            )
        return out

    def resolve_conflict(self, conflict_id: int, resolution: str) -> bool:
        """Resolve a conflict: ``approve_new`` | ``reject_new`` | ``keep_both``."""
        row = self._db.execute(
            "SELECT triple_id_a, triple_id_b FROM conflicts "
            "WHERE id = ? AND resolved_at IS NULL",
            (conflict_id,),
        ).fetchone()
        if row is None:
            return False
        old_id, new_id = row[0], row[1]
        if resolution == "approve_new":
            new = self.get_triple(new_id)
            self._db.execute(
                "UPDATE triples SET status = 'superseded', superseded_by = ?, "
                "valid_to = COALESCE(?, valid_to) WHERE id = ? AND status = 'active'",
                (new_id, new.valid_from if new else None, old_id),
            )
            self._db.execute(
                "UPDATE triples SET status = 'active' WHERE id = ? AND status = 'pending'",
                (new_id,),
            )
        elif resolution == "reject_new":
            self._db.execute(
                "UPDATE triples SET status = 'rejected' WHERE id = ? AND status = 'pending'",
                (new_id,),
            )
        elif resolution == "keep_both":
            self._db.execute(
                "UPDATE triples SET status = 'active' WHERE id = ? AND status = 'pending'",
                (new_id,),
            )
        else:
            return False
        self._db.execute(
            "UPDATE conflicts SET resolved_at = ?, resolution = ? WHERE id = ?",
            (_now(), resolution, conflict_id),
        )
        involved = {old_id, new_id}
        ids: set[int] = set()
        for tid in involved:
            t = self.get_triple(tid)
            if t:
                ids.update((t.head_id, t.tail_id))
        self._recompute_degrees(list(ids))
        self._db.commit()
        return True

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
            "DELETE FROM conflicts WHERE triple_id_a IN "
            f"(SELECT id FROM triples WHERE chunk_id IN ({placeholders})) "
            f"OR triple_id_b IN (SELECT id FROM triples WHERE chunk_id IN ({placeholders}))",
            chunk_ids + chunk_ids,
        )
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
                f"  WHERE t.status = 'active' "
                f"  AND (t.head_id = entities.id OR t.tail_id = entities.id)"
                f") WHERE id IN ({ph})",
                affected_ids,
            )
        self._db.commit()

    def remove_for_source(self, source: str, chunk_ids: list[str]) -> None:
        self.remove_for_chunks(chunk_ids)
