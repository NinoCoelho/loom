"""GraphRAG engine — chunking, entity extraction, hybrid retrieval, and context injection."""

from loom.store.graphrag._chunking import chunk_markdown
from loom.store.graphrag._engine import GraphRAGEngine
from loom.store.graphrag._extraction import (
    ontology_relation_ok,
    parse_extraction_response,
    parse_temporal,
)
from loom.store.graphrag._resolution import EntityResolver
from loom.store.graphrag._types import (
    Chunk,
    ConflictConfig,
    EmbeddingConfig,
    EnrichedRetrieval,
    ExtractionConfig,
    GraphRAGConfig,
    HopRecord,
    OntologyConfig,
    ResolutionConfig,
    RetrievalResult,
    RetrievalTrace,
)

__all__ = [
    "Chunk",
    "ConflictConfig",
    "EmbeddingConfig",
    "EnrichedRetrieval",
    "EntityResolver",
    "ExtractionConfig",
    "GraphRAGConfig",
    "GraphRAGEngine",
    "HopRecord",
    "OntologyConfig",
    "ResolutionConfig",
    "RetrievalResult",
    "RetrievalTrace",
    "chunk_markdown",
    "ontology_relation_ok",
    "parse_extraction_response",
    "parse_temporal",
]
