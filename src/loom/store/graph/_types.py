from __future__ import annotations

from dataclasses import dataclass


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
    source_path: str = ""
    valid_from: str | None = None
    valid_to: str | None = None
    asserted_at: str | None = None
    superseded_by: int | None = None
    status: str = "active"
