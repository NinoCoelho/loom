"""loom.tools.ssh — SshCallTool: run commands on remote hosts via SSH (RFC 0003).

Authenticates through the loom.auth credential pipeline using
``SshPasswordApplier`` or ``SshKeyApplier``.  The tool spec mirrors
``HttpCallTool`` so agents see a consistent contract across transports.

asyncssh is an optional dependency (``loom[ssh]``).  It is imported lazily
so that importing this module does not break consumers that don't need SSH.

Usage::

    from loom.tools.ssh import SshCallTool

    tool = SshCallTool(
        credential_resolver=resolver,
        known_hosts_path=None,    # None → ~/.ssh/known_hosts (asyncssh default)
        connect_timeout=10.0,
        command_timeout=60.0,
        max_output_bytes=10240,
    )

Security notes
--------------
- Default strict host-key checking.  ``known_hosts=False`` disables it and
  prints a loud ``[LOOM SECURITY]`` warning on every invocation.
- Error messages are scrubbed via ``loom.llm.redact`` before returning so
  key material or credentials don't leak into the agent context.
- ``error_class`` in metadata classifies failures: auth | timeout | transport | unknown.
"""

from __future__ import annotations

import time
import warnings
from typing import TYPE_CHECKING

from loom.llm.redact import redact_sensitive_text
from loom.tools.base import ToolHandler, ToolResult
from loom.tools.utils import truncate_text
from loom.types import ToolSpec

if TYPE_CHECKING:
    from loom.auth.resolver import CredentialResolver




def _classify_error(exc: Exception) -> str:
    """Map an asyncssh exception to one of: auth | timeout | transport | unknown."""
    try:
        import asyncssh
    except ImportError:
        pass
    else:
        if isinstance(exc, asyncssh.DisconnectError):
            return "transport"
        if isinstance(exc, asyncssh.PermissionDenied):
            return "auth"
        if isinstance(exc, asyncssh.HostKeyNotVerifiable):
            return "auth"
        if isinstance(exc, (asyncssh.ConnectionLost, asyncssh.ChannelOpenError)):
            return "transport"
        if isinstance(exc, asyncssh.Error):
            msg = str(exc).lower()
            if any(k in msg for k in ("auth", "permission", "denied", "key", "password")):
                return "auth"
            return "transport"

    exc_type = type(exc).__name__.lower()
    exc_msg = str(exc).lower()

    if "timeout" in exc_type or "timeout" in exc_msg:
        return "timeout"
    if any(k in exc_msg for k in ("auth", "permission", "denied", "key", "password")):
        return "auth"
    if any(k in exc_msg for k in ("connect", "refused", "reset", "broken pipe", "network")):
        return "transport"
    return "unknown"


class SshCallTool(ToolHandler):
    """Run a command on a remote host over SSH.

    Args:
        credential_resolver: A ``CredentialResolver`` configured with SSH appliers.
        known_hosts_path: Path to a known_hosts file, ``None`` for the user default
            (asyncssh default: ``~/.ssh/known_hosts``), or ``False`` to disable
            host-key checking (dev escape hatch — emits a security warning).
        connect_timeout: Seconds to wait for the SSH handshake.
        command_timeout: Maximum seconds to wait for a command to finish.
            Per-call ``timeout`` args are capped to this value.
        max_output_bytes: Truncate stdout and stderr at this many bytes.
    """

    def __init__(
        self,
        credential_resolver: CredentialResolver,
        known_hosts_path: str | None = None,
        connect_timeout: float = 10.0,
        command_timeout: float = 60.0,
        max_output_bytes: int = 10240,
    ) -> None:
        self._resolver = credential_resolver
        self._known_hosts_path = known_hosts_path
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._max_output_bytes = max_output_bytes

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="ssh_call",
            description=(
                "Run a command on a remote host over SSH. "
                "Credentials are resolved automatically — do not include passwords or keys. "
                "Returns exit code, stdout, stderr."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": (
                            "Scope key; resolves to a hostname and credential "
                            "via the credential resolver."
                        ),
                    },
                    "command": {
                        "type": "string",
                        "description": (
                            "Command to execute on the remote host. "
                            "Use quoting carefully — executed in a remote shell."
                        ),
                    },
                    "stdin": {
                        "type": "string",
                        "description": "Optional stdin to feed the command.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Override command_timeout (seconds). Bounded by tool max.",
                    },
                },
                "required": ["host", "command"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        try:
            import asyncio

            import asyncssh
        except ImportError:
            return ToolResult(
                text="SSH error: asyncssh is not installed. Run: pip install 'loom[ssh]'",
                metadata={"exit_code": None, "error_class": "transport"},
            )

        scope: str = args.get("host", "")
        command: str = args.get("command", "")
        stdin_data: str | None = args.get("stdin")
        per_call_timeout: float | None = args.get("timeout")

        # Cap per-call timeout at tool maximum.
        command_timeout: float = self._command_timeout
        if per_call_timeout is not None:
            command_timeout = min(float(per_call_timeout), self._command_timeout)

        # Resolve credentials — the resolver auto-injects scope metadata into context
        # so the SSH applier can read hostname/port/username without extra fetch.
        try:
            connect_args = await self._resolver.resolve_for(
                scope=scope,
                transport="ssh",
            )
        except Exception as exc:
            return ToolResult(
                text="SSH error: credential resolution failed — "
                + redact_sensitive_text(str(exc)),
                metadata={"exit_code": None, "error_class": "auth"},
            )

        # known_hosts handling
        if self._known_hosts_path is False:
            warnings.warn(
                "[LOOM SECURITY] host key checking DISABLED for SSH connection to "
                f"{connect_args.get('host', scope)!r}. This is insecure — enable strict "
                "known_hosts checking in production.",
                stacklevel=2,
            )
            connect_args["known_hosts"] = None  # asyncssh: None = no checking
        elif self._known_hosts_path is not None:
            connect_args["known_hosts"] = self._known_hosts_path
        # else: leave known_hosts unset → asyncssh uses ~/.ssh/known_hosts (strict default)

        connect_args["connect_timeout"] = self._connect_timeout

        t_start = time.monotonic()
        try:
            async with asyncssh.connect(**connect_args) as conn:
                result = await asyncio.wait_for(
                    conn.run(command, input=stdin_data),
                    timeout=command_timeout,
                )
        except TimeoutError:
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            return ToolResult(
                text=f"SSH error: command timed out after {command_timeout}s",
                metadata={
                    "exit_code": None,
                    "error_class": "timeout",
                    "duration_ms": elapsed_ms,
                },
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            error_class = _classify_error(exc)
            safe_msg = redact_sensitive_text(str(exc))
            return ToolResult(
                text=f"SSH error: {safe_msg}",
                metadata={
                    "exit_code": None,
                    "error_class": error_class,
                    "duration_ms": elapsed_ms,
                },
            )

        elapsed_ms = int((time.monotonic() - t_start) * 1000)

        stdout_raw = result.stdout or ""
        stderr_raw = result.stderr or ""
        exit_code = result.exit_status

        stdout_text, stdout_truncated = truncate_text(stdout_raw, self._max_output_bytes)
        stderr_text, stderr_truncated = truncate_text(stderr_raw, self._max_output_bytes)

        return ToolResult(
            text=stdout_text,
            metadata={
                "exit_code": exit_code,
                "stderr": stderr_text,
                "truncated_stdout": stdout_truncated,
                "truncated_stderr": stderr_truncated,
                "duration_ms": elapsed_ms,
            },
        )
