"""GraphRAG chunk indexing — chunking, embedding, and vector storage."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from loom.store.graphrag._chunking import chunk_markdown
from loom.store.graphrag._types import Chunk, GraphRAGConfig
from loom.store.vector import VectorStore

logger = logging.getLogger(__name__)


class GraphRAGIndexer:
    def __init__(
        self,
        config: GraphRAGConfig,
        embedding_provider: Any,
        *,
        db_dir: Path,
        entity_graph: Any,
    ) -> None:
        self._config = config
        self._embedder = embedding_provider
        self._entity_graph = entity_graph
        db_dir.mkdir(parents=True, exist_ok=True)
        self._vector_store = VectorStore(
            db_dir / "graphrag_vectors.sqlite", dim=embedding_provider.dim
        )
        self._chunk_db = self._open_chunk_db(db_dir / "graphrag_chunks.sqlite")

    @staticmethod
    def _open_chunk_db(db_path: Path) -> sqlite3.Connection:
        db = sqlite3.connect(str(db_path), check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                heading TEXT DEFAULT '',
                content TEXT NOT NULL,
                char_offset INTEGER DEFAULT 0,
                indexed_at REAL
            )
        """)
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_path)"
        )
        db.commit()
        return db

    def chunk_text(self, text: str, source_path: str) -> list[Chunk]:
        return chunk_markdown(
            text,
            source_path,
            max_size=self._config.chunk_size,
            overlap=self._config.chunk_overlap,
        )

    async def index_source(self, path: str, content: str) -> list[Chunk]:
        chunks = self.chunk_text(content, path)
        if not chunks:
            return chunks

        old_ids = self._chunk_ids_for_source(path)
        if old_ids:
            self._entity_graph.remove_for_chunks(old_ids)
            for cid in old_ids:
                self._vector_store.remove(cid)
            placeholders = ",".join("?" for _ in old_ids)
            self._chunk_db.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", old_ids)
            self._chunk_db.commit()

        texts = [c.content for c in chunks]
        embeddings = await self._embedder.embed(texts)

        for chunk, emb in zip(chunks, embeddings):
            self._chunk_db.execute(
                "INSERT OR REPLACE INTO chunks "
                "(id, source_path, heading, content, char_offset, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk.id,
                    chunk.source_path,
                    chunk.heading,
                    chunk.content,
                    chunk.char_offset,
                    time.time(),
                ),
            )
            self._vector_store.upsert(
                chunk.id,
                emb,
                source=chunk.source_path,
                metadata={"heading": chunk.heading, "offset": chunk.char_offset},
            )
        self._chunk_db.commit()
        return chunks

    def remove_source(self, source_path: str) -> None:
        old_ids = self._chunk_ids_for_source(source_path)
        if not old_ids:
            return
        self._entity_graph.remove_for_chunks(old_ids)
        for cid in old_ids:
            self._vector_store.remove(cid)
        placeholders = ",".join("?" for _ in old_ids)
        self._chunk_db.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", old_ids)
        self._chunk_db.commit()

    def _chunk_ids_for_source(self, source_path: str) -> list[str]:
        rows = self._chunk_db.execute(
            "SELECT id FROM chunks WHERE source_path = ?", (source_path,)
        ).fetchall()
        return [r[0] for r in rows]

    def _get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        row = self._chunk_db.execute(
            "SELECT source_path, heading, content, char_offset FROM chunks WHERE id = ?",
            (chunk_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "source_path": row[0],
            "heading": row[1],
            "content": row[2],
            "char_offset": row[3],
        }
