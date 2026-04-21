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

### 4. LLM Provider Layer (`loom.llm`)

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

### 5. Streaming

`run_turn_stream()` yields `StreamEvent` objects:
- `ContentDeltaEvent` -- text deltas
- `ToolCallDeltaEvent` -- tool call fragments (index-based assembly)
- `UsageEvent` -- token counts
- `StopEvent` -- stop reason

The loop handles tool call assembly: collects fragments, dispatches completed tools, continues iteration.

### 6. Human-in-the-Loop (HITL)

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

### 7. Model Routing (`loom.routing`)

**Message classification:**
- `coding` -- regex: def/class/import/SELECT/traceback/bug/fix/debug
- `reasoning` -- regex: why/explain/analyze/compare/plan/design + length > 40 chars
- `trivial` -- short messages < 80 chars
- `balanced` -- everything else

**Model selection:**
- Each model has `ModelStrengths` (speed/cost/reasoning/coding scores 1-10)
- Primary strength based on category + cost tiebreaker
- Returns best model ID from the registry

### 8. Store Layer (`loom.store`)

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
- Simple key-value secrets
- Suitable for local dev; production apps should use encryption

**Atomic writes** (`store/atomic.py`):
- `tempfile.mkstemp` + `os.replace`
- Cleanup on exception

### 9. Server (`loom.server`)

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

### 10. Configuration (`loom.config`)

**LoomConfig** -- JSON-based:
- `default_model`, `max_iterations`, `system_preamble`, `routing_mode`
- `providers: dict[str, ProviderConfig]` -- base_url, api_key, type
- `models: list[dict]` -- model entries with tags and strengths

**Config resolution precedence:** CLI flags > env vars > config file > defaults

**Environment variables:**
- `LOOM_LLM_BASE_URL` -- LLM API endpoint
- `LOOM_LLM_API_KEY` -- API key
- `LOOM_LLM_MODEL` -- model name

### 11. Error Handling (`loom.errors`, `loom.retry`)

**Error classification:**
- `LLMTransportError` -- network/HTTP errors (retryable)
- `MalformedOutputError` -- parse failures (never retryable)
- `ClassifiedError` -- reason + recovery action (retry/backoff/rotate/compress/abort)

**Retry:**
- Jittered exponential backoff (base 2s, max 60s)
- Only retries `LLMTransportError` with retryable classification
- Monotonic counter for decorrelation

### 12. Agent Communication Protocol (`loom.acp`)

ACP enables agents to call external agents over WebSocket with Ed25519 authentication.

**DeviceKeypair** -- Ed25519 keypair stored at `~/.loom/device.key`:
- `sign_challenge(challenge)` -- produce a base64 signature
- `public_key_b64` -- base64-encoded public key

**AcpCallTool** -- tool handler for calling remote agents:
- Takes `url`, `message`, and optional `agent_name`
- Opens a WebSocket connection, authenticates via challenge-response
- Returns the remote agent's response

**AcpConfig** -- connection configuration (URL, timeout, retries).

### 13. HITL Broker (`loom.hitl`)

For web/SSE integrations where the agent can't directly prompt a terminal user.

**HitlBroker** -- session-scoped Future registry + pub/sub event bus:
- `ask(session_id, kind, message, choices, timeout)` -- parks on an `asyncio.Future`
- `answer(session_id, request_id, answer)` -- resolves the Future
- `subscribe(session_id)` -- async iterator of `HitlEvent` for SSE streaming

**BrokerAskUserTool** -- wraps `HitlBroker.ask()` as a `ToolHandler`, scoped to a session ID.

**Use case:** The FastAPI server creates a `HitlBroker`, wires `BrokerAskUserTool` into the agent's tool registry, and exposes an HTTP endpoint so frontends can resolve pending questions.

### 14. Memory Recall (`loom.store.memory`)

Beyond simple search, `MemoryStore.recall()` provides hybrid retrieval:

**Scoring:** BM25 (FTS5) + salience (pinned/importance/access_count) + recency, blended into a single rank score.

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
When wired in, vector similarity is added to the hybrid score. Default is pure BM25+salience.

**Memory preview** -- top 5 recent memories auto-injected into system prompt (1500 char budget).

## Data Flow

```
User message
    |
    v
Agent.run_turn(messages, context)
    |
    |-- Build system prompt (preamble + identity + memory preview + skill catalog + pending question)
    |-- Annotate short replies (yes/no + pending question)
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
