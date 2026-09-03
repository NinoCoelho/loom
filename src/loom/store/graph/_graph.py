from __future__ import annotations

from pathlib import Path

from loom.store.db import SqliteResource
from loom.store.graph._entities import EntityRepository
from loom.store.graph._queries import GraphQueries
from loom.store.graph._triples import TripleRepository


class EntityGraph(SqliteResource):
    """SQLite-backed entity-relationship graph."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._db = self._init_db(db_path)
        self._db.execute("PRAGMA foreign_keys=ON")
        self._entities = EntityRepository(self._db)
        self._triples = TripleRepository(self._db)
        self._entities._migrate_degree_column()
        self._db.commit()
        self._queries = GraphQueries(self._db)

    def resolve_entity(
        self,
        name: str,
        type: str,
        aliases: dict[str, list[str]] | None = None,
    ) -> int:
        return self._entities.resolve_entity(name, type, aliases)

    def get_entity(self, entity_id: int):
        return self._entities.get_entity(entity_id)

    def find_entity(self, name: str, type: str):
        return self._entities.find_entity(name, type)

    def set_entity_description(self, entity_id: int, description: str) -> None:
        return self._entities.set_entity_description(entity_id, description)

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
        return self._triples.add_triple(
            head_id,
            relation,
            tail_id,
            chunk_id,
            description,
            strength,
            source_path,
            valid_from,
            valid_to,
            conflict_detection,
        )

    def get_triple(self, triple_id: int):
        return self._triples.get_triple(triple_id)

    def list_conflicts(self, resolved: bool = False) -> list[dict]:
        return self._triples.list_conflicts(resolved)

    def resolve_conflict(self, conflict_id: int, resolution: str) -> bool:
        return self._triples.resolve_conflict(conflict_id, resolution)

    def merge_entities(self, survivor_id: int, merged_id: int) -> int | None:
        return self._entities.merge_entities(survivor_id, merged_id)

    def unmerge(self, merge_id: int) -> bool:
        return self._entities.unmerge(merge_id)

    def list_merges(self, reverted: bool = False) -> list[dict]:
        return self._entities.list_merges(reverted)

    def add_mention(self, entity_id: int, chunk_id: str) -> None:
        return self._triples.add_mention(entity_id, chunk_id)

    def remove_for_chunks(self, chunk_ids: list[str]) -> None:
        return self._triples.remove_for_chunks(chunk_ids)

    def remove_for_source(self, source: str, chunk_ids: list[str]) -> None:
        return self._triples.remove_for_source(source, chunk_ids)

    def entities_for_chunk(self, chunk_id: str):
        return self._queries.entities_for_chunk(chunk_id)

    def chunks_for_entity(self, entity_id: int) -> list[str]:
        return self._queries.chunks_for_entity(entity_id)

    def neighbors(self, entity_id: int, max_hops: int = 2):
        return self._queries.neighbors(entity_id, max_hops)

    def count_entities(self) -> int:
        return self._queries.count_entities()

    def count_triples(self) -> int:
        return self._queries.count_triples()

    def list_entities(
        self,
        entity_type: str | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        return self._queries.list_entities(entity_type, search, limit, offset)

    def get_entity_triples(self, entity_id: int):
        return self._queries.get_entity_triples(entity_id)

    def subgraph(
        self,
        seed_id: int,
        max_hops: int = 2,
        max_nodes: int = 200,
        max_neighbors_per_node: int = 50,
    ) -> dict:
        return self._queries.subgraph(seed_id, max_hops, max_nodes, max_neighbors_per_node)

    def connected_components(self) -> list[list[int]]:
        return self._queries.connected_components()

    def entity_degree(self, entity_id: int) -> int:
        return self._queries.entity_degree(entity_id)

    def entity_counts_by_type(self) -> dict[str, int]:
        return self._queries.entity_counts_by_type()

    def list_all_entities(self):
        return self._queries.list_all_entities()

    def list_all_triples(self):
        return self._queries.list_all_triples()
