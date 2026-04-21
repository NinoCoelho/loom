# RFC 0002 — Credentials, Appliers, and Policies

> **Phase A landed in v0.3.** `loom.store.secrets.SecretStore` (Fernet-encrypted typed secrets)
> and `loom.auth` (HTTP appliers + `CredentialResolver`) are available as of v0.3.
> Phase B (policies + HITL) and Phase C (KeychainStore) are not yet implemented.

- **Status**: Draft
- **Author**: —
- **Target release**: 0.3 (phased)
- **Depends on**: none (but synergistic with RFC 0001)
- **Blocks**: RFC 0003 (SSH tool), LLM-provider key rotation

## Summary

Promote credential management from a consumer concern to a first-class loom subsystem. Split into three decoupled layers:

1. **`loom.store.secrets`** — typed, scope-keyed, Fernet-encrypted secret vault (extension of the existing module).
2. **`loom.auth.appliers`** — transport-agnostic adapters that turn a secret into ready-to-use material for a specific transport (HTTP headers, SSH connect args, LLM provider constructor args, etc.).
3. **`loom.auth.policies`** — HITL-gated credential usage policies that sit on top of `loom.hitl` (autonomous / notify-before / notify-after / time-boxed / one-shot).

Each layer is independently usable. Consumers who only need a secrets vault don't pay for appliers or policies.

## Motivation

Every agent framework consumer eventually needs:

- A secure place to put passwords, API keys, OAuth2 client creds, SSH keys.
- A way to turn those secrets into transport-ready material (headers, connection args) without the agent touching bytes of a secret.
- A human-in-the-loop policy for when the agent may use a secret unattended vs when it must ask first.

Helyx has built all three (`CredentialStore`, `TokenExchanger`, credential policies). They are not integration-specific — they belong in loom.

Without this RFC, every new consumer (Nexus, future users) reimplements this stack. With it, they compose.

## Non-goals

- Not a password manager. Scope is "credentials agents use to access systems and services."
- Not a KMS. `Fernet` with a locally-managed key is the at-rest encryption primitive; enterprise KMS integration is a separate concern.
- Not a replacement for OS keychains. A local `Fernet` vault is the default; consumers can plug in their own `SecretBackend` for keychain/HSM integration.
- Not hierarchical ACL on secrets. Scope is a flat opaque string; consumers structure it however they want.

## Layer 1 — `loom.store.secrets`

### Typed secrets

Today: `loom.store.secrets` stores opaque strings.

Proposed: each secret carries a `type` tag and a shape-appropriate payload.

```python
from typing import Literal, TypedDict, Union

class PasswordSecret(TypedDict):
    type: Literal["password"]
    value: str

class ApiKeySecret(TypedDict):
    type: Literal["api_key"]
    value: str

class BasicAuthSecret(TypedDict):
    type: Literal["basic_auth"]
    username: str
    password: str

class BearerTokenSecret(TypedDict):
    type: Literal["bearer_token"]
    token: str
    expires_at: str | None  # ISO8601

class OAuth2ClientCredentialsSecret(TypedDict):
    type: Literal["oauth2_client_credentials"]
    client_id: str
    client_secret: str
    token_url: str
    scopes: list[str] | None

class SshPrivateKeySecret(TypedDict):
    type: Literal["ssh_private_key"]
    key_pem: str               # PEM-encoded
    passphrase: str | None

Secret = Union[
    PasswordSecret, ApiKeySecret, BasicAuthSecret,
    BearerTokenSecret, OAuth2ClientCredentialsSecret,
    SshPrivateKeySecret,
]
```

New types can be added in follow-up RFCs without breaking consumers.

### Scope keys

Each secret is stored under a `scope` — an opaque string the consumer controls. Examples:

- helyx: `prod-oic-us-east` (target name)
- nexus: `agent:coder:openai` (compound)
- multi-tenant: `tenant/prod/oic-us-east` (hierarchical-by-convention)

Loom imposes no structure on scope strings. Conventions are per-consumer.

### Store API

```python
class SecretStore:
    async def put(self, scope: str, secret: Secret, *, metadata: dict | None = None) -> str: ...
    async def get(self, scope: str) -> Secret: ...
    async def list(self, scope_prefix: str | None = None) -> list[SecretMetadata]: ...
    async def revoke(self, scope: str) -> bool: ...  # True if a secret existed; idempotent
    async def rotate(self, scope: str, new_secret: Secret) -> str: ...
```

### Storage backends

- `FernetFileStore` — default, at-rest encrypted JSON file under `$LOOM_HOME/secrets.db` (Fernet key in `$LOOM_HOME/keys/secrets.key` or from `LOOM_SECRET_KEY` env).
- `InMemoryStore` — for tests.
- `KeychainStore` (future) — macOS Keychain / Linux Secret Service.

Pluggable via `SecretBackend` protocol.

## Layer 2 — `loom.auth.appliers`

An **applier** turns a `Secret` into transport-ready material. Each applier knows **one secret type × one transport**.

```python
from typing import Generic, Protocol, TypeVar

S = TypeVar("S", bound=Secret)
T = TypeVar("T")  # transport-specific output type

class Applier(Protocol, Generic[S, T]):
    secret_type: str
    async def apply(self, secret: S, context: dict) -> T: ...
```

### Reference appliers (ship in v0.3)

| Applier | Secret type | Transport | Output |
|---|---|---|---|
| `BasicHttpApplier` | `basic_auth` | HTTP | `dict[str, str]` headers |
| `BearerHttpApplier` | `bearer_token` | HTTP | `dict[str, str]` headers |
| `OAuth2CCHttpApplier` | `oauth2_client_credentials` | HTTP | `dict[str, str]` headers (with in-memory token cache) |
| `ApiKeyHeaderApplier` | `api_key` | HTTP | `dict[str, str]` headers (config: header name) |
| `ApiKeyStringApplier` | `api_key` | LLM provider / generic | `str` |

### Future appliers (follow-up RFCs)

- `SshPasswordApplier`, `SshKeyApplier` — RFC 0003
- `SigV4Applier` (AWS) — separate
- `JwtBearerApplier` (client-assertion) — separate

### OAuth2 token caching

`OAuth2CCHttpApplier` caches access tokens in-process keyed by `(scope, token_url, scopes)`. Cache invalidation on `rotate()` happens via a version counter on the `SecretStore`.

## Layer 3 — `loom.auth.policies`

A **policy** decides whether the agent may use a secret *right now*, possibly asking a human first.

```python
from enum import Enum

class PolicyMode(Enum):
    AUTONOMOUS = "autonomous"            # no gate
    NOTIFY_BEFORE = "notify_before"      # human must approve each use
    NOTIFY_AFTER = "notify_after"        # fire-and-log
    TIME_BOXED = "time_boxed"            # autonomous inside a window
    ONE_SHOT = "one_shot"                # single use, then revoked

class CredentialPolicy:
    scope: str
    mode: PolicyMode
    window_start: datetime | None         # TIME_BOXED
    window_end: datetime | None
    uses_remaining: int | None            # ONE_SHOT / counted

class PolicyEnforcer:
    async def gate(self, scope: str, context: dict) -> GateDecision: ...
    # Raises CredentialDenied or returns a decision with any required HITL prompt already resolved.
```

`PolicyEnforcer.gate()` integrates with `loom.hitl.HitlBroker` for approval prompts. `NOTIFY_BEFORE` produces a `HitlRequest`; the enforcer blocks until a response arrives (or a `TIMEOUT_SENTINEL` resolves it per policy).

## The resolution pipeline

Putting all three layers together:

```
scope → PolicyEnforcer.gate()   ─── may raise CredentialDenied
      → SecretStore.get()
      → Applier.apply(secret, context)
      → transport-ready material
```

A convenience class wraps this:

```python
class CredentialResolver:
    def __init__(self, store: SecretStore, enforcer: PolicyEnforcer, appliers: dict): ...
    async def resolve_for(self, scope: str, transport: str, context: dict) -> Any: ...
```

`resolve_for(scope="prod-oic", transport="http", context={"base_url": "..."})` returns HTTP headers.
`resolve_for(scope="grafana-api", transport="llm_api_key")` returns the key string.

## How consumers use it

### Helyx

```python
# At startup
store = FernetFileStore(path=helyx_home() / "secrets.db")
enforcer = PolicyEnforcer(hitl=broker, policy_store=policy_store)
appliers = {
    ("basic_auth", "http"): BasicHttpApplier(),
    ("bearer_token", "http"): BearerHttpApplier(),
    ("oauth2_client_credentials", "http"): OAuth2CCHttpApplier(),
}
resolver = CredentialResolver(store, enforcer, appliers)

# At request time (in the HTTP tool's pre_request_hook from RFC 0001)
headers = await resolver.resolve_for(scope=target_name, transport="http")
```

Helyx deletes: `store/credentials.py`, `store/auth.py`, credential-policy enforcement code. Keeps: `TargetStore` (targets bind a scope name + base URL + metadata).

### Nexus

Opt-in. Nexus can keep its current approach and adopt the resolver incrementally. Secrets migrate scope-by-scope.

### LLM provider key rotation

```python
provider = OpenAICompatibleProvider(
    base_url="https://api.openai.com/v1",
    api_key_resolver=lambda: resolver.resolve_for("openai-prod", "llm_api_key"),
    default_model="gpt-4o",
)
```

Providers gain an **optional** `api_key_resolver` callable called per-request. Falls back to the static `api_key` arg for today's behavior. No circular import (`loom.llm` doesn't import `loom.store`).

## Backward compatibility

- `loom.store.secrets` already exists with a simpler API. The new typed API is additive; the legacy `get(scope) -> str` becomes a thin wrapper over `get(scope) -> Secret` unpacked to `.value`.
- Providers' current `api_key: str` arg stays. The resolver is additive.
- No existing loom consumer depends on appliers or policies — these are new.

## Security considerations

- **Fernet key management.** Default: file under `$LOOM_HOME/keys/secrets.key`, mode 0600. Env override: `LOOM_SECRET_KEY`. Document the rotation story: encrypt-to-new-key pass + key file swap.
- **Audit log.** `SecretStore` emits `secret.accessed` / `secret.rotated` / `secret.revoked` events. Consumers can subscribe for their audit pipeline.
- **Decrypted-secret lifetime.** The `Secret` object returned by `get()` is in-memory only; we don't cache it across calls. Appliers may cache *derived* material (OAuth2 tokens) but not the secret itself.
- **Logging redaction.** All `Secret` fields tagged for `loom.llm.redact` so they never appear in logs.

## Risks and tradeoffs

- **Alpha security-critical code.** Loom v0.3 is still alpha; API may still change. Advertise clearly. Consumers using this subsystem sign up for that.
- **Scope abstraction flattens hierarchy.** Multi-tenant agents may want structured scope. Kept as opaque string to avoid over-committing; conventions (`tenant/env/service`) work fine.
- **Optional deps balloon with appliers.** Mitigated by extras groups: `loom[oauth2]` is the base (uses httpx which is already core); `loom[ssh]` adds asyncssh; `loom[aws]` adds SigV4 deps.
- **Policy + HITL coupling.** `PolicyEnforcer` depends on `loom.hitl.HitlBroker`. Consumers not using HITL would only use `AUTONOMOUS` policies; acceptable.

## Phased rollout

- **Phase A (v0.3)**: Layer 1 (typed secrets + FernetFileStore) + Layer 2 (HTTP appliers: Basic, Bearer, OAuth2CC, ApiKey). No policies yet; `AUTONOMOUS` implicit.
- **Phase B (v0.3 or v0.4)**: Layer 3 (policies + HITL integration).
- **Phase C (v0.4+)**: KeychainStore, SigV4, JWT, ACL extensions as needed.

Helyx can migrate after Phase A (secrets + HTTP appliers) and further reduce surface after Phase B.

## Test plan

- Roundtrip: put / get / revoke / rotate for every secret type.
- Applier contracts: output shape matches transport spec (HTTP headers dict, etc.).
- OAuth2CC token cache: one `token_url` call per cache window; invalidates on rotate.
- Policy enforcement: `NOTIFY_BEFORE` blocks until HitlBroker resolves; `TIME_BOXED` gates correctly across windows.
- Integration: HTTP tool with resolver-fed `pre_request_hook` (RFC 0001) round-trip against a local echo server.
- Redaction: every `Secret` field redacted by default in log output.

## Open questions

- Should secrets have an **expiry** field separate from embedded `expires_at` on tokens? Proposal: yes, store-level `expires_at` drives automatic cleanup; applier-level `expires_at` drives token refresh. Separate concerns.
- Should appliers be **registered globally** or **passed explicitly** to the resolver? Proposal: explicit registration via `CredentialResolver.register()` so tests can run with a minimal set and consumers only pay for what they import.
- Should `SecretStore` support **versioning** (keep last N rotations for rollback)? Proposal: out of scope for v0.3. Revisit if demand appears.
