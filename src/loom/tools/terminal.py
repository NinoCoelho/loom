"""The ``terminal`` tool — run a shell command after the user approves.

Built on top of :class:`loom.hitl.HitlBroker` so every shell invocation
flows through the same HITL primitive as ``ask_user``. YOLO short-circuits
the confirm prompt; skills that have already confirmed the action with the
user can pass ``require_approval=False`` to skip a redundant second prompt.

Security posture:
  * Always asks the user by default — no silent exec.
  * ``asyncio.create_subprocess_shell`` with a required timeout;
    processes that exceed it are terminated, not left orphaned.
  * ``start_new_session=True`` ensures child processes share a process
    group so ``os.killpg`` kills the entire tree (no orphaned grandchildren).
  * ``CancelledError`` from user-initiated turn cancellation kills the
    process group and returns partial output.
  * Optional ``on_output`` callback streams stdout/stderr chunks as they
    arrive so the host can forward them to a live UI.
  * Output truncation on each stream (stdout/stderr) keeps the
    tool-result envelope bounded.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loom.hitl.broker import CURRENT_SESSION_ID, TIMEOUT_SENTINEL, HitlBroker
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec

_DEFAULT_TIMEOUT_SECONDS = 60
_MAX_TIMEOUT_SECONDS = 600
_DEFAULT_STREAM_CHAR_LIMIT = 4000  # stdout + stderr each, so ~8KB envelope
_APPROVAL_TIMEOUT_SECONDS = 300

TERMINAL_TOOL_SPEC = ToolSpec(
    name="terminal",
    description=(
        "Run a shell command on the user's local machine and return its "
        "output. Requires the user to approve each run (or YOLO mode). "
        "Prefer purpose-built tools when they fit — use `terminal` only "
        "when the action needs a local CLI (e.g. `git log`, `jq`, `ls`)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "Shell command as a single string. Runs through "
                    "the user's default shell so pipes and redirects "
                    "work."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory (absolute or ``~``-prefixed). "
                    "Defaults to the server's current directory. Bad "
                    "paths return a clear error instead of silent "
                    "fallback."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    "Kill the process if it runs longer than this. "
                    f"Default {_DEFAULT_TIMEOUT_SECONDS}. Max "
                    f"{_MAX_TIMEOUT_SECONDS} — anything longer should "
                    "be a background job, not a tool call."
                ),
            },
        },
        "required": ["command"],
    },
)


@dataclass(frozen=True)
class TerminalResult:
    ok: bool
    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    timed_out: bool
    denied: bool
    error: str | None = None

    def to_text(self) -> str:
        return json.dumps(
            {
                "ok": self.ok,
                "exit_code": self.exit_code,
                "stdout": self.stdout,
                "stderr": self.stderr,
                "stdout_truncated": self.stdout_truncated,
                "stderr_truncated": self.stderr_truncated,
                "duration_ms": self.duration_ms,
                "timed_out": self.timed_out,
                "denied": self.denied,
                "error": self.error,
            },
            ensure_ascii=False,
        )


def kill_proc_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate the process group of *proc* (SIGTERM → grace → SIGKILL).

    Safe to call from any thread — catches ``ProcessLookupError`` and
    ``PermissionError`` so the caller never needs to handle them.
    """
    if proc.returncode is not None:
        return
    pid = proc.pid
    if pid is None:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


class TerminalTool(ToolHandler):
    """Shell-with-approval tool wired through a :class:`HitlBroker`.

    Resolves the active session via :data:`loom.hitl.CURRENT_SESSION_ID`;
    the host server must ``.set()`` it before entering the agent loop.
    """

    def __init__(
        self,
        broker: HitlBroker,
        *,
        yolo_getter: Callable[[], bool] | None = None,
        default_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        max_timeout: float = _MAX_TIMEOUT_SECONDS,
        stream_char_limit: int = _DEFAULT_STREAM_CHAR_LIMIT,
        approval_timeout: int = _APPROVAL_TIMEOUT_SECONDS,
        on_output: Callable[[str, str], Awaitable[None]] | None = None,
        proc_register: Callable[[asyncio.subprocess.Process], None] | None = None,
        proc_unregister: Callable[[], None] | None = None,
    ) -> None:
        self._broker = broker
        self._yolo = yolo_getter or (lambda: False)
        self._default_timeout = default_timeout
        self._max_timeout = max_timeout
        self._stream_char_limit = stream_char_limit
        self._approval_timeout = approval_timeout
        self._on_output = on_output
        self._proc_register = proc_register
        self._proc_unregister = proc_unregister

    @property
    def tool(self) -> ToolSpec:
        return TERMINAL_TOOL_SPEC

    async def invoke(self, args: dict[str, Any]) -> ToolResult:
        result = await self._invoke_inner(args)
        return ToolResult(text=result.to_text(), is_error=not result.ok)

    async def _invoke_inner(self, args: dict[str, Any]) -> TerminalResult:
        command = args.get("command")
        if not isinstance(command, str) or not command.strip():
            return _error("`command` is required and must be a non-empty string")
        command = command.strip()

        cwd_raw = args.get("cwd")
        if cwd_raw is not None and not isinstance(cwd_raw, str):
            return _error("`cwd` must be a string if provided")
        cwd = os.path.expanduser(cwd_raw) if cwd_raw else None
        if cwd and not os.path.isdir(cwd):
            return _error(f"cwd does not exist: {cwd!r}")

        timeout = args.get("timeout_seconds", self._default_timeout)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            return _error("`timeout_seconds` must be a positive number")
        timeout = min(float(timeout), float(self._max_timeout))

        require_approval = args.get("require_approval", True)
        if not isinstance(require_approval, bool):
            return _error("`require_approval` must be a boolean")

        if require_approval:
            session_id = CURRENT_SESSION_ID.get()
            if session_id is None:
                return _error(
                    "terminal is unavailable outside a live session — "
                    "CURRENT_SESSION_ID context var is unset"
                )
            answer = await self._broker.ask(
                session_id,
                _approval_prompt(command, cwd),
                kind="confirm",
                timeout_seconds=self._approval_timeout,
                yolo=self._yolo(),
            )
            timed_out = answer == TIMEOUT_SENTINEL
            if timed_out or answer != "yes":
                return TerminalResult(
                    ok=False,
                    exit_code=None,
                    stdout="",
                    stderr="",
                    stdout_truncated=False,
                    stderr_truncated=False,
                    duration_ms=0,
                    timed_out=timed_out,
                    denied=True,
                    error=(
                        "user did not approve the command"
                        + (" (timeout)" if timed_out else "")
                    ),
                )

        return await _run_command(
            command,
            cwd=cwd,
            timeout=timeout,
            stream_char_limit=self._stream_char_limit,
            on_output=self._on_output,
            proc_register=self._proc_register,
            proc_unregister=self._proc_unregister,
        )


def _approval_prompt(command: str, cwd: str | None) -> str:
    dir_str = cwd or os.getcwd()
    return (
        "Agent wants to run this shell command:\n\n"
        f"    {command}\n\n"
        f"Working directory: {dir_str}"
    )


async def _run_command(
    command: str,
    *,
    cwd: str | None,
    timeout: float,
    stream_char_limit: int,
    on_output: Callable[[str, str], Awaitable[None]] | None = None,
    proc_register: Callable[[asyncio.subprocess.Process], None] | None = None,
    proc_unregister: Callable[[], None] | None = None,
) -> TerminalResult:
    start = asyncio.get_running_loop().time()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return _error(f"failed to launch subprocess: {exc}")

    if proc_register is not None:
        proc_register(proc)

    timed_out = False
    cancelled = False
    try:
        if on_output is not None:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                _stream_output(proc, on_output),
                timeout=timeout,
            )
        else:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout,
            )
    except TimeoutError:
        timed_out = True
        kill_proc_group(proc)
        stdout_bytes, stderr_bytes = await _drain(proc)
    except asyncio.CancelledError:
        cancelled = True
        kill_proc_group(proc)
        stdout_bytes, stderr_bytes = await _drain(proc)
    finally:
        if proc_unregister is not None:
            proc_unregister()

    duration_ms = int((asyncio.get_running_loop().time() - start) * 1000)
    stdout, stdout_truncated = _truncate_stream(stdout_bytes, stream_char_limit)
    stderr, stderr_truncated = _truncate_stream(stderr_bytes, stream_char_limit)

    result = TerminalResult(
        ok=not timed_out and not cancelled and (proc.returncode == 0),
        exit_code=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        duration_ms=duration_ms,
        timed_out=timed_out or cancelled,
        denied=False,
        error=(
            "command killed by user" if cancelled
            else "command timed out" if timed_out
            else None
        ),
    )

    if cancelled:
        raise asyncio.CancelledError(result.to_text())

    return result


async def _stream_output(
    proc: asyncio.subprocess.Process,
    on_output: Callable[[str, str], Awaitable[None]],
) -> tuple[bytes, bytes]:
    """Read stdout/stderr line-by-line, forwarding each chunk via *on_output*.

    Returns the full accumulated ``(stdout_bytes, stderr_bytes)`` when the
    process exits.
    """
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    async def _read_stdout() -> None:
        if proc.stdout is None:
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            stdout_chunks.append(line)
            try:
                await on_output(line.decode("utf-8", errors="replace"), "")
            except Exception:
                pass

    async def _read_stderr() -> None:
        if proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            stderr_chunks.append(line)
            try:
                await on_output("", line.decode("utf-8", errors="replace"))
            except Exception:
                pass

    await asyncio.gather(_read_stdout(), _read_stderr())
    await proc.wait()
    return b"".join(stdout_chunks), b"".join(stderr_chunks)


async def _drain(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """Best-effort drain of remaining process output after kill."""
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=5)
    except (TimeoutError, asyncio.CancelledError):
        return b"", b""


def _truncate_stream(raw: bytes | None, limit: int) -> tuple[str, bool]:
    if not raw:
        return "", False
    text = raw.decode("utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit], True
    return text, False


def _error(message: str) -> TerminalResult:
    return TerminalResult(
        ok=False,
        exit_code=None,
        stdout="",
        stderr="",
        stdout_truncated=False,
        stderr_truncated=False,
        duration_ms=0,
        timed_out=False,
        denied=False,
        error=message,
    )


__all__ = ["TerminalTool", "TerminalResult", "TERMINAL_TOOL_SPEC", "kill_proc_group"]
