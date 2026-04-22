"""SQLite-backed vector store for embedding storage and similarity search.

Stores float32 vectors as BLOBs and uses either numpy (when available) or
pure-Python fallback for cosine similarity computation.  Designed for the
personal-knowledge-base scale (hundreds to low-thousands of chunks) where
brute-force search is fast enough and avoids external vector-database
dependencies.
"""

from __future__ import annotations

import json
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from loom.store.embeddings import _batch_cosine


@dataclass
class VectorHit:
    id: str
    source: str
    score: float
    metadata: dict = field(default_factory=dict)


def _pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _unpack_vector(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


class VectorStore:
    """SQLite-backed vector store with cosine-similarity search."""

    def __init__(self, db_path: Path, dim: int = 768) -> None:
        self._path = db_path
        self._dim = dim
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._closed = False
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS vectors (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                embedding BLOB NOT NULL,
                metadata TEXT,
                updated_at REAL
            )
        """)
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_vectors_source ON vectors(source)"
        )
        self._db.commit()

    def close(self) -> None:
        if self._closed:
            return
        self._db.close()
        self._closed = True

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def upsert(
        self,
        id: str,
        embedding: list[float],
        *,
        source: str = "",
        metadata: dict | None = None,
    ) -> None:
        blob = _pack_vector(embedding)
        meta_str = json.dumps(metadata) if metadata else None
        self._db.execute(
            "INSERT OR REPLACE INTO vectors (id, source, embedding, metadata, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (id, source, blob, meta_str, time.time()),
        )
        self._db.commit()

    def remove(self, id: str) -> None:
        self._db.execute("DELETE FROM vectors WHERE id = ?", (id,))
        self._db.commit()

    def remove_for_source(self, source: str) -> int:
        cur = self._db.execute(
            "DELETE FROM vectors WHERE source = ?", (source,)
        )
        self._db.commit()
        return cur.rowcount

    def search(
        self,
        query_embedding: list[float],
        *,
        top_k: int = 20,
        source_filter: str | None = None,
    ) -> list[VectorHit]:
        if source_filter is not None:
            rows = self._db.execute(
                "SELECT id, source, embedding, metadata FROM vectors WHERE source = ?",
                (source_filter,),
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT id, source, embedding, metadata FROM vectors"
            ).fetchall()

        if not rows:
            return []

        ids = [r[0] for r in rows]
        sources = [r[1] for r in rows]
        vectors = [_unpack_vector(r[2]) for r in rows]
        metas = [json.loads(r[3]) if r[3] else {} for r in rows]

        scores = _batch_cosine(query_embedding, vectors)

        scored = list(zip(ids, sources, metas, scores))
        scored.sort(key=lambda t: t[3], reverse=True)

        return [
            VectorHit(id=id_, source=src, score=sc, metadata=meta)
            for id_, src, meta, sc in scored[:top_k]
        ]

    def get(self, id: str) -> VectorHit | None:
        row = self._db.execute(
            "SELECT id, source, embedding, metadata FROM vectors WHERE id = ?",
            (id,),
        ).fetchone()
        if row is None:
            return None
        return VectorHit(
            id=row[0],
            source=row[1],
            score=0.0,
            metadata=json.loads(row[3]) if row[3] else {},
        )

    def get_embedding(self, id: str) -> list[float] | None:
        row = self._db.execute(
            "SELECT embedding FROM vectors WHERE id = ?",
            (id,),
        ).fetchone()
        if row is None:
            return None
        return _unpack_vector(row[0])

    def count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM vectors").fetchone()
        return row[0] if row else 0

    def sources(self) -> list[str]:
        rows = self._db.execute(
            "SELECT DISTINCT source FROM vectors ORDER BY source"
        ).fetchall()
        return [r[0] for r in rows]
