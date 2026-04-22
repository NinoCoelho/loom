# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

- `loom.store.graphrag` subpackage: GraphRAG engine for knowledge-graph-augmented retrieval. `GraphRAGEngine` orchestrates markdown chunking (`chunk_markdown`), embedding + vector storage (`VectorStore`), entity/relationship extraction via LLM with gleaning (`EntityGraph`), and hybrid retrieval (vector similarity + multi-hop graph expansion). Context is injected into the agent loop via `_graphrag_enrich()`. Fully opt-in — pass a `GraphRAGEngine` to `Agent(graphrag=...)` or leave it `None` (default) for no change in behavior.

- `loom.store.vector` — `VectorStore`: SQLite-backed vector store. Float32 vectors packed as BLOBs, brute-force cosine similarity search. numpy-accelerated batch cosine with pure-Python fallback. Public API: `upsert / remove / search / get / get_embedding / count / sources`.

- `loom.store.graph` — `EntityGraph`: SQLite-backed entity-relationship graph. Entities with `(name, type)` canonical keys, directed typed triples with evidence tracking, alias resolution, multi-hop neighbor traversal, orphan cleanup.

- `loom.store.embeddings` — `OllamaEmbeddingProvider` and `OpenAIEmbeddingProvider`: async HTTP clients for embedding APIs (Ollama `/api/embed` and OpenAI-compatible `/v1/embeddings`). Batch support with configurable chunk size.

- `MemoryStore` hybrid recall now supports an optional vector similarity component. When an `EmbeddingProvider` is wired in, query and stored embeddings are compared via cosine similarity and blended into the recall score alongside BM25, salience, and recency. Weights: BM25 0.35, salience 0.25, recency 0.10, vector 0.30. Without an embedder, the original weights (0.55/0.30/0.15) are used.

- New optional extra: `pip install "loom[graphrag]"` (depends on `numpy>=1.26`).

### Fixed

- GraphRAG enrichment now runs once per `run()`/`run_stream()` call instead of on every loop iteration, preventing duplicate context accumulation in the system prompt.
- GraphRAG retrieval uses `VectorStore.get_embedding()` public API instead of reaching into internal `_db` attribute.
- Bare `except Exception: pass` blocks in the agent loop now log warnings for debuggability.
- Memory recall fallback weights are named constants (`_W_BM25_NOVEC`, `_W_SALIENCE_NOVEC`, `_W_RECENCY_NOVEC`) instead of inline magic numbers.

- `loom.mcp` subpackage: MCP (Model Context Protocol) client integration. `McpServerConfig`, `McpClient` (async context manager for stdio/SSE transports), and `McpToolHandler` let agents register and call tools exposed by external MCP servers.
- New optional extra: `pip install "loom[mcp]"` (depends on the official `mcp` SDK).

## [0.3.0]

### Added

- **RFC 0001 — `HttpCallTool` pre-request hook.** Optional `pre_request_hook: async (dict) -> dict` parameter on `HttpCallTool`. Hook runs after argument parsing and before dispatch, receiving and returning `{method, url, headers, body}`. Enables credential injection, URL rewriting, and proxy overrides without forking the tool. Fully backward compatible (hook defaults to `None`).

- **RFC 0002 — Credential subsystem.** Full three-layer credential pipeline:
  - *Layer 1 — `loom.store.secrets.SecretStore`*: Fernet-encrypted, scope-keyed typed secret vault. 8 secret types: `password`, `api_key`, `basic_auth`, `bearer_token`, `oauth2_client_credentials`, `ssh_private_key`, `aws_sigv4`, `jwt_signing_key`. API: `put / get / get_metadata / list / revoke / rotate`. Key auto-generated at `$LOOM_HOME/keys/secrets.key` or from `LOOM_SECRET_KEY` env var.
  - *Layer 1 (alt) — `loom.store.keychain.KeychainStore`* (`loom[keychain]`): same protocol, backed by OS keychain (macOS Keychain / Linux Secret Service / Windows Credential Manager) via `keyring`.
  - *Layer 2 — `loom.auth.appliers`*: 9 transport-specific appliers: `BasicHttpApplier`, `BearerHttpApplier`, `OAuth2CCHttpApplier` (in-process token cache), `ApiKeyHeaderApplier`, `ApiKeyStringApplier`, `SshPasswordApplier`, `SshKeyApplier`, `SigV4Applier` (`loom[aws]`), `JwtBearerApplier` (`loom[jwt]`).
  - *Layer 3 — `loom.auth.policies` + `loom.auth.enforcer`*: `PolicyEnforcer` with 5 HITL-gated modes (`AUTONOMOUS`, `NOTIFY_BEFORE`, `NOTIFY_AFTER`, `TIME_BOXED`, `ONE_SHOT`). Integrates with `loom.hitl.HitlBroker` for approval prompts. `PolicyStore` persists policies as 0600 JSON.
  - `CredentialResolver`: single entry-point wiring store + appliers + enforcer. Supports optional `scope_acl` hook for ACL-gated multi-principal deployments (`loom[jwt]` Phase C).
  - Error types: `AuthApplierError`, `SecretExpiredError`, `NoApplierError`, `ScopeNotFoundError`, `ScopeAccessDenied`, `CredentialDenied`.
  - New optional extras: `loom[keychain]`, `loom[aws]`, `loom[jwt]`.

- **RFC 0003 — `SshCallTool`** (`loom[ssh]`): run one-shot commands on remote hosts via asyncssh. Authenticates through the `loom.auth` credential pipeline (`SshPasswordApplier` or `SshKeyApplier`). Hostname/port/username read from `SecretMetadata` — the agent never sees connection details. Configurable `connect_timeout`, `command_timeout`, `max_output_bytes`, and `known_hosts_path`. Error classification: `auth | timeout | transport | unknown`. Error messages scrubbed via `loom.llm.redact` before returning.

- **412 tests passing** across all three RFCs and the rest of the test suite.

## [0.2.0] - 2025-01-20

### Added

- Agent Home directory: structured layout for agent identity, skills, memory, vault, sessions
- Identity files: SOUL.md (purpose/values), IDENTITY.md (name/role/tone), USER.md (preferences)
- PromptBuilder with composable PromptSection system and priority ordering
- AgentPermissions: configurable write access to SOUL.md, IDENTITY.md, USER.md
- EditIdentityTool: agent can edit profile files within permission bounds
- MemoryStore: structured, searchable memory with categories, tags, FTS5 (with LIKE fallback)
- MemoryToolHandler rewritten with read/write/search/list/delete actions
- Multi-skill registry: agent skills + shared skill directories, agent takes precedence
- AgentRuntime: multi-agent lifecycle manager with shared resources
- DelegateTool: agent-as-tool pattern for inter-agent delegation
- Agent-scoped sessions (each agent has its own SQLite session store)
- Memory preview auto-injection into system prompt (top 5 recent, 1500 char budget)
- Graceful FTS5 fallback when SQLite compiled without FTS5 support

### Changed

- `Agent` constructor now accepts `agent_home`, `permissions`, `memory_store` parameters
- `AgentConfig` now has `extra_tools` field for additional tool registration
- `SkillRegistry` now accepts `additional_dirs` for shared skill directories
- `Agent._build_system_prompt()` uses `PromptBuilder` with identity sections + memory preview
- `system_preamble` still works as fallback when no AgentHome is configured

## [0.1.0] - 2025-01-20

### Added

- Core agentic loop with configurable iterations and streaming support
- Tool system with `ToolHandler` ABC and `ToolRegistry` dispatch
- Skill system with SKILL.md format, progressive disclosure, and security guard
- LLM provider layer: OpenAI-compatible (raw httpx) and Anthropic (SDK)
- Multi-provider support via `ProviderRegistry`
- Human-in-the-loop tools: `ask_user` (confirm/choice/text) and `terminal`
- Model routing with message classification (coding/reasoning/trivial/balanced)
- SQLite session store with history persistence and usage tracking
- Vault store with FTS5 full-text search
- Secret redaction with 30+ patterns
- Error classification with retry/abort/compress/fallback decisions
- Jittered exponential backoff retry logic
- Atomic file writes for crash safety
- FastAPI server factory with chat, streaming, and session endpoints
- Configuration management with env overlay resolution
- Pending question tracking across turns
- Test TUI application for framework validation
