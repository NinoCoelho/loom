# Loom API Reference

## Core Types (`loom.types`)

### `Role`
```python
class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
```

### `StopReason`
```python
class StopReason(str, Enum):
    STOP = "stop"
    TOOL_USE = "tool_use"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"
```

### `ChatMessage`
```python
class ChatMessage(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
```

### `ToolCall`
```python
class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str
```

### `ToolSpec`
```python
class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict  # JSON Schema
```

### `Usage`
```python
class Usage(BaseModel):
    input_tokens: int
    output_tokens: int
```

### `ChatResponse`
```python
class ChatResponse(BaseModel):
    message: ChatMessage
    usage: Usage
    stop_reason: StopReason
    model: str
```

### `StreamEvent`
Discriminated union:
- `ContentDeltaEvent(type="content_delta", delta: str)`
- `ToolCallDeltaEvent(type="tool_call_delta", index: int, id: str | None, name: str | None, arguments_delta: str | None)`
- `UsageEvent(type="usage", usage: Usage)`
- `StopEvent(type="stop", stop_reason: StopReason)`

---

## Agentic Loop (`loom.loop`)

### `AgentConfig`
```python
class AgentConfig:
    max_iterations: int = 32
    model: str | None = None
    system_preamble: str = ""
    on_before_turn: Callable[[list[ChatMessage]], list[ChatMessage]] | None
    on_after_turn: Callable[[AgentTurn], None] | None
    on_tool_result: Callable[[ToolCall, str], None] | None
```

### `Agent`
```python
class Agent:
    def __init__(
        self,
        provider: LLMProvider | None = None,
        provider_registry: ProviderRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        config: AgentConfig | None = None,
    ): ...

    async def run_turn(
        self,
        messages: list[ChatMessage],
        context: dict | None = None,
    ) -> AgentTurn: ...

    async def run_turn_stream(
        self,
        messages: list[ChatMessage],
        context: dict | None = None,
    ) -> AsyncIterator[StreamEvent]: ...
```

### `AgentTurn`
```python
class AgentTurn:
    reply: str
    iterations: int
    skills_touched: list[str]
    messages: list[ChatMessage]
    input_tokens: int
    output_tokens: int
    tool_calls: int
    model: str | None
```

---

## Tool System (`loom.tools`)

### `ToolHandler` (ABC)
```python
class ToolHandler(ABC):
    @property
    @abstractmethod
    def tool(self) -> ToolSpec: ...

    @abstractmethod
    async def invoke(self, args: dict) -> ToolResult: ...
```

### `ToolResult`
```python
class ToolResult:
    def __init__(self, text: str, metadata: dict | None = None): ...
    def to_text(self) -> str: ...
```

### `ToolRegistry`
```python
class ToolRegistry:
    def register(self, handler: ToolHandler) -> None: ...
    def unregister(self, name: str) -> None: ...
    async def dispatch(self, name: str, args: dict) -> ToolResult: ...
    def specs(self) -> list[ToolSpec]: ...
    def has(self, name: str) -> bool: ...
    def list_handlers(self) -> list[str]: ...
```

### Built-in Tools

| Class | Tool Name | File |
|---|---|---|
| `HttpCallTool` | `http_call` | `tools/http.py` |
| `AskUserTool` | `ask_user` | `tools/hitl.py` |
| `TerminalTool` | `terminal` | `tools/hitl.py` |
| `VaultToolHandler` | `vault` | `tools/vault.py` |
| `MemoryToolHandler` | `memory` | `tools/memory.py` |

---

## Skill System (`loom.skills`)

### `Skill`
```python
class Skill(BaseModel):
    name: str
    description: str
    body: str
    source_dir: str
    trust: str = "user"  # "builtin" | "user" | "agent"
    metadata: dict = {}
```

### `SkillRegistry`
```python
class SkillRegistry:
    def __init__(self, skills_dir: Path): ...
    def scan(self) -> None: ...
    def descriptions(self) -> list[tuple[str, str]]: ...
    def get(self, name: str) -> Skill | None: ...
    def list(self) -> list[Skill]: ...
    def register(self, skill: Skill) -> None: ...
    def unregister(self, name: str) -> None: ...
    def reload(self) -> None: ...
```

### `SkillManager`
```python
class SkillManager:
    def __init__(self, registry: SkillRegistry, guard: SkillGuard): ...
    def invoke(self, args: dict) -> str: ...
```
Actions: `create`, `edit`, `patch`, `delete`, `write_file`, `remove_file`

### `SkillGuard`
```python
class SkillGuard:
    def scan(self, content: str, filename: str = "") -> SkillGuardVerdict: ...
```

### `SkillGuardVerdict`
```python
class SkillGuardVerdict(BaseModel):
    level: str   # "safe" | "caution" | "dangerous"
    findings: list[str]
```

---

## LLM Providers (`loom.llm`)

### `LLMProvider` (ABC)
```python
class LLMProvider(ABC):
    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse: ...
    async def chat_stream(self, messages, *, tools=None, model=None) -> AsyncIterator[StreamEvent]: ...
```

### `OpenAICompatibleProvider`
```python
class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, base_url: str, api_key: str | None = None, default_model: str = "gpt-4o", timeout: float = 120.0): ...
```

### `AnthropicProvider`
```python
class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, default_model: str = "claude-sonnet-4-20250514", timeout: float = 120.0): ...
```
Requires `pip install anthropic`.

### `ProviderRegistry`
```python
class ProviderRegistry:
    def register(self, model_id: str, provider: LLMProvider, upstream_model: str) -> None: ...
    def resolve(self, model_id: str) -> tuple[LLMProvider, str]: ...
    def list_models(self) -> list[str]: ...
    @property
    def default_model(self) -> str | None: ...
```

### `redact_sensitive_text(text: str) -> str`
Regex-based secret redaction with 30+ patterns.

---

## Stores (`loom.store`)

### `SessionStore`
```python
class SessionStore:
    def __init__(self, db_path: Path): ...
    def get_or_create(self, session_id: str, title: str | None = None) -> dict: ...
    def get_history(self, session_id: str) -> list[ChatMessage]: ...
    def replace_history(self, session_id: str, messages: list[ChatMessage]) -> None: ...
    def set_title(self, session_id: str, title: str) -> None: ...
    def bump_usage(self, session_id: str, input_tokens: int, output_tokens: int, tool_calls: int) -> None: ...
    def list_sessions(self) -> list[dict]: ...
    def delete_session(self, session_id: str) -> bool: ...
    def search(self, query: str, limit: int = 20) -> list[dict]: ...
```

### `VaultStore`
```python
class VaultStore:
    def __init__(self, vault_dir: Path): ...
    async def search(self, query: str, limit: int = 10) -> list[dict]: ...
    async def read(self, path: str) -> str: ...
    async def write(self, path: str, content: str, metadata: dict | None = None) -> None: ...
    async def list(self, prefix: str = "") -> list[str]: ...
    def reindex_all(self) -> None: ...
```

### `SecretsStore`
```python
class SecretsStore:
    def __init__(self, secrets_path: Path): ...
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> bool: ...
    def list_keys(self) -> list[str]: ...
```

---

## Server (`loom.server`)

### `create_app(...)`
```python
def create_app(
    agent: Agent,
    sessions: SessionStore,
    skills: SkillRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
    extra_routes: Any = None,
) -> FastAPI: ...
```

**Endpoints:**
| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check |
| POST | `/chat` | Non-streaming chat |
| POST | `/chat/stream` | SSE streaming chat |
| GET | `/sessions` | List sessions |
| DELETE | `/sessions/{id}` | Delete session |
| GET | `/skills` | List skills |

---

## Configuration (`loom.config`)

### `LoomConfig`
```python
class LoomConfig(BaseModel):
    default_model: str = ""
    max_iterations: int = 32
    system_preamble: str = ""
    routing_mode: str = "fixed"
    providers: dict[str, ProviderConfig] = {}
    models: list[dict] = []
```

### `resolve_config(cli_overrides, config) -> (base_url, api_key, model)`
Precedence: CLI > env vars > config file > defaults.

**Env vars:** `LOOM_LLM_BASE_URL`, `LOOM_LLM_API_KEY`, `LOOM_LLM_MODEL`

---

## Routing (`loom.routing`)

### `classify_message(text: str) -> MessageCategory`
Returns: `CODING`, `REASONING`, `TRIVIAL`, or `BALANCED`

### `choose_model(message, registry, strengths, default_model) -> str | None`
Selects the best model from the registry based on message category and model strengths.

### `ModelStrengths`
```python
class ModelStrengths:
    speed: int = 5      # 1-10
    cost: int = 5       # 1-10 (higher = cheaper)
    reasoning: int = 5  # 1-10
    coding: int = 5     # 1-10
```

---

## Error Handling (`loom.errors`)

### Exception Hierarchy
```
LLMError
  ├── LLMTransportError (status_code, body)  -- retryable
  └── MalformedOutputError                    -- never retryable
```

### `ClassifiedError`
```python
class ClassifiedError(BaseModel):
    reason: FailoverReason
    retryable: bool
    should_compress: bool
    should_rotate_credential: bool
    should_fallback: bool
    recovery: RecoveryAction
```

### `classify_api_error(error: Exception) -> ClassifiedError`
### `classify_http(status: int, body: str) -> ClassifiedError`

---

## Retry (`loom.retry`)

### `jittered_backoff(attempt, base=2.0, max_delay=60.0, jitter_ratio=0.5) -> float`
### `async with_retry(coro_factory, max_attempts=3) -> Any`
Retries `LLMTransportError` with jittered exponential backoff.
