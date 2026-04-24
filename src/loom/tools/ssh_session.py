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

import asyncio
import secrets
import shlex
import time
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loom.llm.redact import redact_sensitive_text
from loom.tools.base import ToolHandler, ToolResult
from loom.tools.utils import truncate_text
from loom.types import ToolSpec

if TYPE_CHECKING:
    from loom.auth.resolver import CredentialResolver


_TMUX_PREFIX = "loom-"
_DONE_MARKER_FMT = "__LOOM_DONE_{n}_{rc}__"
_DONE_PATTERN = "__LOOM_DONE_"


def _classify_error(exc: Exception) -> str:
    try:
        import asyncssh
    except ImportError:
        pass
    else:
        if isinstance(exc, asyncssh.PermissionDenied):
            return "auth"
        if isinstance(exc, asyncssh.HostKeyNotVerifiable):
            return "auth"
        if isinstance(exc, (asyncssh.DisconnectError, asyncssh.ConnectionLost, asyncssh.ChannelOpenError)):
            return "transport"
        if isinstance(exc, asyncssh.Error):
            msg = str(exc).lower()
            if any(k in msg for k in ("auth", "permission", "denied", "key", "password")):
                return "auth"
            return "transport"

    exc_msg = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in exc_msg:
        return "timeout"
    if any(k in exc_msg for k in ("auth", "permission", "denied", "key", "password")):
        return "auth"
    if any(k in exc_msg for k in ("connect", "refused", "reset", "broken pipe", "network")):
        return "transport"
    return "unknown"


@dataclass
class _SessionState:
    scope: str
    session_id: str
    command_counter: int = 0
    tmpdir: str = ""  # remote directory for per-command output files


@dataclass
class _ScopeState:
    connection: Any = None  # asyncssh.SSHClientConnection
    sessions: dict[str, _SessionState] = field(default_factory=dict)


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
        self._resolver = credential_resolver
        self._known_hosts_path = known_hosts_path
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._max_output_bytes = max_output_bytes
        self._poll_interval = poll_interval
        self._scopes: dict[str, _ScopeState] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ spec

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

    # ------------------------------------------------------------------ entry

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
            if action == "open":
                return await self._action_open(scope, conn, args)
            if action == "send":
                return await self._action_send(scope, conn, args)
            if action == "read":
                return await self._action_read(scope, conn, args)
            if action == "close":
                return await self._action_close(scope, conn, args)
            if action == "list":
                return await self._action_list(scope, conn)
            return _err(f"unknown action: {action!r}", "unknown")
        except _ToolError as e:
            return e.result
        except Exception as exc:
            return _err(
                "SSH error: " + redact_sensitive_text(str(exc)),
                _classify_error(exc),
            )

    # ------------------------------------------------------------------ conn

    async def _ensure_connection(self, scope: str):
        try:
            import asyncssh  # noqa: F401
        except ImportError:
            raise _ToolError(
                _err("asyncssh is not installed. Run: pip install 'loom[ssh]'", "transport")
            )

        async with self._lock:
            state = self._scopes.get(scope)
            if state and state.connection is not None and not state.connection.is_closed():
                return state.connection

            import asyncssh

            try:
                connect_args = await self._resolver.resolve_for(scope=scope, transport="ssh")
            except Exception as exc:
                raise _ToolError(
                    _err(
                        "credential resolution failed — " + redact_sensitive_text(str(exc)),
                        "auth",
                    )
                )

            if self._known_hosts_path is False:
                warnings.warn(
                    "[LOOM SECURITY] host key checking DISABLED for SSH connection to "
                    f"{connect_args.get('host', scope)!r}. This is insecure — enable "
                    "strict known_hosts checking in production.",
                    stacklevel=2,
                )
                connect_args["known_hosts"] = None
            elif self._known_hosts_path is not None:
                connect_args["known_hosts"] = self._known_hosts_path

            connect_args["connect_timeout"] = self._connect_timeout

            conn = await asyncssh.connect(**connect_args)
            if state is None:
                state = _ScopeState(connection=conn)
                self._scopes[scope] = state
            else:
                state.connection = conn
            return conn

    async def _run_remote(self, conn, cmd: str, timeout: float | None = None) -> tuple[int, str, str]:
        """Run a one-shot command on the remote; return (rc, stdout, stderr)."""
        result = await asyncio.wait_for(
            conn.run(cmd, check=False),
            timeout=timeout if timeout is not None else self._command_timeout,
        )
        return (
            result.exit_status if result.exit_status is not None else -1,
            result.stdout or "",
            result.stderr or "",
        )

    # ------------------------------------------------------------------ actions

    async def _action_open(self, scope: str, conn, args: dict) -> ToolResult:
        session_id = args.get("session_id") or secrets.token_hex(4)
        if not _valid_session_id(session_id):
            return _err("session_id must match [A-Za-z0-9_-]+", "unknown")

        # Verify tmux exists.
        rc, _, _ = await self._run_remote(conn, "command -v tmux >/dev/null 2>&1")
        if rc != 0:
            return _err("tmux is not installed on the remote host", "transport")

        tmux_name = _TMUX_PREFIX + session_id
        # Idempotent create.
        rc, _, stderr = await self._run_remote(
            conn,
            f"tmux has-session -t {shlex.quote(tmux_name)} 2>/dev/null "
            f"|| tmux new-session -d -s {shlex.quote(tmux_name)}",
        )
        if rc != 0:
            return _err(f"tmux session create failed: {stderr.strip()}", "transport")

        # Per-session tmpdir for command output files.
        tmpdir = f"/tmp/loom-session-{session_id}"
        rc, _, stderr = await self._run_remote(conn, f"mkdir -p {shlex.quote(tmpdir)}")
        if rc != 0:
            return _err(f"mkdir tmpdir failed: {stderr.strip()}", "transport")

        state = self._scopes[scope]
        if session_id not in state.sessions:
            state.sessions[session_id] = _SessionState(
                scope=scope, session_id=session_id, tmpdir=tmpdir
            )

        return ToolResult(
            text=f"session opened: {session_id}",
            metadata={
                "session_id": session_id,
                "tmux_name": tmux_name,
                "action": "open",
            },
        )

    async def _action_send(self, scope: str, conn, args: dict) -> ToolResult:
        session_id = args.get("session_id") or ""
        command = args.get("command") or ""
        if not session_id:
            return _err("send requires 'session_id'", "unknown")
        if not command:
            return _err("send requires 'command'", "unknown")

        state = self._scopes[scope].sessions.get(session_id)
        if state is None:
            # Allow adopting an already-opened session if the tmux exists remotely.
            tmux_name = _TMUX_PREFIX + session_id
            rc, _, _ = await self._run_remote(
                conn, f"tmux has-session -t {shlex.quote(tmux_name)} 2>/dev/null"
            )
            if rc != 0:
                return _err(
                    f"session {session_id!r} not open — call action=open first",
                    "unknown",
                )
            tmpdir = f"/tmp/loom-session-{session_id}"
            await self._run_remote(conn, f"mkdir -p {shlex.quote(tmpdir)}")
            state = _SessionState(scope=scope, session_id=session_id, tmpdir=tmpdir)
            self._scopes[scope].sessions[session_id] = state

        per_call_timeout = args.get("timeout")
        command_timeout = self._command_timeout
        if per_call_timeout is not None:
            command_timeout = min(float(per_call_timeout), self._command_timeout)

        state.command_counter += 1
        n = state.command_counter
        out_file = f"{state.tmpdir}/cmd-{n}.out"
        done_marker = _DONE_MARKER_FMT.format(n=n, rc="$?")
        # Build the wrapped command. We escape the user command into single
        # quotes via shlex.quote and wrap with a shell eval so redirection
        # affects only the user command, not the marker echo.
        wrapped = (
            f"eval {shlex.quote(command)} "
            f"> {shlex.quote(out_file)} 2>&1; "
            f"echo {done_marker}"
        )
        tmux_name = _TMUX_PREFIX + session_id
        send_cmd = (
            f"tmux send-keys -t {shlex.quote(tmux_name)} "
            f"{shlex.quote(wrapped)} Enter"
        )

        t_start = time.monotonic()
        rc, _, stderr = await self._run_remote(conn, send_cmd)
        if rc != 0:
            return _err(f"tmux send-keys failed: {stderr.strip()}", "transport")

        # Poll for completion.
        expected_prefix = f"__LOOM_DONE_{n}_"
        exit_code: int | None = None
        deadline = t_start + command_timeout
        while True:
            if time.monotonic() > deadline:
                return ToolResult(
                    text=f"SSH error: command timed out after {command_timeout}s "
                    f"(session={session_id}, seq={n}). The command may still be running; "
                    f"use action=read to inspect pane.",
                    metadata={
                        "exit_code": None,
                        "error_class": "timeout",
                        "session_id": session_id,
                        "seq": n,
                        "duration_ms": int((time.monotonic() - t_start) * 1000),
                    },
                )
            rc, pane, _ = await self._run_remote(
                conn,
                f"tmux capture-pane -p -S -200 -t {shlex.quote(tmux_name)}",
            )
            if rc == 0 and expected_prefix in pane:
                # Extract the exit code from the marker.
                idx = pane.rfind(expected_prefix)
                tail = pane[idx + len(expected_prefix):]
                end = tail.find("__")
                if end > 0:
                    try:
                        exit_code = int(tail[:end])
                    except ValueError:
                        exit_code = None
                break
            await asyncio.sleep(self._poll_interval)

        # Read the captured output file.
        rc, stdout_raw, read_err = await self._run_remote(
            conn, f"cat {shlex.quote(out_file)}"
        )
        if rc != 0:
            stdout_raw = ""  # treat missing file as empty

        stdout_text, stdout_trunc = truncate_text(stdout_raw, self._max_output_bytes)

        return ToolResult(
            text=stdout_text,
            metadata={
                "exit_code": exit_code,
                "session_id": session_id,
                "seq": n,
                "truncated_stdout": stdout_trunc,
                "duration_ms": int((time.monotonic() - t_start) * 1000),
            },
        )

    async def _action_read(self, scope: str, conn, args: dict) -> ToolResult:
        session_id = args.get("session_id") or ""
        if not session_id:
            return _err("read requires 'session_id'", "unknown")
        lines = int(args.get("lines") or 200)
        tmux_name = _TMUX_PREFIX + session_id

        rc, _, _ = await self._run_remote(
            conn, f"tmux has-session -t {shlex.quote(tmux_name)} 2>/dev/null"
        )
        if rc != 0:
            return _err(f"session {session_id!r} is not open", "unknown")

        rc, pane, stderr = await self._run_remote(
            conn,
            f"tmux capture-pane -p -S -{lines} -t {shlex.quote(tmux_name)}",
        )
        if rc != 0:
            return _err(f"capture-pane failed: {stderr.strip()}", "transport")

        text, trunc = truncate_text(pane, self._max_output_bytes)
        return ToolResult(
            text=text,
            metadata={
                "session_id": session_id,
                "truncated_stdout": trunc,
                "lines": lines,
            },
        )

    async def _action_close(self, scope: str, conn, args: dict) -> ToolResult:
        session_id = args.get("session_id") or ""
        if not session_id:
            return _err("close requires 'session_id'", "unknown")
        tmux_name = _TMUX_PREFIX + session_id
        tmpdir = f"/tmp/loom-session-{session_id}"

        await self._run_remote(
            conn, f"tmux kill-session -t {shlex.quote(tmux_name)} 2>/dev/null; :"
        )
        await self._run_remote(conn, f"rm -rf {shlex.quote(tmpdir)}")
        self._scopes[scope].sessions.pop(session_id, None)

        return ToolResult(
            text=f"session closed: {session_id}",
            metadata={"session_id": session_id, "action": "close"},
        )

    async def _action_list(self, scope: str, conn) -> ToolResult:
        rc, stdout, _ = await self._run_remote(
            conn,
            "tmux list-sessions -F '#S' 2>/dev/null | grep "
            f"'^{_TMUX_PREFIX}' || true",
        )
        names = [
            line[len(_TMUX_PREFIX):]
            for line in stdout.splitlines()
            if line.startswith(_TMUX_PREFIX)
        ]
        return ToolResult(
            text="\n".join(names) if names else "(no loom sessions)",
            metadata={"sessions": names, "action": "list"},
        )

    # ------------------------------------------------------------------ cleanup

    async def aclose(self) -> None:
        """Close all cached SSH connections. Does not kill remote tmux sessions."""
        for state in self._scopes.values():
            conn = state.connection
            if conn is not None and not conn.is_closed():
                conn.close()
                try:
                    await conn.wait_closed()
                except Exception:
                    pass
        self._scopes.clear()


# ---------------------------------------------------------------------- helpers


class _ToolError(Exception):
    def __init__(self, result: ToolResult) -> None:
        self.result = result


def _err(msg: str, error_class: str) -> ToolResult:
    return ToolResult(
        text=f"SSH error: {msg}",
        metadata={"exit_code": None, "error_class": error_class},
        is_error=True,
    )


def _valid_session_id(s: str) -> bool:
    if not s:
        return False
    return all(c.isalnum() or c in ("_", "-") for c in s)
