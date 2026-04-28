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

from dataclasses import dataclass
from pathlib import Path

from loom.store.db import SqliteResource


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
    degree INTEGER NOT NULL DEFAULT 0,
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


class EntityGraph(SqliteResource):
    """SQLite-backed entity-relationship graph."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._db = self._init_db(db_path)
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(_SCHEMA)
        self._migrate_degree_column()
        self._db.commit()

    def _migrate_degree_column(self) -> None:
        """Backfill ``entities.degree`` on databases created before the
        column existed. Idempotent: no-op once the column + index are in
        place. Without the denormalised count, ``list_entities``'s
        ``ORDER BY`` falls back to a correlated subquery that scales O(N²)."""
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
        # Index lives outside _SCHEMA because CREATE INDEX would fire
        # before the migration adds the column on legacy databases.
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
        # Bump denormalised degree only when a new row was actually inserted
        # (UNIQUE conflicts on the same chunk-mention return rowcount=0).
        # ``id IN (h, t)`` dedupes self-loops to one update — same as the
        # original ``COUNT(*) WHERE head_id=? OR tail_id=?`` semantics.
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

    def entities_for_chunk(self, chunk_id: str) -> list[Entity]:
        rows = self._db.execute(
            "SELECT e.id, e.name, e.type, e.canonical, e.description "
            "FROM entities e JOIN entity_mentions em ON e.id = em.entity_id "
            "WHERE em.chunk_id = ?",
            (chunk_id,),
        ).fetchall()
        return [
            Entity(id=r[0], name=r[1], type=r[2], canonical=r[3], description=r[4]) for r in rows
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
                    "SELECT head_id, tail_id FROM triples WHERE head_id = ? OR tail_id = ?",
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
            Entity(id=r[0], name=r[1], type=r[2], canonical=r[3], description=r[4]) for r in rows
        ]

    def remove_for_chunks(self, chunk_ids: list[str]) -> None:
        if not chunk_ids:
            return
        placeholders = ",".join("?" for _ in chunk_ids)
        # Snapshot endpoints of triples about to be deleted so we can refresh
        # their denormalised degree afterward. Cheaper than rebuilding all
        # entities' degrees (which is O(N²) at scale).
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

    def count_entities(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM entities").fetchone()
        return row[0] if row else 0

    def count_triples(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM triples").fetchone()
        return row[0] if row else 0

    def list_entities(
        self,
        entity_type: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Entity]:
        clauses: list[str] = []
        params: list[object] = []
        if entity_type is not None:
            clauses.append("type = ?")
            params.append(entity_type)
        if search is not None:
            clauses.append("name LIKE ?")
            params.append(f"%{search}%")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._db.execute(
            f"SELECT e.id, e.name, e.type, e.canonical, e.description "
            f"FROM entities e"
            f"{where} "
            f"ORDER BY e.degree DESC "
            f"LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        return [
            Entity(id=r[0], name=r[1], type=r[2], canonical=r[3], description=r[4]) for r in rows
        ]

    def get_entity_triples(self, entity_id: int) -> list[Triple]:
        rows = self._db.execute(
            "SELECT id, head_id, relation, tail_id, chunk_id, description, strength "
            "FROM triples WHERE head_id = ? OR tail_id = ?",
            (entity_id, entity_id),
        ).fetchall()
        return [
            Triple(
                id=r[0],
                head_id=r[1],
                relation=r[2],
                tail_id=r[3],
                chunk_id=r[4],
                description=r[5],
                strength=r[6],
            )
            for r in rows
        ]

    def subgraph(
        self,
        seed_id: int,
        max_hops: int = 2,
        max_nodes: int = 200,
        max_neighbors_per_node: int = 50,
    ) -> dict:
        """BFS expansion of a seed entity's neighbourhood.

        ``max_neighbors_per_node`` caps how many triples a single hub
        contributes per visit — without it, exploring a 5000-edge hub like
        "Microsoft" pulls every triple even though only ``max_nodes`` are
        ever returned. Strongest triples win (``ORDER BY strength DESC``).
        """
        visited_ids: set[int] = {seed_id}
        frontier: set[int] = {seed_id}
        seen_triple_ids: set[int] = set()
        all_triples: list[Triple] = []

        for _ in range(max_hops):
            next_frontier: set[int] = set()
            for eid in frontier:
                if len(visited_ids) >= max_nodes:
                    break
                rows = self._db.execute(
                    "SELECT id, head_id, relation, tail_id, chunk_id, description, strength "
                    "FROM triples WHERE head_id = ? OR tail_id = ? "
                    "ORDER BY strength DESC LIMIT ?",
                    (eid, eid, max_neighbors_per_node),
                ).fetchall()
                for r in rows:
                    if r[0] in seen_triple_ids:
                        continue
                    seen_triple_ids.add(r[0])
                    t = Triple(
                        id=r[0],
                        head_id=r[1],
                        relation=r[2],
                        tail_id=r[3],
                        chunk_id=r[4],
                        description=r[5],
                        strength=r[6],
                    )
                    all_triples.append(t)
                    other = t.tail_id if t.head_id == eid else t.head_id
                    if other not in visited_ids and len(visited_ids) < max_nodes:
                        visited_ids.add(other)
                        next_frontier.add(other)
            frontier = next_frontier
            if not frontier:
                break

        if not visited_ids:
            return {"nodes": [], "edges": []}
        placeholders = ",".join("?" for _ in visited_ids)
        rows = self._db.execute(
            f"SELECT id, name, type, canonical, description FROM entities "
            f"WHERE id IN ({placeholders})",
            list(visited_ids),
        ).fetchall()
        nodes = [
            Entity(id=r[0], name=r[1], type=r[2], canonical=r[3], description=r[4]) for r in rows
        ]
        return {"nodes": nodes, "edges": all_triples}

    def connected_components(self) -> list[list[int]]:
        parent: dict[int, int] = {}

        def find(x: int) -> int:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        rows = self._db.execute("SELECT id FROM entities").fetchall()
        for r in rows:
            parent[r[0]] = r[0]

        triples = self._db.execute("SELECT head_id, tail_id FROM triples").fetchall()
        for h, t in triples:
            union(h, t)

        groups: dict[int, list[int]] = {}
        for eid in parent:
            root = find(eid)
            groups.setdefault(root, []).append(eid)

        return sorted(groups.values(), key=len, reverse=True)

    def entity_degree(self, entity_id: int) -> int:
        row = self._db.execute(
            "SELECT degree FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        return row[0] if row else 0

    def entity_counts_by_type(self) -> dict[str, int]:
        rows = self._db.execute(
            "SELECT type, COUNT(*) FROM entities GROUP BY type ORDER BY COUNT(*) DESC"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def list_all_entities(self) -> list[Entity]:
        rows = self._db.execute(
            "SELECT id, name, type, canonical, description FROM entities ORDER BY name"
        ).fetchall()
        return [
            Entity(id=r[0], name=r[1], type=r[2], canonical=r[3], description=r[4]) for r in rows
        ]

    def list_all_triples(self) -> list[Triple]:
        rows = self._db.execute(
            "SELECT id, head_id, relation, tail_id, chunk_id, description, strength FROM triples"
        ).fetchall()
        return [
            Triple(
                id=r[0],
                head_id=r[1],
                relation=r[2],
                tail_id=r[3],
                chunk_id=r[4],
                description=r[5],
                strength=r[6],
            )
            for r in rows
        ]

    def set_entity_description(self, entity_id: int, description: str) -> None:
        self._db.execute(
            "UPDATE entities SET description = ? WHERE id = ?",
            (description, entity_id),
        )
        self._db.commit()
