from __future__ import annotations

import sqlite3

from loom.store.graph._types import Entity, Triple


class GraphQueries:
    def __init__(self, db: sqlite3.Connection) -> None:
        self._db = db

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
