# RFC 0003 — `SshCallTool` (asyncssh)

- **Status**: Implemented (v0.3, Phase A — full)
- **Author**: —
- **Target release**: 0.3 (after RFC 0002 Phase A)
- **Depends on**: RFC 0002 (for SSH appliers)
- **Extras group**: `loom[ssh]`

## Summary

Add `loom.tools.ssh.SshCallTool` — an agent tool for running commands on remote servers via SSH. Built on `asyncssh`. Authenticates via the `loom.auth` pipeline (RFC 0002) using `SshPasswordApplier` or `SshKeyApplier`. Shape parallels `HttpCallTool` so agents see a consistent tool contract across transports.

## Motivation

Agentic ops workflows need to reach beyond HTTP:

- SSH into a host to fetch a log file the platform's API doesn't expose.
- Run a diagnostic command on a stuck integration node.
- Restart a process on a remote server when an incident fires.
- Tail a systemd unit's journal.

Every consumer building an ops agent will eventually need SSH. Putting it in loom means each consumer doesn't reimplement transport + auth. Building it on top of RFC 0002 from day one means the credential pipeline (policies, audit, rotation) applies uniformly across HTTP and SSH.

## Non-goals

- Not a full interactive SSH client. This is one-shot command execution, return stdout/stderr/exit code.
- Not a port forwarder. `asyncssh` supports port forwarding; exposing it is a separate RFC if ever needed.
- Not SFTP. If file transfer is needed, a future `SftpTool` RFC handles that.
- Not host key management UX. Default strict known-hosts; configurable but no in-tool UI for accepting new fingerprints.

## Design

### Tool API

```python
from loom.tools.ssh import SshCallTool

tool = SshCallTool(
    credential_resolver=resolver,   # from RFC 0002
    known_hosts_path=None,          # None => use user default; False => disable (dev only)
    connect_timeout=10.0,
    command_timeout=60.0,
    max_output_bytes=10240,
)
```

### Tool spec (what the agent sees)

```json
{
  "name": "ssh_call",
  "description": "Run a command on a remote host over SSH. Credentials are resolved automatically — do not include passwords or keys. Returns exit code, stdout, stderr.",
  "parameters": {
    "type": "object",
    "properties": {
      "host": {
        "type": "string",
        "description": "Scope key; resolves to a hostname and credential via the credential resolver."
      },
      "command": {
        "type": "string",
        "description": "Command to execute on the remote host. Use quoting carefully."
      },
      "stdin": {
        "type": "string",
        "description": "Optional stdin to feed the command."
      },
      "timeout": {
        "type": "number",
        "description": "Override command_timeout (seconds). Bounded by tool default."
      }
    },
    "required": ["host", "command"]
  }
}
```

### Output

```python
ToolResult(
    text="<stdout, truncated if over max_output_bytes>",
    metadata={
        "exit_code": 0,
        "stderr": "<stderr, truncated>",
        "truncated_stdout": False,
        "truncated_stderr": False,
        "duration_ms": 317,
    },
)
```

On transport error (connection refused, auth failure, timeout), `ToolResult(text="SSH error: <classified message>", metadata={"exit_code": None, "error_class": "<one of: auth|timeout|transport|unknown>"})`.

### Auth flow

1. Tool receives `{host: "prod-oic-db-1", command: "..."}`.
2. Calls `resolver.resolve_for(scope="prod-oic-db-1", transport="ssh")`.
3. Resolver returns a dict: `{username: str, auth: SshAuth}` where `SshAuth` is one of `SshPasswordAuth(password=...)`, `SshKeyAuth(key_pem=..., passphrase=...)`, or `SshAgentAuth()`.
4. Also returns `hostname` and `port` (from scope metadata if not overridden in the tool call).
5. asyncssh establishes the connection with the resolved creds.

### Scope metadata

Beyond the secret, SSH needs: hostname, port, username. Three options for where these live:

**Option A (chosen)**: The credential resolver's context dict carries them.

```python
# Scope registered once
store.put(
    "prod-oic-db-1",
    {"type": "ssh_private_key", "key_pem": "...", "passphrase": None},
    metadata={"hostname": "db1.prod.oic", "port": 22, "username": "ops"},
)
```

The SSH applier reads `hostname`/`port`/`username` from `SecretMetadata` when building the connection args.

**Option B (rejected)**: Require consumers to pass `host`, `port`, `username` explicitly to the tool. Simpler but shifts memorization onto the agent; defeats the "agent never sees connection details" principle.

### `SshPasswordApplier` / `SshKeyApplier`

Defined in RFC 0002. This RFC just consumes them.

```python
class SshPasswordApplier:
    secret_type = "password"  # or "basic_auth" if reusing
    async def apply(self, secret, context) -> SshConnectArgs: ...

class SshKeyApplier:
    secret_type = "ssh_private_key"
    async def apply(self, secret, context) -> SshConnectArgs: ...
```

`SshConnectArgs` is the normalized shape the tool feeds to `asyncssh.connect()`.

### Host key checking

- Default: strict, uses `~/.ssh/known_hosts` (asyncssh default).
- Override: `known_hosts_path="/path/to/known_hosts"` for custom paths.
- Dev escape hatch: `known_hosts=False` disables checking. Prints a `[LOOM SECURITY] host key checking DISABLED` warning on every invocation. Not recommended. Will be tagged by the skill guard as a risky config.

### Timeouts

- `connect_timeout` — how long to wait for the TCP/SSH handshake before giving up.
- `command_timeout` — how long to wait for the command to finish after connection. Configurable per-call, capped by the tool-level setting.

### Output truncation

Same philosophy as `HttpCallTool`: truncate both stdout and stderr at `max_output_bytes` with a visible marker. The model needs to see *some* of the output on failure to reason about it; don't swallow it silently.

### Concurrency and connection reuse

First version: one connection per invocation, closed on completion. Simple, safe, slower for chatty agents.

Follow-up (not in this RFC): a connection pool keyed by `(host, user)` with an idle-timeout. Gated on measured demand.

## Dependencies

- `asyncssh >= 2.14` — pure Python, well-maintained.
- Transitively pulls `cryptography` — already a loom dep for Fernet.
- Extras group: `pip install "loom[ssh]"`.

## Backward compatibility

New module. Additive. No impact on existing consumers.

## Security considerations

- **Command injection.** The agent constructs a command string. If the agent is prompted to target a file path from user input without quoting, this is a shell injection vector. Mitigations:
  - Document in the tool description that `command` is executed in a shell.
  - Recommend the skill guard flag skills that build `command` from unquoted string interpolation.
  - Future: add a structured `argv` variant (array of args, no shell) as a follow-up RFC.
- **Privilege escalation.** If the resolved user has `sudo` without password, the agent effectively runs as root. This is a deployment decision, not a loom problem, but the doc should call it out.
- **Host key pinning.** Default strict. Disabling is a loud, per-call warning. Skill guard flags `known_hosts=False` in configs.
- **Credential exposure on error.** asyncssh error messages can sometimes include the key path or host info; scrub via `loom.llm.redact` before returning in `ToolResult`.
- **Timeout as DoS protection.** Default `command_timeout=60` prevents an agent from hanging forever on a wedged remote process.

## Risks and tradeoffs

- **Alpha security-critical code.** Same caveat as RFC 0002.
- **asyncssh is ~2MB installed.** Gated behind `loom[ssh]` extras — not paid by consumers who don't need SSH.
- **No argv mode in v1.** Shell-quoting is the user's responsibility in the first cut. Follow-up RFC adds `argv` for safer scripted invocations.

## Test plan

- Spin up a local sshd (via Docker or `asyncssh.create_server`) for integration tests.
- Roundtrip: password auth, key auth, bad creds → `error_class="auth"`.
- Command runs, exit=0, captures stdout/stderr.
- Command exits non-zero; assert `exit_code` and stderr captured.
- Timeout: command sleeps 10s with `timeout=1`; assert `error_class="timeout"` and connection cleaned up.
- Output truncation: command prints >max; assert truncation markers in text + metadata.
- Host key mismatch: fresh `known_hosts`; assert auth error with `error_class="auth"` and fingerprint in stderr (redacted).
- Integration with `CredentialResolver` from RFC 0002: end-to-end agent tool call.

## Open questions

- **Keep connections open for multi-step agent plans?** Proposal: no, v1. Each tool call is independent. Revisit with pooling RFC when demand is measured.
- **Should we ship a sibling `SftpTool` now?** Proposal: no. Wait for demand; SSH-plus-stdout covers most diagnostic workflows.
- **`argv` vs `command`?** Proposal: ship `command` only in v1 (shell semantics). Add `argv` in a follow-up if injection surface proves painful.
