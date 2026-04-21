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
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
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
- `ToolExecStartEvent(type="tool_exec_start", tool_call_id: str, name: str, arguments: str)`
- `ToolExecResultEvent(type="tool_exec_result", tool_call_id: str, name: str, text: str, is_error: bool = False)`
- `LimitReachedEvent(type="limit_reached", iterations: int)`
- `ErrorEvent(type="error", message: str, reason: str | None, status_code: int | None, retryable: bool = False)`
- `DoneEvent(type="done", context: dict = {})`

---

## Agentic Loop (`loom.loop`)

### `AgentConfig`
```python
class AgentConfig:
    max_iterations: int = 32
    model: str | None = None
    system_preamble: str = ""
    extra_tools: list[ToolHandler] = []
    on_before_turn: Callable[[list[ChatMessage]], list[ChatMessage]] | None
    on_after_turn: Callable[[AgentTurn], None] | None
    on_tool_result: Callable[[ToolCall, str], None] | None
    on_event: Callable[[StreamEvent], None] | None
    choose_model: Callable[[list[ChatMessage]], str | None] | None
    limit_message_builder: Callable[[int], str] | None
    affirmatives: set[str]
    negatives: set[str]
    serialize_event: Callable[[StreamEvent], str] | None
    before_llm_call: Callable[[list[ChatMessage], list[ToolSpec]], None] | None
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
| `DelegateTool` | `delegate` | `tools/delegate.py` |
| `EditIdentityTool` | `edit_identity` | `tools/profile.py` |

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

Deprecated. Use `SecretStore` for new code.

---

## Credentials (`loom.auth`, `loom.store.secrets`, `loom.store.keychain`)

### Secret Types (`loom.store.secrets`)

Eight typed dicts that cover common credential shapes:

| TypedDict | `type` literal | Key fields |
|---|---|---|
| `PasswordSecret` | `"password"` | `value: str` |
| `ApiKeySecret` | `"api_key"` | `value: str` |
| `BasicAuthSecret` | `"basic_auth"` | `username: str`, `password: str` |
| `BearerTokenSecret` | `"bearer_token"` | `token: str`, `expires_at: str \| None` |
| `OAuth2ClientCredentialsSecret` | `"oauth2_client_credentials"` | `client_id`, `client_secret`, `token_url`, `scopes` |
| `SshPrivateKeySecret` | `"ssh_private_key"` | `key_pem: str`, `passphrase: str \| None` |
| `AwsSigV4Secret` | `"aws_sigv4"` | `access_key_id`, `secret_access_key`, `session_token`, `region` |
| `JwtSigningKeySecret` | `"jwt_signing_key"` | `private_key_pem`, `algorithm`, `key_id`, `issuer`, `audience`, `subject`, `ttl_seconds` |

`Secret = Union[PasswordSecret, ApiKeySecret, BasicAuthSecret, BearerTokenSecret, OAuth2ClientCredentialsSecret, SshPrivateKeySecret, AwsSigV4Secret, JwtSigningKeySecret]`

### `SecretStore`

Fernet-encrypted, scope-keyed secret store. Key auto-generated at `<store_dir>/keys/secrets.key`; override with `LOOM_SECRET_KEY` env var.

```python
class SecretStore:
    def __init__(self, path: Path, *, key_path: Path | None = None): ...
    async def put(self, scope: str, secret: Secret, *, metadata: dict | None = None) -> str: ...
    async def get(self, scope: str) -> Secret | None: ...
    async def get_metadata(self, scope: str) -> dict | None: ...
    async def list(self, scope_prefix: str | None = None) -> list[SecretMetadata]: ...
    async def revoke(self, scope: str) -> bool: ...     # True if existed; idempotent
    async def rotate(self, scope: str, new_secret: Secret) -> str: ...  # raises KeyError if absent
```

### `KeychainStore`

OS keychain backend (`loom[keychain]`). Identical public protocol to `SecretStore`.

```python
class KeychainStore:
    def __init__(self, service: str = "loom"): ...
    async def put(self, scope: str, secret: Secret, *, metadata: dict | None = None) -> str: ...
    async def get(self, scope: str) -> Secret | None: ...
    async def get_metadata(self, scope: str) -> dict | None: ...
    async def list(self, scope_prefix: str | None = None) -> list[SecretMetadata]: ...
    async def revoke(self, scope: str) -> bool: ...
    async def rotate(self, scope: str, new_secret: Secret) -> str: ...
```

Backed by macOS Keychain / Linux Secret Service / Windows Credential Manager via `keyring`.

### `SecretMetadata`
```python
class SecretMetadata(TypedDict):
    scope: str
    secret_type: str
    created_at: str   # ISO 8601
    version: int
    metadata: dict    # free-form; SSH stores hostname/port/username here
```

---

### Appliers (`loom.auth.appliers`)

| Class | `secret_type` | Transport | Output |
|---|---|---|---|
| `BasicHttpApplier` | `basic_auth` | `http` | `{"Authorization": "Basic <b64>"}` |
| `BearerHttpApplier` | `bearer_token` | `http` | `{"Authorization": "Bearer <token>"}` |
| `OAuth2CCHttpApplier` | `oauth2_client_credentials` | `http` | `{"Authorization": "Bearer <access_token>"}` |
| `ApiKeyHeaderApplier(header_name="X-API-Key")` | `api_key` | `http` | `{header_name: value}` |
| `ApiKeyStringApplier` | `api_key` | `llm_api_key` | raw `str` |
| `SshPasswordApplier` | `password` | `ssh` | `SshConnectArgs` |
| `SshKeyApplier` | `ssh_private_key` | `ssh` | `SshConnectArgs` |
| `SigV4Applier` | `aws_sigv4` | `http` | full headers dict with `Authorization`, `x-amz-date` (`loom[aws]`) |
| `JwtBearerApplier` | `jwt_signing_key` | `http` | `{"Authorization": "Bearer <jwt>"}` (`loom[jwt]`) |

**Applier protocol:**
```python
class Applier(Protocol):
    secret_type: str
    async def apply(self, secret: Secret, context: dict) -> Any: ...
```

---

### `CredentialResolver` (`loom.auth.resolver`)

```python
class CredentialResolver:
    def __init__(
        self,
        store: SecretStore,
        appliers: dict[tuple[str, str], Applier] | None = None,
        *,
        enforcer: PolicyEnforcer | None = None,
        scope_acl: Callable[[str, str, str], bool] | None = None,
    ): ...

    def register(self, applier: Applier, *, transport: str) -> None: ...

    async def resolve_for(
        self,
        scope: str,
        transport: str,
        context: dict | None = None,
    ) -> Any: ...
```

`resolve_for` pipeline: ACL check → `enforcer.gate()` → `store.get()` → `applier.apply()`. Raises `ScopeAccessDenied`, `CredentialDenied`, `ScopeNotFoundError`, or `NoApplierError` on failure.

---

### `PolicyStore` (`loom.auth.policy_store`)

File-backed persistence for `CredentialPolicy` objects (plain JSON, mode 0600).

```python
class PolicyStore:
    def __init__(self, path: Path): ...
    async def put(self, policy: CredentialPolicy) -> None: ...
    async def get(self, scope: str) -> CredentialPolicy | None: ...
    async def delete(self, scope: str) -> bool: ...
    async def list(self, scope_prefix: str | None = None) -> list[CredentialPolicy]: ...
    async def decrement_uses(self, scope: str) -> int: ...  # ONE_SHOT countdown
```

### `CredentialPolicy` (`loom.auth.policies`)

```python
@dataclass(frozen=True)
class CredentialPolicy:
    scope: str
    mode: PolicyMode
    window_start: datetime | None = None   # TIME_BOXED
    window_end: datetime | None = None     # TIME_BOXED
    uses_remaining: int | None = None      # ONE_SHOT / counted
    prompt_message: str | None = None      # NOTIFY_BEFORE custom prompt
```

### `PolicyMode` (`loom.auth.policies`)

```python
class PolicyMode(StrEnum):
    AUTONOMOUS = "autonomous"       # no gate
    NOTIFY_BEFORE = "notify_before" # human must approve each use
    NOTIFY_AFTER = "notify_after"   # fire-and-log
    TIME_BOXED = "time_boxed"       # autonomous inside [window_start, window_end)
    ONE_SHOT = "one_shot"           # single use, then auto-revoked
```

### `PolicyEnforcer` (`loom.auth.enforcer`)

```python
class PolicyEnforcer:
    def __init__(
        self,
        policy_store: PolicyStore,
        hitl: HitlBroker | None = None,
        secret_store: SecretStore | None = None,
    ): ...

    async def gate(self, scope: str, context: dict | None = None) -> GateDecision: ...
    # Raises CredentialDenied on denial; returns GateDecision on success.
```

### `GateDecision` (`loom.auth.enforcer`)

```python
@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    policy: CredentialPolicy | None   # None = implicit AUTONOMOUS
    prompt_resolution: str | None     # NOTIFY_BEFORE answer when approved
    reason: str | None                # denial reason when allowed=False
```

---

### `SshCallTool` (`loom.tools.ssh`)

Requires `loom[ssh]`.

```python
class SshCallTool(ToolHandler):
    def __init__(
        self,
        credential_resolver: CredentialResolver,
        known_hosts_path: str | None = None,  # None = ~/.ssh/known_hosts; False = disable (dev only)
        connect_timeout: float = 10.0,
        command_timeout: float = 60.0,
        max_output_bytes: int = 10240,
    ): ...
```

Tool name: `ssh_call`. Parameters: `host` (scope key), `command` (str), `stdin` (str, optional), `timeout` (float, optional).

On success, `ToolResult.text` is stdout (truncated at `max_output_bytes`). Metadata keys: `exit_code`, `stderr`, `truncated_stdout`, `truncated_stderr`, `duration_ms`.

On error, `ToolResult.metadata["error_class"]` is one of `auth | timeout | transport | unknown`.

---

### Credential error types (`loom.auth.errors`, `loom.auth.enforcer`)

| Exception | Raised when |
|---|---|
| `AuthApplierError` | Base class for all applier errors |
| `SecretExpiredError` | Bearer token `expires_at` is in the past |
| `NoApplierError` | No applier registered for `(secret_type, transport)` |
| `ScopeNotFoundError` | Scope absent from the `SecretStore` |
| `ScopeAccessDenied` | `scope_acl` hook returned `False` |
| `CredentialDenied` | `PolicyEnforcer.gate()` denied access (policy rule or HITL rejection) |

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

---

## Agent Communication Protocol (`loom.acp`)

### `AcpConfig`
```python
class AcpConfig(BaseModel):
    url: str
    timeout: float = 30.0
    retries: int = 2
```

### `AcpCallTool(ToolHandler)`
Tool handler for calling remote agents over WebSocket with Ed25519 authentication.

### `DeviceKeypair`
```python
class DeviceKeypair:
    public_key_b64: str
    def sign_challenge(self, challenge: str) -> str: ...
```

### `load_or_create_keypair(path: Path) -> DeviceKeypair`

---

## MCP Client (`loom.mcp`)

Optional (`pip install "loom[mcp]"`). Connect to external MCP servers and expose their tools as Loom `ToolHandler` instances.

### `McpServerConfig`
```python
class McpServerConfig(BaseModel):
    name: str
    transport: Literal["stdio", "sse"] = "stdio"
    # stdio transport
    command: list[str] | None = None
    env: dict[str, str] = {}
    # sse transport
    url: str | None = None
    headers: dict[str, str] = {}
```

### `McpClient`
Async context manager owning one MCP session.
```python
class McpClient:
    def __init__(self, config: McpServerConfig) -> None: ...
    async def __aenter__(self) -> McpClient: ...
    async def __aexit__(self, *exc) -> None: ...
    async def list_tools(self) -> list[McpToolHandler]: ...
    async def call_tool(self, name: str, args: dict) -> ToolResult: ...
```

### `McpToolHandler(ToolHandler)`
Wraps one remote MCP tool. Constructed by `McpClient.list_tools()`; register with a `ToolRegistry` to make it callable by the agent.

**Usage:**
```python
from loom.mcp import McpClient, McpServerConfig

config = McpServerConfig(name="web", transport="stdio", command=["npx", "-y", "my-mcp-server"])
async with McpClient(config) as client:
    for handler in await client.list_tools():
        tool_registry.register(handler)
    await agent.run_turn(messages)  # client must stay open here
```

---

## HITL Broker (`loom.hitl`)

### `HitlBroker`
```python
class HitlBroker:
    async def ask(self, session_id: str, kind: str, message: str, choices: list[str] | None = None, timeout: float = 120.0) -> str: ...
    def answer(self, session_id: str, request_id: str, answer: str) -> None: ...
    async def subscribe(self, session_id: str) -> AsyncIterator[HitlEvent]: ...
```

### `BrokerAskUserTool(ToolHandler)`
Wraps `HitlBroker.ask()` as a tool handler, scoped to a session ID.

### `HitlRequest`
```python
class HitlRequest(BaseModel):
    request_id: str
    session_id: str
    kind: str
    message: str
    choices: list[str] | None
```

### `HitlEvent`
```python
class HitlEvent(BaseModel):
    type: str  # "request" | "resolved" | "timeout"
    request: HitlRequest | None
    answer: str | None
```

### Constants
- `CURRENT_SESSION_ID` -- context key for session-scoped HITL
- `TIMEOUT_SENTINEL` -- returned when a HITL request times out

---

## Memory Store (`loom.store.memory`)

### `MemoryEntry`
```python
class MemoryEntry:
    key: str
    category: str
    tags: list[str]
    content: str
    created: str
    updated: str
    importance: int = 3
    pinned: bool = False
    access_count: int = 0
```

### `MemoryStore`
```python
class MemoryStore:
    def __init__(self, memory_dir: Path, embedding_provider: EmbeddingProvider | None = None): ...
    async def write(self, key: str, content: str, category: str = "", tags: list[str] = []) -> MemoryEntry: ...
    async def read(self, key: str) -> MemoryEntry | None: ...
    async def search(self, query: str, limit: int = 10) -> list[MemoryEntry]: ...
    async def recall(self, query: str, limit: int = 10) -> list[RecallHit]: ...
    async def delete(self, key: str) -> bool: ...
    async def list_entries(self, category: str = "") -> list[MemoryEntry]: ...
    async def pin(self, key: str) -> bool: ...
    async def set_importance(self, key: str, level: int) -> bool: ...
    async def touch(self, key: str) -> None: ...
```

### `EmbeddingProvider` (Protocol)
```python
class EmbeddingProvider(Protocol):
    dim: int
    async def embed(self, texts: list[str]) -> list[list[float]]: ...
```

### `RecallHit`
```python
class RecallHit(BaseModel):
    entry: MemoryEntry
    score: float
    source: str  # "bm25" | "salience" | "recency" | "hybrid"
```

---

## Home & Identity (`loom.home`, `loom.permissions`, `loom.prompt`)

### `AgentHome`
```python
class AgentHome:
    def __init__(self, root: Path, name: str = "default"): ...
    @property
    def soul_path(self) -> Path: ...
    @property
    def identity_path(self) -> Path: ...
    @property
    def user_path(self) -> Path: ...
    @property
    def skills_dir(self) -> Path: ...
    @property
    def memory_dir(self) -> Path: ...
    @property
    def vault_dir(self) -> Path: ...
    @property
    def sessions_db(self) -> Path: ...
    def ensure_dirs(self) -> None: ...
```

### `AgentPermissions`
```python
class AgentPermissions(BaseModel):
    user_writable: bool = False
    soul_writable: bool = False
    identity_writable: bool = False
    skills_creatable: bool = False
    terminal_allowed: bool = False
    delegate_allowed: bool = False
```

### `PromptSection`
```python
class PromptSection(BaseModel):
    title: str
    body: str
    priority: int = 0
```

### `PromptBuilder`
```python
class PromptBuilder:
    def add(self, section: PromptSection) -> None: ...
    def build(self) -> str: ...
```
