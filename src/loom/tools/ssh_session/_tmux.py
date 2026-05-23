from __future__ import annotations

import asyncio
import secrets
import shlex
import time
from dataclasses import dataclass
from typing import Any

from loom.tools.base import ToolResult
from loom.tools.utils import truncate_text

from ._helpers import _err, _valid_session_id

_TMUX_PREFIX = "loom-"
_DONE_MARKER_FMT = "__LOOM_DONE_{n}_{rc}__"
_DONE_PATTERN = "__LOOM_DONE_"


@dataclass
class _SessionState:
    scope: str
    session_id: str
    command_counter: int = 0
    tmpdir: str = ""


class TmuxManager:
    def __init__(
        self,
        pool: Any,
        tool: Any,
        max_output_bytes: int = 10240,
        poll_interval: float = 0.2,
        command_timeout: float = 60.0,
    ) -> None:
        self._pool = pool
        self._tool = tool
        self._max_output_bytes = max_output_bytes
        self._poll_interval = poll_interval
        self._command_timeout = command_timeout

    async def _run_remote(
        self, conn, cmd: str, timeout: float | None = None
    ) -> tuple[int, str, str]:
        return await self._tool._run_remote(conn, cmd, timeout)

    async def dispatch(
        self, action: str, scope: str, conn, args: dict
    ) -> ToolResult:
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

    async def _action_open(self, scope: str, conn, args: dict) -> ToolResult:
        session_id = args.get("session_id") or secrets.token_hex(4)
        if not _valid_session_id(session_id):
            return _err("session_id must match [A-Za-z0-9_-]+", "unknown")

        rc, _, _ = await self._run_remote(conn, "command -v tmux >/dev/null 2>&1")
        if rc != 0:
            return _err("tmux is not installed on the remote host", "transport")

        tmux_name = _TMUX_PREFIX + session_id
        rc, _, stderr = await self._run_remote(
            conn,
            f"tmux has-session -t {shlex.quote(tmux_name)} 2>/dev/null "
            f"|| tmux new-session -d -s {shlex.quote(tmux_name)}",
        )
        if rc != 0:
            return _err(f"tmux session create failed: {stderr.strip()}", "transport")

        tmpdir = f"/tmp/loom-session-{session_id}"
        rc, _, stderr = await self._run_remote(
            conn, f"mkdir -p {shlex.quote(tmpdir)}"
        )
        if rc != 0:
            return _err(f"mkdir tmpdir failed: {stderr.strip()}", "transport")

        state = self._pool._scopes[scope]
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

        state = self._pool._scopes[scope].sessions.get(session_id)
        if state is None:
            tmux_name = _TMUX_PREFIX + session_id
            rc, _, _ = await self._run_remote(
                conn,
                f"tmux has-session -t {shlex.quote(tmux_name)} 2>/dev/null",
            )
            if rc != 0:
                return _err(
                    f"session {session_id!r} not open — call action=open first",
                    "unknown",
                )
            tmpdir = f"/tmp/loom-session-{session_id}"
            await self._run_remote(conn, f"mkdir -p {shlex.quote(tmpdir)}")
            state = _SessionState(
                scope=scope, session_id=session_id, tmpdir=tmpdir
            )
            self._pool._scopes[scope].sessions[session_id] = state

        per_call_timeout = args.get("timeout")
        command_timeout = self._command_timeout
        if per_call_timeout is not None:
            command_timeout = min(float(per_call_timeout), self._command_timeout)

        state.command_counter += 1
        n = state.command_counter
        out_file = f"{state.tmpdir}/cmd-{n}.out"
        done_marker = _DONE_MARKER_FMT.format(n=n, rc="$?")
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

        expected_prefix = f"__LOOM_DONE_{n}_"
        exit_code: int | None = None
        deadline = t_start + command_timeout
        while True:
            if time.monotonic() > deadline:
                return ToolResult(
                    text=(
                        f"SSH error: command timed out after {command_timeout}s "
                        f"(session={session_id}, seq={n}). The command may still be running; "
                        f"use action=read to inspect pane."
                    ),
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
                idx = pane.rfind(expected_prefix)
                tail = pane[idx + len(expected_prefix) :]
                end = tail.find("__")
                if end > 0:
                    try:
                        exit_code = int(tail[:end])
                    except ValueError:
                        exit_code = None
                break
            await asyncio.sleep(self._poll_interval)

        rc, stdout_raw, read_err = await self._run_remote(
            conn, f"cat {shlex.quote(out_file)}"
        )
        if rc != 0:
            stdout_raw = ""

        stdout_text, stdout_trunc = truncate_text(
            stdout_raw, self._max_output_bytes
        )

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
            conn,
            f"tmux has-session -t {shlex.quote(tmux_name)} 2>/dev/null",
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
            conn,
            f"tmux kill-session -t {shlex.quote(tmux_name)} 2>/dev/null; :",
        )
        await self._run_remote(conn, f"rm -rf {shlex.quote(tmpdir)}")
        self._pool._scopes[scope].sessions.pop(session_id, None)

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
            line[len(_TMUX_PREFIX) :]
            for line in stdout.splitlines()
            if line.startswith(_TMUX_PREFIX)
        ]
        return ToolResult(
            text="\n".join(names) if names else "(no loom sessions)",
            metadata={"sessions": names, "action": "list"},
        )
