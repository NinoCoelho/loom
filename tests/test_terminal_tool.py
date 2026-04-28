"""Tests for the broker-wired terminal tool."""

from __future__ import annotations

import asyncio
import json

from loom.hitl import CURRENT_SESSION_ID, HitlBroker
from loom.tools.terminal import TERMINAL_TOOL_SPEC, TerminalTool


def _decode(text: str) -> dict:
    return json.loads(text)


class TestTerminalSpec:
    def test_spec_shape(self) -> None:
        assert TERMINAL_TOOL_SPEC.name == "terminal"
        props = TERMINAL_TOOL_SPEC.parameters["properties"]
        assert {"command", "cwd", "timeout_seconds"} <= set(props)
        assert TERMINAL_TOOL_SPEC.parameters["required"] == ["command"]

    def test_tool_property_returns_module_spec(self) -> None:
        tool = TerminalTool(HitlBroker())
        assert tool.tool is TERMINAL_TOOL_SPEC


class TestTerminalValidation:
    async def test_missing_command(self) -> None:
        tool = TerminalTool(HitlBroker())
        result = await tool.invoke({})
        body = _decode(result.text)
        assert body["ok"] is False
        assert "command" in body["error"]

    async def test_blank_command(self) -> None:
        tool = TerminalTool(HitlBroker())
        result = await tool.invoke({"command": "   "})
        body = _decode(result.text)
        assert body["ok"] is False

    async def test_bad_cwd(self) -> None:
        tool = TerminalTool(HitlBroker())
        result = await tool.invoke(
            {"command": "echo hi", "cwd": "/no/such/dir/xyz"}
        )
        body = _decode(result.text)
        assert body["ok"] is False
        assert "cwd does not exist" in body["error"]

    async def test_negative_timeout(self) -> None:
        tool = TerminalTool(HitlBroker())
        result = await tool.invoke(
            {"command": "echo hi", "timeout_seconds": -1}
        )
        body = _decode(result.text)
        assert body["ok"] is False


class TestTerminalApproval:
    async def test_no_session_returns_error(self) -> None:
        tool = TerminalTool(HitlBroker())
        # No CURRENT_SESSION_ID set in this context
        result = await tool.invoke({"command": "echo hi"})
        body = _decode(result.text)
        assert body["ok"] is False
        assert "CURRENT_SESSION_ID" in body["error"]

    async def test_user_approves_and_runs(self) -> None:
        broker = HitlBroker()
        tool = TerminalTool(broker)
        sub = broker.subscribe("s1")

        async def approve() -> None:
            ev = await sub.get()
            assert ev.kind == "user_request"
            broker.resolve("s1", ev.data["request_id"], "yes")

        token = CURRENT_SESSION_ID.set("s1")
        try:
            answerer = asyncio.create_task(approve())
            result = await tool.invoke(
                {"command": "echo hello-loom", "timeout_seconds": 5}
            )
            await answerer
        finally:
            CURRENT_SESSION_ID.reset(token)

        body = _decode(result.text)
        assert body["ok"] is True
        assert body["exit_code"] == 0
        assert "hello-loom" in body["stdout"]
        assert body["denied"] is False
        assert body["timed_out"] is False

    async def test_user_denies(self) -> None:
        broker = HitlBroker()
        tool = TerminalTool(broker)
        sub = broker.subscribe("s1")

        async def deny() -> None:
            ev = await sub.get()
            broker.resolve("s1", ev.data["request_id"], "no")

        token = CURRENT_SESSION_ID.set("s1")
        try:
            answerer = asyncio.create_task(deny())
            result = await tool.invoke({"command": "echo nope"})
            await answerer
        finally:
            CURRENT_SESSION_ID.reset(token)

        body = _decode(result.text)
        assert body["ok"] is False
        assert body["denied"] is True
        assert body["timed_out"] is False

    async def test_yolo_skips_prompt(self) -> None:
        broker = HitlBroker()
        tool = TerminalTool(broker, yolo_getter=lambda: True)

        token = CURRENT_SESSION_ID.set("s1")
        try:
            result = await tool.invoke(
                {"command": "echo yolo", "timeout_seconds": 5}
            )
        finally:
            CURRENT_SESSION_ID.reset(token)

        body = _decode(result.text)
        assert body["ok"] is True
        assert "yolo" in body["stdout"]

    async def test_require_approval_false_skips_prompt(self) -> None:
        broker = HitlBroker()
        tool = TerminalTool(broker)
        # Note: require_approval=False, so no session id needed
        result = await tool.invoke(
            {"command": "echo skip-prompt", "require_approval": False}
        )
        body = _decode(result.text)
        assert body["ok"] is True
        assert "skip-prompt" in body["stdout"]


class TestTerminalExecution:
    async def test_failing_command_returns_exit_code(self) -> None:
        tool = TerminalTool(HitlBroker())
        result = await tool.invoke(
            {"command": "exit 7", "require_approval": False}
        )
        body = _decode(result.text)
        assert body["ok"] is False
        assert body["exit_code"] == 7
        assert body["denied"] is False

    async def test_timeout_kills_process(self) -> None:
        tool = TerminalTool(HitlBroker())
        result = await tool.invoke(
            {
                "command": "sleep 5",
                "require_approval": False,
                "timeout_seconds": 1,
            }
        )
        body = _decode(result.text)
        assert body["timed_out"] is True
        assert body["ok"] is False
        assert body["error"] == "command timed out"

    async def test_stdout_truncation(self) -> None:
        tool = TerminalTool(HitlBroker(), stream_char_limit=100)
        # Print 1000 chars of 'A'
        result = await tool.invoke(
            {
                "command": "python3 -c \"print('A' * 1000)\"",
                "require_approval": False,
            }
        )
        body = _decode(result.text)
        assert body["ok"] is True
        assert body["stdout_truncated"] is True
        assert len(body["stdout"]) == 100
