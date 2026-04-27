"""Loom — an AI agent framework for building, running, and composing agents.

Top-level exports are grouped by subsystem:

* **Agent core** — :class:`~loom.loop.Agent`, :class:`~loom.loop.AgentConfig`,
  :class:`~loom.loop.AgentTurn`, :class:`~loom.runtime.AgentRuntime`
* **LLM** — :class:`~loom.llm.base.LLMProvider`, :class:`~loom.llm.registry.ProviderRegistry`
* **Types** — :class:`~loom.types.ChatMessage`, :class:`~loom.types.StreamEvent`,
  :class:`~loom.types.ToolSpec`, etc.
* **Store** — :class:`~loom.store.memory.MemoryStore`, :class:`~loom.store.session.SessionStore`,
  :class:`~loom.store.vault.VaultProvider`, :class:`~loom.store.graphrag.GraphRAGEngine`
* **Tools** — :class:`~loom.tools.base.ToolHandler`, :class:`~loom.tools.registry.ToolRegistry`
* **Skills** — :class:`~loom.skills.types.Skill`, :class:`~loom.skills.registry.SkillRegistry`
* **Auth** — credential appliers, resolvers, and policies (:mod:`loom.auth`)
* **Heartbeat** — :class:`~loom.heartbeat.types.HeartbeatDriver`, schedulers, and managers
* **Search** — :class:`~loom.search.base.SearchProvider`, multi-provider
  orchestration, web search tool
* **Scrape** — :class:`~loom.scrape.base.ScrapeProvider`, Scrapling cascade
  fetcher, web scrape tool
* **HITL** — :class:`~loom.hitl.broker.HitlBroker` for web/SSE human-in-the-loop
* **MCP / ACP** — optional extras: Model Context Protocol and multi-agent WebSocket transport

See :doc:`/index` for a quick-start guide.
"""

from loom.acp import AcpCallTool, AcpConfig
from loom.auth import (
    ApiKeyHeaderApplier,
    ApiKeyStringApplier,
    Applier,
    AuthApplierError,
    BasicHttpApplier,
    BearerHttpApplier,
    CredentialDenied,
    CredentialPolicy,
    CredentialResolver,
    GateDecision,
    JwtBearerApplier,
    MissingPrincipalError,
    NoApplierError,
    OAuth2CCHttpApplier,
    PolicyEnforcer,
    PolicyMode,
    PolicyStore,
    ScopeAccessDenied,
    ScopeAcl,
    ScopeNotFoundError,
    SecretExpiredError,
    SigV4Applier,
    SshConnectArgs,
    SshKeyApplier,
    SshPasswordApplier,
)
from loom.heartbeat import (
    HeartbeatDriver,
    HeartbeatEvent,
    HeartbeatManager,
    HeartbeatRecord,
    HeartbeatRegistry,
    HeartbeatRunRecord,
    HeartbeatScheduler,
    HeartbeatStore,
    HeartbeatToolHandler,
    RunFn,
    Schedule,
    is_due,
    load_heartbeat,
    make_run_fn,
    parse_schedule,
)
from loom.hitl import (
    CURRENT_SESSION_ID,
    TIMEOUT_SENTINEL,
    BrokerAskUserTool,
    HitlBroker,
    HitlEvent,
    HitlRequest,
)
from loom.home import AgentHome
from loom.llm.base import LLMProvider
from loom.llm.registry import ProviderRegistry
from loom.loop import Agent, AgentConfig, AgentTurn
from loom.mcp import McpClient, McpServerConfig, McpToolHandler
from loom.permissions import AgentPermissions
from loom.prompt import PromptBuilder, PromptSection
from loom.runtime import AgentRuntime
from loom.scrape import ScrapeProvider, ScrapeProviderError, ScrapeResult, ScraplingProvider
from loom.search import (
    BraveSearchProvider,
    CompositeSearchProvider,
    DuckDuckGoSearchProvider,
    GoogleSearchProvider,
    SearchProvider,
    SearchProviderError,
    SearchResult,
    SearchStrategy,
    TavilySearchProvider,
)
from loom.skills.guard import SkillGuard
from loom.skills.manager import SkillManager
from loom.skills.registry import SkillRegistry
from loom.skills.types import Skill, SkillGuardVerdict, SkillMetadata
from loom.store.cookies import CookieStore, FilesystemCookieStore
from loom.store.embeddings import OllamaEmbeddingProvider, OpenAIEmbeddingProvider
from loom.store.graph import Entity, EntityGraph, Triple
from loom.store.graphrag import (
    Chunk,
    EnrichedRetrieval,
    GraphRAGConfig,
    GraphRAGEngine,
    HopRecord,
    RetrievalResult,
    RetrievalTrace,
    chunk_markdown,
)
from loom.store.keychain import KeychainStore
from loom.store.memory import MemoryEntry, MemoryStore
from loom.store.secrets import (
    ApiKeySecret,
    AwsSigV4Secret,
    BasicAuthSecret,
    BearerTokenSecret,
    JwtSigningKeySecret,
    OAuth2ClientCredentialsSecret,
    PasswordSecret,
    Secret,
    SecretMetadata,
    SecretsStore,
    SecretStore,
    SshPrivateKeySecret,
)
from loom.store.vault import FilesystemVaultProvider, VaultProvider
from loom.store.vector import VectorHit, VectorStore
from loom.tools.base import ToolHandler, ToolResult
from loom.tools.registry import ToolRegistry
from loom.tools.scrape import WebScrapeTool
from loom.tools.search import WebSearchTool
from loom.tools.ssh import SshCallTool
from loom.tools.vault import VaultToolHandler
from loom.types import (
    ChatMessage,
    ChatResponse,
    ContentDeltaEvent,
    ContentPart,
    DoneEvent,
    ErrorEvent,
    FilePart,
    ImagePart,
    LimitReachedEvent,
    OverflowEvent,
    Role,
    StopEvent,
    StopReason,
    StreamEvent,
    TextPart,
    ToolCall,
    ToolCallDeltaEvent,
    ToolExecResultEvent,
    ToolExecStartEvent,
    ToolSpec,
    Usage,
    UsageEvent,
    VideoPart,
)

__version__ = "0.4.0b1"

__all__ = [
    # auth — Phase A
    "Applier",
    "BasicHttpApplier",
    "BearerHttpApplier",
    "OAuth2CCHttpApplier",
    "ApiKeyHeaderApplier",
    "ApiKeyStringApplier",
    "CredentialResolver",
    "AuthApplierError",
    "SecretExpiredError",
    "NoApplierError",
    "ScopeNotFoundError",
    # auth — Phase B
    "PolicyMode",
    "CredentialPolicy",
    "PolicyStore",
    "GateDecision",
    "CredentialDenied",
    "PolicyEnforcer",
    # auth — RFC 0003 SSH appliers
    "SshConnectArgs",
    "SshPasswordApplier",
    "SshKeyApplier",
    # auth — Phase C
    "SigV4Applier",
    "JwtBearerApplier",
    "ScopeAccessDenied",
    "MissingPrincipalError",
    "ScopeAcl",
    # store.secrets
    "ApiKeySecret",
    "AwsSigV4Secret",
    "BasicAuthSecret",
    "BearerTokenSecret",
    "JwtSigningKeySecret",
    "OAuth2ClientCredentialsSecret",
    "PasswordSecret",
    "Secret",
    "SecretMetadata",
    "SecretStore",
    "SecretsStore",
    "SshPrivateKeySecret",
    # store.keychain
    "KeychainStore",
    "Agent",
    "AgentConfig",
    "AgentHome",
    "AgentRuntime",
    "AgentTurn",
    "ChatMessage",
    "ChatResponse",
    "ContentDeltaEvent",
    "ContentPart",
    "DoneEvent",
    "ErrorEvent",
    "FilePart",
    "ImagePart",
    "LimitReachedEvent",
    "LLMProvider",
    "OverflowEvent",
    "StopEvent",
    "ToolCallDeltaEvent",
    "ToolExecResultEvent",
    "ToolExecStartEvent",
    "UsageEvent",
    "MemoryEntry",
    "MemoryStore",
    "GraphRAGConfig",
    "GraphRAGEngine",
    "Chunk",
    "EnrichedRetrieval",
    "HopRecord",
    "RetrievalResult",
    "RetrievalTrace",
    "Entity",
    "EntityGraph",
    "Triple",
    "OllamaEmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "VectorHit",
    "VectorStore",
    "chunk_markdown",
    "AgentPermissions",
    "PromptBuilder",
    "PromptSection",
    "ProviderRegistry",
    "Role",
    "Skill",
    "SkillGuard",
    "SkillGuardVerdict",
    "SkillManager",
    "SkillMetadata",
    "SkillRegistry",
    "StopReason",
    "StreamEvent",
    "TextPart",
    "ToolCall",
    "SshCallTool",
    "ToolHandler",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "VaultProvider",
    "VideoPart",
    "FilesystemVaultProvider",
    "VaultToolHandler",
    "AcpCallTool",
    "AcpConfig",
    "McpClient",
    "McpServerConfig",
    "McpToolHandler",
    "BrokerAskUserTool",
    "CURRENT_SESSION_ID",
    "HitlBroker",
    "HitlEvent",
    "HitlRequest",
    "TIMEOUT_SENTINEL",
    # heartbeat
    "HeartbeatDriver",
    "HeartbeatEvent",
    "HeartbeatManager",
    "HeartbeatRecord",
    "HeartbeatRegistry",
    "HeartbeatRunRecord",
    "HeartbeatScheduler",
    "HeartbeatStore",
    "HeartbeatToolHandler",
    "RunFn",
    "Schedule",
    "is_due",
    "load_heartbeat",
    "make_run_fn",
    "parse_schedule",
    # search
    "BraveSearchProvider",
    "CompositeSearchProvider",
    "DuckDuckGoSearchProvider",
    "GoogleSearchProvider",
    "SearchProvider",
    "SearchProviderError",
    "SearchResult",
    "SearchStrategy",
    "TavilySearchProvider",
    # scrape
    "ScrapeProvider",
    "ScrapeProviderError",
    "ScrapeResult",
    "ScraplingProvider",
    # store.cookies
    "CookieStore",
    "FilesystemCookieStore",
    # tools — search/scrape
    "WebSearchTool",
    "WebScrapeTool",
]
