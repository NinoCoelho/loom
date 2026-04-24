"""Tests for loom.tools.ssh_session.SshSessionTool.

We avoid spinning up a real tmux-on-ssh server (too heavy for unit tests)
and instead subclass the tool to inject a fake remote shell that mimics
tmux semantics in-process. This exercises the real state machine for
open/send/read/close/list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from unittest.mock import MagicMock

import pytest

from loom.tools.ssh_session import (
    SshSessionTool,
    _classify_error,
    _valid_session_id,
)


# ---------------------------------------------------------------------- helpers


def test_valid_session_id_accepts_simple() -> None:
    assert _valid_session_id("abc")
    assert _valid_session_id("a-b_c1")


def test_valid_session_id_rejects_bad() -> None:
    assert not _valid_session_id("")
    assert not _valid_session_id("with space")
    assert not _valid_session_id("semi;colon")
    assert not _valid_session_id("back`tick")


def test_classify_error_timeout() -> None:
    assert _classify_error(TimeoutError()) == "timeout"


def test_classify_error_auth_keyword() -> None:
    assert _classify_error(Exception("permission denied")) == "auth"


def test_classify_error_unknown() -> None:
    assert _classify_error(Exception("weird")) == "unknown"


# ---------------------------------------------------------------------- fake

@dataclass
class _FakeTmuxSession:
    pane: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)


class _FakeRemote:
    """Simulates enough of a remote shell with tmux to drive SshSessionTool."""

    def __init__(self, has_tmux: bool = True) -> None:
        self.has_tmux = has_tmux
        self.sessions: dict[str, _FakeTmuxSession] = {}
        self.tmpdirs: set[str] = set()
        # Map file path -> content
        self.files: dict[str, str] = {}

    async def run(self, cmd: str):
        stripped = cmd.strip()

        # command -v tmux
        if "command -v tmux" in stripped:
            rc = 0 if self.has_tmux else 1
            return _FakeRunResult(rc, "", "")

        # compound: tmux has-session ... || tmux new-session -d -s NAME  (check first)
        m = re.search(
            r"tmux has-session -t '?([^'\s]+)'? 2>/dev/null \|\| tmux new-session -d -s '?([^'\s]+)'?",
            stripped,
        )
        if m:
            name = m.group(2)
            if name not in self.sessions:
                self.sessions[name] = _FakeTmuxSession()
            return _FakeRunResult(0, "", "")

        # tmux has-session -t NAME
        m = re.match(r"tmux has-session -t '?([^'\s]+)'?", stripped)
        if m:
            name = m.group(1)
            return _FakeRunResult(0 if name in self.sessions else 1, "", "")

        # mkdir -p TMPDIR
        m = re.match(r"mkdir -p '?([^'\s]+)'?", stripped)
        if m:
            self.tmpdirs.add(m.group(1))
            return _FakeRunResult(0, "", "")

        # rm -rf TMPDIR
        m = re.match(r"rm -rf '?([^'\s]+)'?", stripped)
        if m:
            self.tmpdirs.discard(m.group(1))
            return _FakeRunResult(0, "", "")

        # tmux send-keys -t NAME 'WRAPPED' Enter
        m = re.match(
            r"tmux send-keys -t '?([^'\s]+)'? '(.*)' Enter", stripped, re.DOTALL
        )
        if m:
            name = m.group(1)
            wrapped = m.group(2).replace("'\"'\"'", "'")  # unescape shlex quoting
            sess = self.sessions.get(name)
            if sess is None:
                return _FakeRunResult(1, "", "no such session")
            # Parse wrapped form:
            # eval 'USERCMD' > 'OUTFILE' 2>&1; echo __LOOM_DONE_<n>_$?__
            wm = re.match(
                r"eval (?:'(.*)'|(\S+)) > '?([^'\s]+)'? 2>&1; echo __LOOM_DONE_(\d+)_\$\?__",
                wrapped,
                re.DOTALL,
            )
            if wm:
                user_cmd = wm.group(1) if wm.group(1) is not None else wm.group(2)
                out_file = wm.group(3)
                n = wm.group(4)
                # Simulate executing user_cmd.
                rc, stdout = self._simulate_user_cmd(user_cmd)
                self.files[out_file] = stdout
                # Append done marker to pane.
                sess.pane.append(f"__LOOM_DONE_{n}_{rc}__")
            return _FakeRunResult(0, "", "")

        # tmux capture-pane -p -S -N -t NAME
        m = re.match(
            r"tmux capture-pane -p -S -\d+ -t '?([^'\s]+)'?", stripped
        )
        if m:
            name = m.group(1)
            sess = self.sessions.get(name)
            if sess is None:
                return _FakeRunResult(1, "", "no such session")
            return _FakeRunResult(0, "\n".join(sess.pane) + "\n", "")

        # cat OUTFILE
        m = re.match(r"cat '?([^'\s]+)'?", stripped)
        if m:
            path = m.group(1)
            if path in self.files:
                return _FakeRunResult(0, self.files[path], "")
            return _FakeRunResult(1, "", "no such file")

        # tmux kill-session
        m = re.search(r"tmux kill-session -t '?([^'\s]+)'?", stripped)
        if m:
            self.sessions.pop(m.group(1), None)
            return _FakeRunResult(0, "", "")

        # tmux list-sessions
        if "tmux list-sessions" in stripped:
            names = "\n".join(self.sessions.keys())
            return _FakeRunResult(0, names + ("\n" if names else ""), "")

        return _FakeRunResult(127, "", f"unknown cmd: {stripped}")

    def _simulate_user_cmd(self, cmd: str) -> tuple[int, str]:
        if cmd.startswith("echo "):
            return 0, cmd[5:] + "\n"
        if cmd == "false":
            return 1, ""
        if cmd == "pwd":
            return 0, "/tmp\n"
        return 0, ""


@dataclass
class _FakeRunResult:
    exit_status: int
    stdout: str
    stderr: str


class _FakeSshSessionTool(SshSessionTool):
    """Override connection + remote-run to use an in-memory fake."""

    def __init__(self, fake: _FakeRemote, **kw) -> None:
        super().__init__(credential_resolver=MagicMock(), **kw)
        self._fake = fake

    async def _ensure_connection(self, scope: str):
        # Ensure we have state dict entry.
        if scope not in self._scopes:
            from loom.tools.ssh_session import _ScopeState

            self._scopes[scope] = _ScopeState(connection=object())
        return self._scopes[scope].connection

    async def _run_remote(self, conn, cmd: str, timeout=None):
        r = await self._fake.run(cmd)
        return r.exit_status, r.stdout, r.stderr


# ---------------------------------------------------------------------- tests


async def test_open_creates_session() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake)
    result = await tool.invoke({"action": "open", "host": "h", "session_id": "s1"})
    assert not result.is_error, result.text
    assert result.metadata["session_id"] == "s1"
    assert "loom-s1" in fake.sessions


async def test_open_without_tmux_errors() -> None:
    fake = _FakeRemote(has_tmux=False)
    tool = _FakeSshSessionTool(fake)
    result = await tool.invoke({"action": "open", "host": "h", "session_id": "s1"})
    assert result.is_error
    assert "tmux is not installed" in result.text


async def test_open_is_idempotent() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake)
    r1 = await tool.invoke({"action": "open", "host": "h", "session_id": "x"})
    r2 = await tool.invoke({"action": "open", "host": "h", "session_id": "x"})
    assert not r1.is_error and not r2.is_error
    assert list(fake.sessions.keys()) == ["loom-x"]


async def test_send_roundtrip_captures_stdout_and_exit() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake, poll_interval=0.001)
    await tool.invoke({"action": "open", "host": "h", "session_id": "s"})
    result = await tool.invoke(
        {"action": "send", "host": "h", "session_id": "s", "command": "echo hello"}
    )
    assert not result.is_error
    assert result.metadata["exit_code"] == 0
    assert "hello" in result.text
    assert result.metadata["seq"] == 1


async def test_send_propagates_nonzero_exit() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake, poll_interval=0.001)
    await tool.invoke({"action": "open", "host": "h", "session_id": "s"})
    result = await tool.invoke(
        {"action": "send", "host": "h", "session_id": "s", "command": "false"}
    )
    assert result.metadata["exit_code"] == 1


async def test_send_requires_open_session() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake, poll_interval=0.001)
    result = await tool.invoke(
        {"action": "send", "host": "h", "session_id": "missing", "command": "echo hi"}
    )
    assert result.is_error
    assert "not open" in result.text


async def test_send_seq_increments_within_session() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake, poll_interval=0.001)
    await tool.invoke({"action": "open", "host": "h", "session_id": "s"})
    r1 = await tool.invoke(
        {"action": "send", "host": "h", "session_id": "s", "command": "echo a"}
    )
    r2 = await tool.invoke(
        {"action": "send", "host": "h", "session_id": "s", "command": "echo b"}
    )
    assert r1.metadata["seq"] == 1
    assert r2.metadata["seq"] == 2


async def test_read_returns_pane() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake, poll_interval=0.001)
    await tool.invoke({"action": "open", "host": "h", "session_id": "s"})
    await tool.invoke(
        {"action": "send", "host": "h", "session_id": "s", "command": "echo visible"}
    )
    result = await tool.invoke({"action": "read", "host": "h", "session_id": "s"})
    assert not result.is_error
    assert "__LOOM_DONE_1_0__" in result.text


async def test_close_kills_session() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake, poll_interval=0.001)
    await tool.invoke({"action": "open", "host": "h", "session_id": "s"})
    assert "loom-s" in fake.sessions
    result = await tool.invoke({"action": "close", "host": "h", "session_id": "s"})
    assert not result.is_error
    assert "loom-s" not in fake.sessions


async def test_list_reports_loom_sessions() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake, poll_interval=0.001)
    await tool.invoke({"action": "open", "host": "h", "session_id": "a"})
    await tool.invoke({"action": "open", "host": "h", "session_id": "b"})
    result = await tool.invoke({"action": "list", "host": "h"})
    assert set(result.metadata["sessions"]) == {"a", "b"}


async def test_invalid_session_id_rejected() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake)
    result = await tool.invoke(
        {"action": "open", "host": "h", "session_id": "bad id"}
    )
    assert result.is_error


async def test_unknown_action_errors() -> None:
    fake = _FakeRemote()
    tool = _FakeSshSessionTool(fake)
    result = await tool.invoke({"action": "bogus", "host": "h"})
    assert result.is_error


def test_tool_spec_shape() -> None:
    tool = SshSessionTool(credential_resolver=MagicMock())
    spec = tool.tool
    assert spec.name == "ssh_session"
    assert "action" in spec.parameters["properties"]
    assert spec.parameters["required"] == ["action", "host"]


