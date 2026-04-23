# Loom Architecture

## Overview

Loom is a Python framework for building agentic applications. It provides the core infrastructure that any agent needs: an iterative LLM loop, tool calling, skill management, streaming, human-in-the-loop approvals, and multi-provider support.

The framework is designed around **composition over inheritance**. Apps create components, configure them, and wire them together. The framework doesn't dictate your domain model.

## Core Concepts

### 1. Agentic Loop (`loom.loop`)

The `Agent` class implements the core agentic pattern:

```
User message -> Build system prompt -> LOOP:
    LLM call (with retry) -> 
    If tool_calls: dispatch each -> append results -> continue
    If stop: extract reply -> return AgentTurn
```

**Key features:**
- Configurable max iterations (default 32)
- Progressive disclosure of skills (only names/descriptions in system prompt, full body loaded via `activate_skill`)
- Pending question tracking across turns (annotates short replies like "yes"/"no" with context)
- Streaming and non-streaming modes
- Hook points: `on_before_turn`, `on_after_turn`, `on_tool_result`

**AgentConfig** controls behavior:
- `max_iterations` -- iteration budget
- `model` -- default model ID for provider registry
- `system_preamble` -- static system prompt prefix
- `on_before_turn(messages)` -- transform messages before each turn
- `on_after_turn(turn)` -- callback after completion
- `on_tool_result(tool_call, result)` -- callback per tool execution

### 2. Tool System (`loom.tools`)

Tools are the agent's way of acting in the world.

**ToolHandler** (abstract base class):
```python
class ToolHandler(ABC):
    @property
    @abstractmethod
    def tool(self) -> ToolSpec: ...  # Declaration for the LLM

    @abstractmethod
    async def invoke(self, args: dict) -> ToolResult: ...  # Execution
```

**ToolRegistry** manages handlers:
- `register(handler)` -- add a tool
- `dispatch(name, args)` -- invoke by name (catches exceptions, returns error ToolResult)
- `specs()` -- get all tool declarations for LLM

**ToolResult** carries execution output:
- `text: str` -- primary text result (always present)
- `metadata: dict` -- ancillary data (exit codes, status, etc.)
- `content_parts: list[ContentPart] | None` -- structured media (images, files) alongside text
- `is_error: bool` -- error flag

When `content_parts` is set, the agent loop constructs a multimodal `ChatMessage` (list of `ContentPart`) instead of a plain string, forwarding images and other media natively to the model.

**Built-in tools:**
| Tool | File | Description |
|---|---|---|
| `http_call` | `tools/http.py` | GET/POST requests with configurable timeout and truncation |
| `ask_user` | `tools/hitl.py` | HITL: confirm/choice/text questions to the user |
| `terminal` | `tools/hitl.py` | Approval-gated shell command execution |
| `vault` | `tools/vault.py` | Search/read/write/list on a vault store |
| `memory` | `tools/memory.py` | Read/write/search/list/delete on structured memory |
| `delegate` | `tools/delegate.py` | Inter-agent delegation via AgentRuntime |
| `edit_identity` | `tools/profile.py` | Edit SOUL/IDENTITY/USER.md within permission bounds |
| `web_search` | `tools/search.py` | Multi-provider web search (DDGS, Brave, Tavily, Google) with concurrent/fallback strategies |

**Search providers** (`loom.search`):

| Provider | Class | Requires API key | Install |
|---|---|---|---|
| DuckDuckGo (default) | `DuckDuckGoSearchProvider` | No | `pip install "loom[search]"` |
| Brave | `BraveSearchProvider` | Yes | — |
| Tavily | `TavilySearchProvider` | Yes | — |
| Google Custom Search | `GoogleSearchProvider` | Yes (`api_key` + `cx`) | — |

The DDGS provider runs in a background thread via `asyncio.to_thread()` to avoid blocking the event loop. Multiple providers can be composed with `CompositeSearchProvider` using `CONCURRENT` (fire all, merge, deduplicate) or `FALLBACK` (try sequentially, stop when enough results) strategies.

**Adding custom tools** -- subclass `ToolHandler`, implement `tool` and `invoke`, register with `ToolRegistry`.

### 3. Skill System (`loom.skills`)

Skills are Markdown documents (SKILL.md) that teach the agent new procedures.

**Format:**
```markdown
---
name: my-skill
description: What this skill does
---

# Instructions for the agent
Step-by-step procedure...
```

**Progressive disclosure:**
1. System prompt lists only `(name, description)` pairs
2. Agent calls `activate_skill(name)` -> full body injected as tool result
3. Agent follows the instructions using its available tools

**SkillRegistry** -- discovers SKILL.md files, maintains in-memory index:
- `scan()` -- discover all skills
- `descriptions()` -> `(name, description)` pairs for system prompt
- `get(name)` -> full Skill object

**SkillManager** -- 6-op lifecycle (create/edit/patch/delete/write_file/remove_file):
- All writes are atomic (tempfile + rename)
- All writes are guard-scanned before persisting
- Validation: re-parses after write, rolls back on failure
- Path safety: prevents traversal outside skill directory

**SkillGuard** -- regex security scanner:
- `dangerous` (blocked): credential exfiltration, destructive commands, prompt injection
- `caution` (logged): persistence mechanisms (cron, launchd, systemd)
- `safe`: no findings

**Trust tiers:** `builtin` (seed skills), `user` (operator-authored), `agent` (LLM-authored)

### 4. Multimodal Content (`loom.types`, `loom.media`)

`ChatMessage.content` accepts three forms:
- `str` — plain text (backward compatible, no change to existing code)
- `list[ContentPart]` — structured content with typed parts
- `None` — empty content

**Content part types:**

| Type | Fields | Use case |
|---|---|---|
| `TextPart` | `text: str` | Inline text alongside other parts |
| `ImagePart` | `source: str`, `media_type: str` | PNG, JPEG, GIF, WebP images |
| `VideoPart` | `source: str`, `media_type: str` | MP4, WebM video clips |
| `FilePart` | `source: str`, `media_type: str` | PDFs, documents, arbitrary files |

Files are referenced by path or URL. They are loaded from disk at send-time by the provider layer — never stored as base64 blobs in memory or the database. MIME types are inferred from file extensions when `media_type` is omitted.

**`loom.media`** provides file I/O utilities:
- `infer_media_type(source)` — MIME type from extension
- `load_file_bytes(source)` — read file from disk or fetch from URL
- `encode_to_data_url(source)` — base64 data URL (for OpenAI)
- `encode_to_base64(source)` — raw base64 + media type (for Anthropic)

**Provider integration:**
- `OpenAICompatibleProvider._convert_content_part()` maps parts to OpenAI's `image_url` format with base64 data URLs
- `AnthropicProvider._convert_content_part()` maps parts to Anthropic's `image` source blocks with base64 data

**Backward compatibility:** The `ChatMessage.text_content` property extracts text from any content format. All existing code that accesses `.content` as a string continues to work when content is `str`. The agent loop uses `text_content` internally for string operations like short-reply annotation and GraphRAG enrichment.

### 5. LLM Provider Layer (`loom.llm`)

**LLMProvider** (abstract base class):
```python
class LLMProvider(ABC):
    async def chat(messages, *, tools, model) -> ChatResponse: ...
    async def chat_stream(messages, *, tools, model) -> AsyncIterator[StreamEvent]: ...
```

**OpenAICompatibleProvider** -- raw httpx, no SDK:
- Works with OpenAI, Ollama, LM Studio, vLLM, Together, Groq, etc.
- Full streaming with index-based tool call assembly
- Maps to framework types (ChatResponse, StreamEvent)

**AnthropicProvider** -- uses `anthropic` SDK:
- Maps between framework types and Anthropic content blocks
- System message extraction (Anthropic requires separate system param)
- Streaming via `messages.stream()`

**ProviderRegistry** -- maps model IDs to (provider, upstream_model_name):
```python
registry = ProviderRegistry()
registry.register("gpt-4o", openai_provider, "gpt-4o")
registry.register("llama3", ollama_provider, "llama3")
provider, model = registry.resolve("gpt-4o")
```

**Secret redaction** (`llm/redact.py`):
- 30+ patterns for API keys, tokens, connection strings, JWTs, etc.
- Idempotent (already-redacted tokens pass through)
- Applied to outbound LLM payloads

### 6. Streaming

`run_turn_stream()` yields `StreamEvent` objects:
- `ContentDeltaEvent` -- text deltas
- `ToolCallDeltaEvent` -- tool call fragments (index-based assembly)
- `UsageEvent` -- token counts
- `StopEvent` -- stop reason

The loop handles tool call assembly: collects fragments, dispatches completed tools, continues iteration.

### 7. Human-in-the-Loop (HITL)

**ask_user** tool:
- `confirm` -- yes/no question
- `choice` -- pick from options
- `text` -- free-form input
- The handler is a callback: `async (kind, message, choices) -> str`
- In the TUI, this prompts the user in the terminal
- In the server, this parks on an asyncio.Future and emits SSE events

**terminal** tool:
- Composes on top of ask_user for approval
- Configurable timeout, max output truncation
- Runs via `asyncio.create_subprocess_shell`

### 8. Model Routing (`loom.routing`)

**Message classification:**
- `coding` -- regex: def/class/import/SELECT/traceback/bug/fix/debug
- `reasoning` -- regex: why/explain/analyze/compare/plan/design + length > 40 chars
- `trivial` -- short messages < 80 chars
- `balanced` -- everything else

**Model selection:**
- Each model has `ModelStrengths` (speed/cost/reasoning/coding scores 1-10)
- Primary strength based on category + cost tiebreaker
- Returns best model ID from the registry

### 9. Store Layer (`loom.store`)

**SessionStore** -- SQLite at `~/.loom/agents/<name>/sessions.sqlite` (per-agent):
- Message persistence with tool_calls serialization
- Usage tracking (tokens, tool calls)
- Session metadata (title, model, context)
- Search (LIKE-based, can upgrade to FTS5)

**VaultStore** -- filesystem + FTS5:
- Markdown documents with optional YAML frontmatter
- FTS5 full-text search with BM25 ranking and snippets
- Tag extraction from frontmatter and #hashtags
- Atomic writes, path traversal prevention
- Auto-reindexing on write

**SecretsStore** -- plaintext JSON at `~/.loom/secrets.json` (0600):
- Simple key-value secrets; kept for backward compatibility
- **Deprecated** — prefer `SecretStore` for new code

**SecretStore** -- Fernet-encrypted JSON at a caller-supplied path (RFC 0002):
- 8 typed secrets: `password`, `api_key`, `basic_auth`, `bearer_token`, `oauth2_client_credentials`, `ssh_private_key`, `aws_sigv4`, `jwt_signing_key`
- Key auto-generated at `<store_dir>/keys/secrets.key` (mode 0600); override with `LOOM_SECRET_KEY` env var
- All reads decrypt from disk on every call (no in-process secret cache)
- `put / get / get_metadata / list / revoke / rotate`

**KeychainStore** -- OS keychain backend for `SecretStore`-compatible access (`loom[keychain]`):
- Same protocol as `SecretStore`; backed by macOS Keychain / Linux Secret Service / Windows Credential Manager via `keyring`

**Atomic writes** (`store/atomic.py`):
- `tempfile.mkstemp` + `os.replace`
- Cleanup on exception

### 10. Server (`loom.server`)

**create_app()** factory:
```python
app = create_app(agent, sessions, skills, tool_registry)
```

**Endpoints:**
| Method | Path | Description |
|---|---|---|
| GET | /health | Health check |
| POST | /chat | Non-streaming chat |
| POST | /chat/stream | SSE streaming chat |
| GET | /sessions | List sessions |
| DELETE | /sessions/{id} | Delete session |
| GET | /skills | List skills |

Apps extend with domain routes.

### 11. Configuration (`loom.config`)

**LoomConfig** -- JSON-based:
- `default_model`, `max_iterations`, `system_preamble`, `routing_mode`
- `providers: dict[str, ProviderConfig]` -- base_url, api_key, type
- `models: list[dict]` -- model entries with tags and strengths

**Config resolution precedence:** CLI flags > env vars > config file > defaults

**Environment variables:**
- `LOOM_LLM_BASE_URL` -- LLM API endpoint
- `LOOM_LLM_API_KEY` -- API key
- `LOOM_LLM_MODEL` -- model name

### 12. Error Handling (`loom.errors`, `loom.retry`)

**Error classification:**
- `LLMTransportError` -- network/HTTP errors (retryable)
- `MalformedOutputError` -- parse failures (never retryable)
- `ClassifiedError` -- reason + recovery action (retry/backoff/rotate/compress/abort)

**Retry:**
- Jittered exponential backoff (base 2s, max 60s)
- Only retries `LLMTransportError` with retryable classification
- Monotonic counter for decorrelation

### 13. Agent Communication Protocol (`loom.acp`)

ACP enables agents to call external agents over WebSocket with Ed25519 authentication.

**DeviceKeypair** -- Ed25519 keypair stored at `~/.loom/device.key`:
- `sign_challenge(challenge)` -- produce a base64 signature
- `public_key_b64` -- base64-encoded public key

**AcpCallTool** -- tool handler for calling remote agents:
- Takes `url`, `message`, and optional `agent_name`
- Opens a WebSocket connection, authenticates via challenge-response
- Returns the remote agent's response

**AcpConfig** -- connection configuration (URL, timeout, retries).

### 14. MCP Client (`loom.mcp`)

MCP (Model Context Protocol) client integration -- connect to external MCP servers and register their tools with a Loom `ToolRegistry`. Optional subpackage (requires `pip install "loom[mcp]"`).

**McpServerConfig** -- Pydantic model describing one server:
- `transport: "stdio" | "sse"`
- stdio: `command: list[str]`, `env: dict`
- sse: `url: str`, `headers: dict`

**McpClient** -- async context manager that owns the session lifecycle:
- `__aenter__` launches the subprocess (stdio) or opens SSE, then calls `initialize()`
- `list_tools()` discovers remote tools via `tools/list` and returns `McpToolHandler` instances
- `call_tool(name, args)` proxies to `tools/call`, returning a `ToolResult` with optional `content_parts` for native image forwarding

When an MCP server returns `ImageContent` blocks, the client saves them to temporary files and returns `ImagePart` references in `ToolResult.content_parts`. The agent loop then constructs multimodal tool-result messages, forwarding images natively to the model instead of embedding raw base64 as text.

**McpToolHandler** -- a `ToolHandler` wrapping one remote MCP tool. Constructed with a `call_fn` callable (bound to the parent `McpClient.call_tool`) to avoid circular coupling.

**Lifecycle:** the `McpClient` context manager must stay open while tools are in use -- register the handlers inside the `async with` block.

### 15. HITL Broker (`loom.hitl`)

For web/SSE integrations where the agent can't directly prompt a terminal user.

**HitlBroker** -- session-scoped Future registry + pub/sub event bus:
- `ask(session_id, kind, message, choices, timeout)` -- parks on an `asyncio.Future`
- `answer(session_id, request_id, answer)` -- resolves the Future
- `subscribe(session_id)` -- async iterator of `HitlEvent` for SSE streaming

**BrokerAskUserTool** -- wraps `HitlBroker.ask()` as a `ToolHandler`, scoped to a session ID.

**Use case:** The FastAPI server creates a `HitlBroker`, wires `BrokerAskUserTool` into the agent's tool registry, and exposes an HTTP endpoint so frontends can resolve pending questions.

### 16. Memory Recall (`loom.store.memory`)

Beyond simple search, `MemoryStore.recall()` provides hybrid retrieval:

**Scoring:** BM25 (FTS5) + salience (pinned/importance/access_count) + recency + optional vector similarity, blended into a single rank score.

**Salience signals:**
- `pinned` -- always promoted to top results
- `importance` -- user-set priority (1-5)
- `access_count` -- frequently accessed memories rank higher
- `recency` -- decayed by age

**EmbeddingProvider** (optional protocol):
```python
class EmbeddingProvider(Protocol):
    dim: int
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```
When wired in, vector similarity is blended into the hybrid score (weight 0.30). Weights with embeddings: BM25 0.35, salience 0.25, recency 0.10, vector 0.30. Without an embedder: BM25 0.55, salience 0.30, recency 0.15.

**Memory preview** -- top 5 recent memories auto-injected into system prompt (1500 char budget).

### 17. Credential Subsystem (`loom.auth`, `loom.store.secrets`)

Implements RFC 0002 (credentials + appliers + policies) and RFC 0003 (SSH tool). Three decoupled layers; each is independently usable.

#### Layer 1 — Secret storage

`SecretStore` is the default backend: typed, Fernet-encrypted, scope-keyed JSON file. `KeychainStore` is the OS-backed alternative (`loom[keychain]`). Both expose the same async protocol: `put / get / get_metadata / list / revoke / rotate`.

Scopes are opaque strings (e.g. `"prod-oic-us-east"`, `"agent:coder:openai"`). Loom imposes no structure.

#### Layer 2 — Auth appliers

An **applier** converts a `Secret` into transport-ready material. Each applier handles exactly one `(secret_type, transport)` pair.

| Applier | Secret type | Transport | Output |
|---|---|---|---|
| `BasicHttpApplier` | `basic_auth` | `http` | `{"Authorization": "Basic ..."}` |
| `BearerHttpApplier` | `bearer_token` | `http` | `{"Authorization": "Bearer ..."}` |
| `OAuth2CCHttpApplier` | `oauth2_client_credentials` | `http` | `{"Authorization": "Bearer ..."}` (token cached in-process) |
| `ApiKeyHeaderApplier` | `api_key` | `http` | `{header_name: value}` (configurable header) |
| `ApiKeyStringApplier` | `api_key` | `llm_api_key` | raw `str` |
| `SshPasswordApplier` | `password` | `ssh` | `SshConnectArgs` |
| `SshKeyApplier` | `ssh_private_key` | `ssh` | `SshConnectArgs` |
| `SigV4Applier` | `aws_sigv4` | `http` | full headers dict including `Authorization` (`loom[aws]`) |
| `JwtBearerApplier` | `jwt_signing_key` | `http` | `{"Authorization": "Bearer <signed-jwt>"}` (`loom[jwt]`) |

Extras groups: `loom[ssh]` (asyncssh), `loom[aws]` (botocore), `loom[jwt]` (PyJWT), `loom[keychain]` (keyring).

#### Layer 3 — Policy enforcement (HITL gating)

`PolicyEnforcer.gate(scope, context)` runs *before* the secret is fetched. Five modes:

| Mode | Behaviour |
|---|---|
| `AUTONOMOUS` | No gate — agent uses credential freely |
| `NOTIFY_BEFORE` | Blocks; human must approve via `HitlBroker` before secret is released |
| `NOTIFY_AFTER` | Fire-and-log — secret released immediately, event emitted for audit |
| `TIME_BOXED` | Autonomous inside `[window_start, window_end)`; denied outside |
| `ONE_SHOT` | Allowed once; `uses_remaining` decremented to 0 and secret auto-revoked |

No policy configured for a scope → implicit `AUTONOMOUS` (backward compatible).

`PolicyStore` persists `CredentialPolicy` objects as 0600 JSON (not encrypted — policies are metadata, not secrets).

#### The resolution pipeline

```
scope
  → ACL check (scope_acl, optional)       raises ScopeAccessDenied
  → PolicyEnforcer.gate()                  raises CredentialDenied
  → SecretStore.get()                      raises ScopeNotFoundError
  → Applier.apply(secret, context)         raises NoApplierError / AuthApplierError
  → transport-ready material
```

`CredentialResolver` wires these three layers together. Consumers register appliers with `resolver.register(applier, transport=...)`, then call `await resolver.resolve_for(scope, transport)`.

#### Pre-request hook integration (RFC 0001)

`HttpCallTool` accepts an optional `pre_request_hook: async (dict) -> dict` that runs after argument parsing and before the HTTP request is dispatched. The hook receives and returns `{method, url, headers, body}`. This is the canonical way to feed resolver output into an HTTP tool call.

#### SshCallTool (RFC 0003)

`loom.tools.ssh.SshCallTool` runs one-shot commands on remote hosts via asyncssh. It calls `resolver.resolve_for(scope, "ssh")` to get `SshConnectArgs` (host, port, username, auth material) and never exposes connection details to the agent. Returns `ToolResult` with `exit_code`, `stderr`, `truncated_stdout`, `truncated_stderr`, `duration_ms` in metadata. Errors are classified as `auth | timeout | transport | unknown`.

Requires `loom[ssh]`.

### 18. Heartbeat Scheduler (`loom.heartbeat`)

Heartbeats are recurring scheduled tasks. Each one consists of two files in its own directory: `HEARTBEAT.md` (YAML frontmatter with `name`, `description`, `schedule`, `enabled` + a Markdown body used as the agent's system prompt) and `driver.py` (a class `Driver(HeartbeatDriver)` that implements `check(state) -> (events, new_state)`).

**Key design choices:**
- **Drivers are stateless pure functions.** They receive state-in and return state-out; the runtime persists state between ticks via `HeartbeatStore` (SQLite, WAL mode).
- **Events drive agent invocations.** When `driver.check()` returns a non-empty events list, the scheduler calls `run_fn(instructions, [event_message])` once per event. The agent runs with the heartbeat's instructions as its system prompt and the event summary as the user message.
- **Multi-instance support.** State is keyed by `(heartbeat_id, instance_id)`, so the same driver package can run as independent instances without shared state.
- **SessionStore integration (optional).** When a `SessionStore` is provided, each agent invocation is persisted as a titled session for observability.

**Components:**

| Class | Role |
|---|---|
| `HeartbeatDriver` | ABC — implement `check(state) -> (events, new_state)` |
| `HeartbeatRegistry` | In-memory index; scans directories for `HEARTBEAT.md` files |
| `HeartbeatStore` | SQLite state persistence keyed by `(heartbeat_id, instance_id)` |
| `HeartbeatScheduler` | Asyncio background loop; ticks, fires, invokes agent |
| `HeartbeatManager` | Disk CRUD (create/delete/enable/disable/list) + registry sync |
| `HeartbeatToolHandler` | `manage_heartbeat` tool — lets the agent manage heartbeats at runtime |

**Schedule formats:** natural language (`"every 5 minutes"`), cron shorthands (`@daily`, `@hourly`), or 5-field cron (`"0 9 * * 1-5"`).

**Wire-up example:**
```python
from loom.heartbeat import HeartbeatRegistry, HeartbeatScheduler, HeartbeatStore, make_run_fn

registry = HeartbeatRegistry(heartbeats_dir=Path(".myapp/heartbeats"))
registry.scan()

store = HeartbeatStore(db_path=Path(".myapp/heartbeats.db"))
scheduler = HeartbeatScheduler(registry, store, run_fn=make_run_fn(agent))
scheduler.start()  # background asyncio.Task
```

### 19. GraphRAG (`loom.store.graphrag`)

Graph-based Retrieval-Augmented Generation. Fully opt-in — pass a `GraphRAGEngine` to `Agent(graphrag=...)` or leave it `None` (default).

**Components:**

| Module | Class | Role |
|---|---|---|
| `store.vector` | `VectorStore` | SQLite-backed vector store; float32 BLOBs, brute-force cosine search |
| `store.graph` | `EntityGraph` | SQLite-backed entity-relationship graph; multi-hop BFS traversal, paginated listing, subgraph extraction, connected components, degree counts |
| `store.embeddings` | `OllamaEmbeddingProvider` / `OpenAIEmbeddingProvider` | Async embedding API clients |
| `store.graphrag` | `GraphRAGEngine` | Orchestrator: chunking, indexing, extraction, retrieval, context formatting |

**Pipeline:**

1. **Chunking** — `chunk_markdown()` splits text on headings, merges small sections, and splits large ones with overlap. Deterministic chunk IDs via SHA-256.
2. **Embedding + indexing** — chunks are embedded and stored in `VectorStore`. Source-level replacement (re-indexing a path removes old chunks first).
3. **Entity extraction** (optional, requires `llm_provider`) — an LLM extracts entities and relationships from each chunk using a structured JSON prompt. Supports gleaning (re-prompting for missed entities). Results are stored in `EntityGraph` with mention tracking and alias resolution.
4. **Hybrid retrieval** — vector similarity search finds top chunks; graph expansion adds related chunks via multi-hop entity neighbors. Results are scored and deduplicated.
5. **Context injection** — `format_context()` assembles results into a Markdown block within a configurable character budget. The agent loop appends this to the system message once per `run()`/`run_stream()` call.

**Usage levels (all opt-in):**
- No GraphRAG — default, no overhead.
- Vector search only — embedder without LLM provider.
- Full GraphRAG — embedder + LLM for entity extraction.

**New optional extra:** `pip install "loom[graphrag]"` (numpy>=1.26 for accelerated batch cosine similarity; pure-Python fallback when absent).

---

## Data Flow

```
User message
    |
    v
Agent.run_turn(messages, context)
    |
    |-- Build system prompt (preamble + identity + memory preview + skill catalog + pending question)
    |-- Annotate short replies (yes/no + pending question)
    |-- (optional) GraphRAG enrich: retrieve relevant context, inject into system message
    |
    v
LOOP (max_iterations):
    |
    |-- Resolve provider (from ProviderRegistry or single provider)
    |-- Optionally route to best model via classify_message + choose_model
    |-- Call LLM (with jittered retry on transport errors)
    |
    |-- If stop (no tool_calls):
    |       |-- Extract pending question
    |       |-- Return AgentTurn
    |
    |-- If tool_calls:
    |       |-- For each tool_call:
    |       |       |-- If activate_skill: inject skill body
    |       |       |-- If delegate: forward to sub-agent via AgentRuntime
    |       |       |-- If acp_call: forward to external agent via WebSocket
    |       |       |-- Else: dispatch to ToolRegistry
    |       |       |-- Append tool result to messages
    |       |-- Continue loop
    |
    v
AgentTurn (reply, iterations, skills_touched, messages, usage, model)
```

## Design Principles

1. **Composition over inheritance** -- Components are created and wired, not subclassed
2. **Optional everything** -- Streaming, HITL, multi-provider, skills are all opt-in
3. **No framework lock-in** -- Use what you need, extend what you want
4. **Crash safety** -- All disk mutations are atomic
5. **Security first** -- Guard scanner, secret redaction, path traversal prevention
6. **Type safe** -- Pydantic v2 models throughout
