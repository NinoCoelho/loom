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

from loom.store.graph._graph import EntityGraph as EntityGraph
from loom.store.graph._types import Entity as Entity
from loom.store.graph._types import Triple as Triple

__all__ = ["Entity", "EntityGraph", "Triple"]
