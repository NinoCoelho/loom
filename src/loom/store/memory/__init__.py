"""Agent memory store — markdown-on-disk + SQLite FTS5 + salience signals.

When a :class:`VaultProvider` is supplied, the store delegates file I/O and
FTS5 search to the vault (reads/writes land under ``<vault_prefix>/``).
The standalone path (no vault) retains the original local-disk + SQLite
behaviour and is the default.
"""

from loom.store.memory._core import (
    EmbeddingProvider,
    MemoryEntry,
    MemoryStore,
    RecallHit,
    SearchHit,
)
from loom.store.memory._vault_backend import VaultMemoryBackend

__all__ = [
    "EmbeddingProvider",
    "MemoryEntry",
    "MemoryStore",
    "RecallHit",
    "SearchHit",
    "VaultMemoryBackend",
]
