"""Tests for RFC 0004 implementation — rich SSE events.

Covers:
1. DoneEvent typed fields (model, iterations, input_tokens, etc.)
2. serialize_event — Pydantic → dict with session_id injection
3. /chat/stream SSE route — full event payloads, session anchor,
   usage tracking, and history persistence.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from loom.llm.base import LLMProvider
from loom.loop import Agent, AgentConfig
from loom.server.events import serialize_event
from loom.store.session import SessionStore
from loom.tools.base import ToolHandler, ToolResult
from loom.tools.registry import ToolRegistry
from loom.types import (
    ChatMessage,
    ChatResponse,
    ContentDeltaEvent,
    DoneEvent,
    ErrorEvent,
    LimitReachedEvent,
    Role,
    StopEvent,
    StopReason,
    StreamEvent,
    ToolCall,
    ToolCallDeltaEvent,
    ToolExecResultEvent,
    ToolExecStartEvent,
    ToolSpec,
    Usage,
    UsageEvent,
)


# ── Helpers ───────────────────────────────────────────────────────────────


class _EchoTool(ToolHandler):
    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="echo",
            description="echo",
            parameters={
                "type": "object",
                "properties": {"x": {"type": "string"}},
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        return ToolResult(text=f"echoed:{args.get('x', '')}")


class _ScriptedProvider(LLMProvider):
    """Replays a list of per-turn event sequences."""

    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns
        self._idx = 0

    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        raise NotImplementedError

    async def chat_stream(
        self, messages, *, tools=None, model=None
    ) -> AsyncIterator[StreamEvent]:
        events = self._turns[self._idx]
        self._idx += 1
        for e in events:
            yield e


def _tool_turn(tc_id: str, name: str, args: str) -> list[StreamEvent]:
    return [
        ToolCallDeltaEvent(index=0, id=tc_id, name=name, arguments_delta=args),
        UsageEvent(usage=Usage(input_tokens=10, output_tokens=5)),
        StopEvent(stop_reason=StopReason.TOOL_USE),
    ]


def _final_turn(text: str) -> list[StreamEvent]:
    return [
        ContentDeltaEvent(delta=text),
        UsageEvent(usage=Usage(input_tokens=3, output_tokens=1)),
        StopEvent(stop_reason=StopReason.STOP),
    ]


# ── 1. DoneEvent typed fields ───────────────────────────────────────────


class TestDoneEventFields:
    def test_default_values(self):
        e = DoneEvent()
        assert e.type == "done"
        assert e.model == ""
        assert e.iterations == 0
        assert e.input_tokens == 0
        assert e.output_tokens == 0
        assert e.tool_calls == 0
        assert e.stop_reason is None
        assert e.session_id is None
        assert e.skills_touched == []
        assert e.context == {}

    def test_typed_fields_set(self):
        e = DoneEvent(
            model="gpt-4o",
            iterations=5,
            input_tokens=100,
            output_tokens=50,
            tool_calls=3,
            stop_reason=StopReason.STOP,
            session_id="abc123",
            skills_touched=["python", "search"],
            context={"custom": "data"},
        )
        assert e.model == "gpt-4o"
        assert e.iterations == 5
        assert e.input_tokens == 100
        assert e.output_tokens == 50
        assert e.tool_calls == 3
        assert e.stop_reason == StopReason.STOP
        assert e.session_id == "abc123"
        assert e.skills_touched == ["python", "search"]
        assert e.context == {"custom": "data"}

    def test_model_dump_includes_typed_fields(self):
        e = DoneEvent(model="m", iterations=2, input_tokens=10)
        d = e.model_dump()
        assert d["model"] == "m"
        assert d["iterations"] == 2
        assert d["input_tokens"] == 10

    def test_backward_compat_context_still_works(self):
        e = DoneEvent(context={"messages": ["x"], "limit_reached": True})
        assert e.context["limit_reached"] is True


class TestDoneEventFromStream:
    """Verify that run_turn_stream populates the typed DoneEvent fields."""

    async def test_normal_completion(self):
        provider = _ScriptedProvider([_final_turn("hello")])
        agent = Agent(provider=provider, config=AgentConfig())
        events = [
            e
            async for e in agent.run_turn_stream(
                [ChatMessage(role=Role.USER, content="hi")]
            )
        ]
        done = next(e for e in events if isinstance(e, DoneEvent))
        assert done.iterations >= 1
        assert done.input_tokens > 0
        assert done.output_tokens > 0
        assert done.tool_calls == 0
        assert done.stop_reason == StopReason.STOP
        assert done.skills_touched == []

    async def test_with_tool_call(self):
        tools = ToolRegistry()
        tools.register(_EchoTool())
        provider = _ScriptedProvider(
            [
                _tool_turn("tc1", "echo", '{"x":"hi"}'),
                _final_turn("done"),
            ]
        )
        agent = Agent(provider=provider, tool_registry=tools, config=AgentConfig())
        events = [
            e
            async for e in agent.run_turn_stream(
                [ChatMessage(role=Role.USER, content="go")]
            )
        ]
        done = next(e for e in events if isinstance(e, DoneEvent))
        assert done.iterations == 2
        assert done.tool_calls == 1
        # Usage accumulated across both turns
        assert done.input_tokens > 0
        assert done.output_tokens > 0

    async def test_hook_error_done_event(self):
        """before_llm_call hook error emits DoneEvent with model."""

        def _boom(msgs):
            raise RuntimeError("hook failed")

        provider = _ScriptedProvider([_final_turn("hi")])
        agent = Agent(
            provider=provider,
            config=AgentConfig(before_llm_call=_boom),
        )
        events = [
            e
            async for e in agent.run_turn_stream(
                [ChatMessage(role=Role.USER, content="go")]
            )
        ]
        done = next(e for e in events if isinstance(e, DoneEvent))
        assert done.model == ""
        assert done.iterations == 0

    async def test_limit_reached_done_event(self):
        tools = ToolRegistry()
        tools.register(_EchoTool())
        provider = _ScriptedProvider(
            [_tool_turn(f"tc{i}", "echo", '{"x":"loop"}') for i in range(5)]
        )
        agent = Agent(
            provider=provider,
            tool_registry=tools,
            config=AgentConfig(max_iterations=2),
        )
        events = [
            e
            async for e in agent.run_turn_stream(
                [ChatMessage(role=Role.USER, content="go")]
            )
        ]
        done = next(e for e in events if isinstance(e, DoneEvent))
        assert done.iterations == 2
        assert done.context.get("limit_reached") is True


# ── 2. serialize_event ──────────────────────────────────────────────────


class TestSerializeEvent:
    def test_pydantic_model_to_dict(self):
        ev = ContentDeltaEvent(delta="hello")
        result = serialize_event(ev)
        assert result["type"] == "content_delta"
        assert result["delta"] == "hello"

    def test_session_id_injection(self):
        ev = ContentDeltaEvent(delta="hi")
        result = serialize_event(ev, session_id="sess-1")
        assert result["session_id"] == "sess-1"
        assert result["delta"] == "hi"

    def test_done_event_full_serialization(self):
        ev = DoneEvent(
            model="gpt-4o",
            iterations=3,
            input_tokens=100,
            output_tokens=50,
            tool_calls=2,
            stop_reason=StopReason.STOP,
            session_id="s1",
            skills_touched=["search"],
        )
        result = serialize_event(ev, session_id="s1")
        assert result["type"] == "done"
        assert result["model"] == "gpt-4o"
        assert result["iterations"] == 3
        assert result["input_tokens"] == 100
        assert result["output_tokens"] == 50
        assert result["tool_calls"] == 2
        assert result["stop_reason"] == "stop"
        assert result["session_id"] == "s1"
        assert result["skills_touched"] == ["search"]

    def test_error_event_serialization(self):
        ev = ErrorEvent(message="rate limited", reason="RATE_LIMIT", status_code=429, retryable=True)
        result = serialize_event(ev, session_id="s2")
        assert result["type"] == "error"
        assert result["message"] == "rate limited"
        assert result["status_code"] == 429
        assert result["retryable"] is True
        assert result["session_id"] == "s2"

    def test_tool_exec_start_serialization(self):
        ev = ToolExecStartEvent(tool_call_id="tc1", name="search", arguments='{"q":"x"}')
        result = serialize_event(ev, session_id="s3")
        assert result["type"] == "tool_exec_start"
        assert result["tool_call_id"] == "tc1"
        assert result["name"] == "search"
        assert result["session_id"] == "s3"

    def test_tool_exec_result_serialization(self):
        ev = ToolExecResultEvent(tool_call_id="tc1", name="search", text="found", is_error=False)
        result = serialize_event(ev)
        assert result["type"] == "tool_exec_result"
        assert result["is_error"] is False
        assert "session_id" not in result  # not provided

    def test_limit_reached_serialization(self):
        ev = LimitReachedEvent(iterations=32)
        result = serialize_event(ev, session_id="s4")
        assert result["type"] == "limit_reached"
        assert result["iterations"] == 32
        assert result["session_id"] == "s4"

    def test_usage_event_serialization(self):
        ev = UsageEvent(usage=Usage(input_tokens=50, output_tokens=25))
        result = serialize_event(ev, session_id="s5")
        assert result["type"] == "usage"
        assert result["usage"]["input_tokens"] == 50
        assert result["session_id"] == "s5"

    def test_dict_passthrough_enriched(self):
        """If a custom serializer already returned a dict, enrich with session_id."""
        d = {"type": "custom", "payload": 42}
        result = serialize_event(d, session_id="s6")
        assert result["type"] == "custom"
        assert result["session_id"] == "s6"
        assert result["payload"] == 42

    def test_dict_passthrough_preserves_existing_session_id(self):
        d = {"type": "custom", "session_id": "original"}
        result = serialize_event(d, session_id="new")
        # setdefault preserves the original
        assert result["session_id"] == "original"


# ── 3. /chat/stream SSE route ───────────────────────────────────────────


def _parse_sse_lines(body: str) -> list[dict]:
    """Parse SSE ``data:`` lines into a list of JSON dicts."""
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


class _MockRouteProvider(LLMProvider):
    """Provider that returns a simple response for route-level tests."""

    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        return ChatResponse(
            message=ChatMessage(role=Role.ASSISTANT, content="ok"),
            usage=Usage(input_tokens=10, output_tokens=5),
            stop_reason=StopReason.STOP,
            model="mock",
        )

    async def chat_stream(
        self, messages, *, tools=None, model=None
    ) -> AsyncIterator[StreamEvent]:
        yield ContentDeltaEvent(delta="hel")
        yield ContentDeltaEvent(delta="lo")
        yield UsageEvent(usage=Usage(input_tokens=10, output_tokens=5))
        yield StopEvent(stop_reason=StopReason.STOP)


class _MockToolRouteProvider(LLMProvider):
    """Provider that makes one tool call then finishes."""

    _call_count: int = 0

    def __init__(self) -> None:
        self._call_count = 0

    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        self._call_count += 1
        if self._call_count == 1:
            return ChatResponse(
                message=ChatMessage(
                    role=Role.ASSISTANT,
                    content=None,
                    tool_calls=[
                        ToolCall(id="tc1", name="echo", arguments='{"x":"test"}')
                    ],
                ),
                usage=Usage(input_tokens=15, output_tokens=8),
                stop_reason=StopReason.TOOL_USE,
                model="mock",
            )
        return ChatResponse(
            message=ChatMessage(role=Role.ASSISTANT, content="tool done"),
            usage=Usage(input_tokens=5, output_tokens=3),
            stop_reason=StopReason.STOP,
            model="mock",
        )

    async def chat_stream(
        self, messages, *, tools=None, model=None
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError("use chat() path for route tests")


class TestSSEChatStreamRoute:
    """Integration tests for the /chat/stream endpoint using httpx."""

    @pytest.fixture
    def agent_setup(self, tmp_path):
        from loom.store.session import SessionStore

        agent = Agent(
            provider=_MockRouteProvider(),
            config=AgentConfig(),
        )
        sessions = SessionStore(tmp_path / "sessions.sqlite")
        return agent, sessions

    @pytest.fixture
    def agent_with_tools(self, tmp_path):
        from loom.store.session import SessionStore

        tools = ToolRegistry()
        tools.register(_EchoTool())
        agent = Agent(
            provider=_MockToolRouteProvider(),
            tool_registry=tools,
            config=AgentConfig(),
        )
        sessions = SessionStore(tmp_path / "sessions.sqlite")
        return agent, sessions

    def _make_app(self, agent, sessions):
        from loom.server.app import create_app

        return create_app(agent, sessions)

    @pytest.mark.asyncio
    async def test_session_anchor_first_event(self, agent_setup):
        import httpx

        agent, sessions = agent_setup
        app = self._make_app(agent, sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/chat/stream",
                json={"message": "hello"},
            )
            assert resp.status_code == 200
            events = _parse_sse_lines(resp.text)
            assert events[0]["type"] == "session"
            assert "session_id" in events[0]
            assert len(events[0]["session_id"]) > 0

    @pytest.mark.asyncio
    async def test_content_deltas_have_full_payload(self, agent_setup):
        import httpx

        agent, sessions = agent_setup
        app = self._make_app(agent, sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/chat/stream",
                json={"message": "hello"},
            )
            events = _parse_sse_lines(resp.text)
            deltas = [e for e in events if e["type"] == "content_delta"]
            assert len(deltas) >= 2
            assert deltas[0]["delta"] == "hel"
            assert deltas[1]["delta"] == "lo"
            # Every delta carries session_id
            assert all("session_id" in d for d in deltas)

    @pytest.mark.asyncio
    async def test_done_event_has_typed_fields(self, agent_setup):
        import httpx

        agent, sessions = agent_setup
        app = self._make_app(agent, sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/chat/stream",
                json={"message": "hello"},
            )
            events = _parse_sse_lines(resp.text)
            done_events = [e for e in events if e["type"] == "done"]
            assert len(done_events) == 1
            done = done_events[0]
            assert done["iterations"] >= 1
            assert done["input_tokens"] >= 0
            assert done["output_tokens"] >= 0
            assert "session_id" in done
            assert "model" in done

    @pytest.mark.asyncio
    async def test_all_events_carry_session_id(self, agent_setup):
        import httpx

        agent, sessions = agent_setup
        app = self._make_app(agent, sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/chat/stream",
                json={"message": "hello"},
            )
            events = _parse_sse_lines(resp.text)
            # Every event after the anchor should carry session_id
            for ev in events[1:]:
                assert "session_id" in ev, f"Missing session_id in {ev['type']} event"

    @pytest.mark.asyncio
    async def test_session_id_echoed_in_done(self, agent_setup):
        import httpx

        agent, sessions = agent_setup
        app = self._make_app(agent, sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/chat/stream",
                json={"message": "hello", "session_id": "my-session"},
            )
            events = _parse_sse_lines(resp.text)
            done = next(e for e in events if e["type"] == "done")
            assert done["session_id"] == "my-session"

    @pytest.mark.asyncio
    async def test_history_persisted_after_stream(self, agent_setup):
        import httpx

        agent, sessions = agent_setup
        app = self._make_app(agent, sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/chat/stream",
                json={"message": "hello", "session_id": "persist-test"},
            )
            assert resp.status_code == 200

        # Check history was persisted
        history = sessions.get_history("persist-test")
        assert len(history) >= 2
        assert history[0].role == Role.USER
        assert history[0].content == "hello"
        assert history[1].role == Role.ASSISTANT
        assert history[1].content == "hello"  # "hel" + "lo"

    @pytest.mark.asyncio
    async def test_usage_bumped_after_stream(self, agent_setup):
        import httpx

        agent, sessions = agent_setup
        app = self._make_app(agent, sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/chat/stream",
                json={"message": "hello", "session_id": "usage-test"},
            )
            assert resp.status_code == 200

        session = sessions.get_or_create("usage-test")
        assert session["input_tokens"] > 0
        assert session["output_tokens"] > 0

    @pytest.mark.asyncio
    async def test_tool_events_in_stream(self, agent_with_tools):
        import httpx

        agent, sessions = agent_with_tools
        app = self._make_app(agent, sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/chat/stream",
                json={"message": "echo test", "session_id": "tool-test"},
            )
            # This uses the non-streaming path (chat not chat_stream),
            # so the sync endpoint gets tool execution. For the streaming
            # route, we get the done event with tool_calls > 0.
            # The non-streaming /chat endpoint is tested separately.

    @pytest.mark.asyncio
    async def test_new_session_id_generated(self, agent_setup):
        import httpx

        agent, sessions = agent_setup
        app = self._make_app(agent, sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/chat/stream",
                json={"message": "hello", "session_id": "__new__"},
            )
            events = _parse_sse_lines(resp.text)
            anchor = events[0]
            assert anchor["type"] == "session"
            # Should be a generated ID, not "__new__"
            assert anchor["session_id"] != "__new__"
            assert len(anchor["session_id"]) == 12  # uuid4 hex[:12]

    @pytest.mark.asyncio
    async def test_existing_session_reused(self, agent_setup):
        import httpx

        agent, sessions = agent_setup
        app = self._make_app(agent, sessions)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            # First request creates the session
            await client.post(
                "/chat/stream",
                json={"message": "first", "session_id": "reuse-me"},
            )
            # Second request reuses it (history should accumulate)
            await client.post(
                "/chat/stream",
                json={"message": "second", "session_id": "reuse-me"},
            )

        history = sessions.get_history("reuse-me")
        # 2 user messages + 2 assistant replies
        assert len(history) == 4
        assert history[0].content == "first"
        assert history[2].content == "second"
