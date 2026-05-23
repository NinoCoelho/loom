"""loom.tools.ssh_session — SshSessionTool: persistent remote shells via tmux.

Unlike :class:`~loom.tools.ssh.SshCallTool`, which opens a fresh SSH channel
for each command, this tool drives a persistent tmux session on the remote
host. Shell state (cwd, env vars, activated virtualenvs, background jobs)
survives across invocations, enabling long-running commands, interactive
programs, and multi-step workflows that depend on earlier state.

Design
------
For each ``(scope, session_id)`` pair we maintain a tmux session named
``loom-<session_id>`` on the remote host. Commands are executed via
``tmux send-keys`` with a marker protocol that lets us demarcate per-command
output and capture the exit code:

    {{ <user command> ; }} > <tmpfile> 2>&1; echo __LOOM_DONE_<n>_$?__

We then poll ``tmux capture-pane`` until the done-marker appears, read the
tmpfile for clean output, and return. The tmux session persists across
SSH reconnects — if the SSH transport drops while a command is running,
the command keeps running and can be inspected on the next call.

Actions (selected via ``action`` arg):
  * ``open``  — ensure a tmux session exists (idempotent).
  * ``send``  — run a command; wait for completion; return stdout/stderr/exit.
  * ``read``  — capture current pane buffer (for interactive programs or
    tailing output from a still-running command).
  * ``close`` — kill the tmux session and remove temp files.
  * ``list``  — list loom-managed tmux sessions on the remote.

Requirements
------------
- ``tmux`` installed on the remote host (detected on first ``open``).
- asyncssh installed locally (``loom[ssh]``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loom.llm.redact import redact_sensitive_text
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec

from ._classify import _classify_error
from ._helpers import _ToolError, _err
from ._pool import SshConnectionPool
from ._tmux import TmuxManager

if TYPE_CHECKING:
    from loom.auth.resolver import CredentialResolver


class SshSessionTool(ToolHandler):
    """Run commands inside a persistent tmux session on a remote host.

    Args:
        credential_resolver: :class:`CredentialResolver` configured with SSH appliers.
        known_hosts_path: Path to ``known_hosts``, ``None`` for asyncssh default,
            or ``False`` to disable host-key checking (emits a security warning).
        connect_timeout: Seconds for the SSH handshake.
        command_timeout: Max seconds to wait for a single command to finish.
            Per-call ``timeout`` values are capped to this.
        max_output_bytes: Truncate stdout/stderr at this many bytes.
        poll_interval: Seconds between tmux capture-pane polls while waiting.
    """

    def __init__(
        self,
        credential_resolver: CredentialResolver,
        known_hosts_path: str | bool | None = None,
        connect_timeout: float = 10.0,
        command_timeout: float = 60.0,
        max_output_bytes: int = 10240,
        poll_interval: float = 0.2,
    ) -> None:
        self._pool = SshConnectionPool(
            credential_resolver=credential_resolver,
            known_hosts_path=known_hosts_path,
            connect_timeout=connect_timeout,
            command_timeout=command_timeout,
        )
        self._tmux = TmuxManager(
            pool=self._pool,
            tool=self,
            max_output_bytes=max_output_bytes,
            poll_interval=poll_interval,
            command_timeout=command_timeout,
        )
        self._scopes = self._pool._scopes

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="ssh_session",
            description=(
                "Run commands inside a persistent tmux-backed shell on a remote host. "
                "Shell state (cwd, env vars, background jobs) survives across calls. "
                "Actions: open, send, read, close, list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["open", "send", "read", "close", "list"],
                        "description": "Session operation to perform.",
                    },
                    "host": {
                        "type": "string",
                        "description": "Scope key; resolves to hostname + credential.",
                    },
                    "session_id": {
                        "type": "string",
                        "description": (
                            "Identifier for the tmux session within this host. "
                            "Omit on 'open' to auto-generate; required for send/read/close."
                        ),
                    },
                    "command": {
                        "type": "string",
                        "description": "Command to execute (action=send).",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Override command_timeout seconds (capped).",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of pane lines to capture (action=read). Default 200.",
                    },
                },
                "required": ["action", "host"],
            },
        )

    async def _ensure_connection(self, scope: str):
        return await self._pool.ensure_connection(scope)

    async def _run_remote(
        self, conn, cmd: str, timeout: float | None = None
    ) -> tuple[int, str, str]:
        return await self._pool.run_remote(conn, cmd, timeout)

    async def invoke(self, args: dict) -> ToolResult:
        action = args.get("action", "")
        scope = args.get("host", "")
        if not action:
            return _err("missing 'action'", "unknown")
        if not scope:
            return _err("missing 'host'", "unknown")

        try:
            conn = await self._ensure_connection(scope)
        except _ToolError as e:
            return e.result
        except Exception as exc:
            return _err(
                "connection failed — " + redact_sensitive_text(str(exc)),
                _classify_error(exc),
            )

        try:
            return await self._tmux.dispatch(action, scope, conn, args)
        except _ToolError as e:
            return e.result
        except Exception as exc:
            return _err(
                "SSH error: " + redact_sensitive_text(str(exc)),
                _classify_error(exc),
            )

    async def aclose(self) -> None:
        """Close all cached SSH connections. Does not kill remote tmux sessions."""
        await self._pool.aclose()
