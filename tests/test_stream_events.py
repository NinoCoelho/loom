"""Tests for richer StreamEvent types and run_turn_stream wiring."""

from __future__ import annotations

from collections.abc import AsyncIterator

from loom.llm.base import LLMProvider
from loom.loop import Agent, AgentConfig
from loom.tools.base import ToolHandler, ToolResult
from loom.tools.registry import ToolRegistry
from loom.types import (
    ChatMessage,
    ChatResponse,
    ContentDeltaEvent,
    ErrorEvent,
    LimitReachedEvent,
    Role,
    StopEvent,
    StopReason,
    StreamEvent,
    ToolCallDeltaEvent,
    ToolExecResultEvent,
    ToolExecStartEvent,
    ToolSpec,
    Usage,
    UsageEvent,
)

# ── Event type shape ────────────────────────────────────────────────────


class TestEventTypes:
    def test_tool_exec_start(self):
        e = ToolExecStartEvent(tool_call_id="t1", name="echo", arguments='{"x":1}')
        assert e.type == "tool_exec_start"
        assert e.tool_call_id == "t1"

    def test_tool_exec_result_default_not_error(self):
        e = ToolExecResultEvent(tool_call_id="t1", name="echo", text="ok")
        assert e.type == "tool_exec_result"
        assert e.is_error is False

    def test_tool_exec_result_is_error(self):
        e = ToolExecResultEvent(
            tool_call_id="t1", name="boom", text="Tool error: x", is_error=True
        )
        assert e.is_error is True

    def test_limit_reached(self):
        e = LimitReachedEvent(iterations=32)
        assert e.type == "limit_reached"
        assert e.iterations == 32

    def test_error_event_optional_reason(self):
        e = ErrorEvent(message="boom")
        assert e.type == "error"
        assert e.reason is None
        e2 = ErrorEvent(message="auth bad", reason="AUTH_PERMANENT")
        assert e2.reason == "AUTH_PERMANENT"


# ── run_turn_stream wiring ──────────────────────────────────────────────


class _EchoTool(ToolHandler):
    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="echo",
            description="echo args",
            parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        )

    async def invoke(self, arguments: dict) -> ToolResult:
        return ToolResult(text=f"echoed:{arguments.get('x', '')}")


class _BoomTool(ToolHandler):
    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="boom",
            description="always raises",
            parameters={"type": "object", "properties": {}},
        )

    async def invoke(self, arguments: dict) -> ToolResult:
        raise RuntimeError("kaboom")


class _ScriptedProvider(LLMProvider):
    """Replays a list of per-turn event sequences."""

    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns
        self._idx = 0

    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        raise NotImplementedError

    async def chat_stream(self, messages, *, tools=None, model=None) -> AsyncIterator[StreamEvent]:
        events = self._turns[self._idx]
        self._idx += 1
        for e in events:
            yield e


def _tool_turn(tc_id: str, name: str, args: str) -> list[StreamEvent]:
    return [
        ToolCallDeltaEvent(index=0, id=tc_id, name=name, arguments_delta=args),
        UsageEvent(usage=Usage(input_tokens=5, output_tokens=2)),
        StopEvent(stop_reason=StopReason.TOOL_USE),
    ]


def _final_turn(text: str) -> list[StreamEvent]:
    return [
        ContentDeltaEvent(delta=text),
        UsageEvent(usage=Usage(input_tokens=3, output_tokens=1)),
        StopEvent(stop_reason=StopReason.STOP),
    ]


class TestStreamWiring:
    async def test_tool_exec_events_emitted(self):
        tools = ToolRegistry()
        tools.register(_EchoTool())
        provider = _ScriptedProvider(
            [
                _tool_turn("tc1", "echo", '{"x":"hi"}'),
                _final_turn("done"),
            ]
        )
        agent = Agent(provider=provider, tool_registry=tools, config=AgentConfig())

        events: list[StreamEvent] = []
        async for ev in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")]):
            events.append(ev)

        kinds = [e.type for e in events]
        assert "tool_exec_start" in kinds
        assert "tool_exec_result" in kinds

        start = next(e for e in events if isinstance(e, ToolExecStartEvent))
        result = next(e for e in events if isinstance(e, ToolExecResultEvent))
        assert start.name == "echo"
        assert start.tool_call_id == "tc1"
        assert result.text == "echoed:hi"
        assert result.is_error is False

        # Order: start precedes result for the same call id.
        assert kinds.index("tool_exec_start") < kinds.index("tool_exec_result")

    async def test_tool_exception_marks_is_error(self):
        tools = ToolRegistry()
        tools.register(_BoomTool())
        provider = _ScriptedProvider(
            [
                _tool_turn("tc1", "boom", "{}"),
                _final_turn("recovered"),
            ]
        )
        agent = Agent(provider=provider, tool_registry=tools, config=AgentConfig())

        results = [
            e
            async for e in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")])
            if isinstance(e, ToolExecResultEvent)
        ]
        assert len(results) == 1
        assert results[0].is_error is True
        assert "kaboom" in results[0].text

    async def test_limit_reached_event_on_iteration_cap(self):
        tools = ToolRegistry()
        tools.register(_EchoTool())
        # Every turn returns a tool call — never terminates.
        provider = _ScriptedProvider(
            [_tool_turn(f"tc{i}", "echo", '{"x":"loop"}') for i in range(5)]
        )
        agent = Agent(provider=provider, tool_registry=tools, config=AgentConfig(max_iterations=3))

        events = [
            e async for e in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")])
        ]
        limit_events = [e for e in events if isinstance(e, LimitReachedEvent)]
        assert len(limit_events) == 1
        assert limit_events[0].iterations == 3
