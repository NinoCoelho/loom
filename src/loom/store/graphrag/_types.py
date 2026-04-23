"""GraphRAG types — configuration dataclasses and result models."""

from __future__ import annotations

from dataclasses import dataclass, field


_DEFAULT_ENTITY_TYPES = [
    "person",
    "project",
    "concept",
    "technology",
    "decision",
    "resource",
]

_DEFAULT_CORE_RELATIONS = [
    "uses",
    "depends_on",
    "part_of",
    "created_by",
    "related_to",
]


@dataclass
class OntologyConfig:
    entity_types: list[str] = field(default_factory=lambda: list(_DEFAULT_ENTITY_TYPES))
    core_relations: list[str] = field(default_factory=lambda: list(_DEFAULT_CORE_RELATIONS))
    allow_custom_relations: bool = True
    aliases: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class EmbeddingConfig:
    provider: str = "ollama"
    model: str = "nomic-embed-text"
    base_url: str = "http://localhost:11434"
    key_env: str = ""
    dimensions: int = 768


@dataclass
class ExtractionConfig:
    model: str | None = None
    max_gleanings: int = 1


@dataclass
class GraphRAGConfig:
    enabled: bool = False
    embeddings: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    ontology: OntologyConfig = field(default_factory=OntologyConfig)
    max_hops: int = 2
    context_budget: int = 3000
    top_k: int = 10
    chunk_size: int = 1000
    chunk_overlap: int = 100


@dataclass
class Chunk:
    id: str
    source_path: str
    heading: str
    content: str
    char_offset: int


@dataclass
class RetrievalResult:
    chunk_id: str
    source_path: str
    heading: str
    content: str
    score: float
    source: str
    related_entities: list[str] = field(default_factory=list)


@dataclass
class HopRecord:
    from_entity: str
    to_entity: str
    relation: str
    hop_depth: int


@dataclass
class RetrievalTrace:
    seed_entities: list[str] = field(default_factory=list)
    hops: list[HopRecord] = field(default_factory=list)
    expanded_entity_ids: list[int] = field(default_factory=list)


@dataclass
class EnrichedRetrieval:
    results: list[RetrievalResult] = field(default_factory=list)
    trace: RetrievalTrace = field(default_factory=RetrievalTrace)
    subgraph_nodes: list[dict] = field(default_factory=list)
    subgraph_edges: list[dict] = field(default_factory=list)
