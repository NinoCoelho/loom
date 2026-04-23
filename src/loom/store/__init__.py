"""Storage layer — secrets, memory, graph, vector, session, vault, and cookies.

Provides pluggable backends for the core storage primitives used by
the agent runtime:

* **Secrets** — typed secrets (:class:`Secret`) resolved by an
  :class:`Applier` and persisted via :class:`SecretsStore` or
  :class:`SecretStore` (keychain-backed).
* **Memory** — a dual-layer store (:class:`MemoryStore`) with full-text
  search (FTS5) and salience-weighted recall.
* **Graph** — entity knowledge graph (:class:`EntityGraph`) backed by
  SQLite, with optional GraphRAG enrichment (:class:`GraphRAGEngine`).
* **Vector** — nearest-neighbour retrieval (:class:`VectorStore`).
* **Session** — SQLite-backed session history (:class:`SessionStore`).
* **Vault** — typed file vault (:class:`VaultProvider`) for binary or
  structured file storage.
* **Cookies** — domain-keyed cookie persistence (:class:`CookieStore`)
  for web scraping auth.

Embedding providers (:class:`OpenAIEmbeddingProvider`,
:class:`OllamaEmbeddingProvider`) are also re-exported for use by
the memory and vector stores.
"""

from loom.store.atomic import atomic_write as atomic_write
from loom.store.cookies import (
    CookieStore as CookieStore,
)
from loom.store.cookies import (
    FilesystemCookieStore as FilesystemCookieStore,
)
from loom.store.embeddings import (
    OllamaEmbeddingProvider as OllamaEmbeddingProvider,
)
from loom.store.embeddings import (
    OpenAIEmbeddingProvider as OpenAIEmbeddingProvider,
)
from loom.store.graph import (
    Entity as Entity,
)
from loom.store.graph import (
    EntityGraph as EntityGraph,
)
from loom.store.graph import (
    Triple as Triple,
)
from loom.store.graphrag import (
    Chunk as Chunk,
)
from loom.store.graphrag import (
    EnrichedRetrieval as EnrichedRetrieval,
)
from loom.store.graphrag import (
    GraphRAGConfig as GraphRAGConfig,
)
from loom.store.graphrag import (
    GraphRAGEngine as GraphRAGEngine,
)
from loom.store.graphrag import (
    HopRecord as HopRecord,
)
from loom.store.graphrag import (
    RetrievalResult as RetrievalResult,
)
from loom.store.graphrag import (
    RetrievalTrace as RetrievalTrace,
)
from loom.store.graphrag import (
    chunk_markdown as chunk_markdown,
)
from loom.store.keychain import KeychainStore as KeychainStore
from loom.store.memory import (
    MemoryEntry as MemoryEntry,
)
from loom.store.memory import (
    MemoryStore as MemoryStore,
)
from loom.store.memory import (
    SearchHit as SearchHit,
)
from loom.store.secrets import ApiKeySecret as ApiKeySecret
from loom.store.secrets import AwsSigV4Secret as AwsSigV4Secret
from loom.store.secrets import BasicAuthSecret as BasicAuthSecret
from loom.store.secrets import BearerTokenSecret as BearerTokenSecret
from loom.store.secrets import JwtSigningKeySecret as JwtSigningKeySecret
from loom.store.secrets import OAuth2ClientCredentialsSecret as OAuth2ClientCredentialsSecret
from loom.store.secrets import PasswordSecret as PasswordSecret
from loom.store.secrets import Secret as Secret
from loom.store.secrets import SecretMetadata as SecretMetadata
from loom.store.secrets import SecretsStore as SecretsStore
from loom.store.secrets import SecretStore as SecretStore
from loom.store.secrets import SshPrivateKeySecret as SshPrivateKeySecret
from loom.store.session import SessionStore as SessionStore
from loom.store.vault import (
    FilesystemVaultProvider as FilesystemVaultProvider,
)
from loom.store.vault import (
    VaultProvider as VaultProvider,
)
from loom.store.vault import (
    VaultStore as VaultStore,
)
from loom.store.vector import (
    VectorHit as VectorHit,
)
from loom.store.vector import (
    VectorStore as VectorStore,
)

__all__ = [
    "atomic_write",
    "ApiKeySecret",
    "AwsSigV4Secret",
    "BasicAuthSecret",
    "BearerTokenSecret",
    "Chunk",
    "CookieStore",
    "Entity",
    "EntityGraph",
    "EnrichedRetrieval",
    "GraphRAGConfig",
    "GraphRAGEngine",
    "HopRecord",
    "JwtSigningKeySecret",
    "KeychainStore",
    "MemoryEntry",
    "MemoryStore",
    "OAuth2ClientCredentialsSecret",
    "OllamaEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "PasswordSecret",
    "RetrievalResult",
    "RetrievalTrace",
    "Secret",
    "SecretMetadata",
    "SecretStore",
    "SecretsStore",
    "SearchHit",
    "SessionStore",
    "SshPrivateKeySecret",
    "Triple",
    "VectorHit",
    "VectorStore",
    "FilesystemCookieStore",
    "FilesystemVaultProvider",
    "VaultProvider",
    "VaultStore",
    "chunk_markdown",
]
