"""Tests for AgentConfig enhancement hooks — the extension points that
let Nexus (and other embedders) fully drive the loop without forking."""

from __future__ import annotations

from collections.abc import AsyncIterator

from loom.errors import LLMTransportError
from loom.llm.base import LLMProvider
from loom.loop import Agent, AgentConfig
from loom.tools.base import ToolHandler, ToolResult
from loom.tools.registry import ToolRegistry
from loom.types import (
    ChatMessage,
    ChatResponse,
    ContentDeltaEvent,
    DoneEvent,
    ErrorEvent,
    Role,
    StopEvent,
    StopReason,
    StreamEvent,
    ToolCallDeltaEvent,
    ToolSpec,
    Usage,
    UsageEvent,
)


def _final_turn(text: str) -> list[StreamEvent]:
    return [
        ContentDeltaEvent(delta=text),
        UsageEvent(usage=Usage(input_tokens=3, output_tokens=1)),
        StopEvent(stop_reason=StopReason.STOP),
    ]


class _ScriptedProvider(LLMProvider):
    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns
        self._idx = 0
        self.last_model: str | None = None

    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        self.last_model = model
        return ChatResponse(
            message=ChatMessage(role=Role.ASSISTANT, content="non-stream"),
            usage=Usage(input_tokens=1, output_tokens=1),
            stop_reason=StopReason.STOP,
            model=model or "",
        )

    async def chat_stream(self, messages, *, tools=None, model=None) -> AsyncIterator[StreamEvent]:
        self.last_model = model
        events = self._turns[self._idx]
        self._idx += 1
        for e in events:
            yield e


class _RaisingStreamProvider(LLMProvider):
    """Streams N content deltas then raises mid-stream."""

    def __init__(self, deltas_before_fail: int = 0, status_code: int = 503) -> None:
        self._n = deltas_before_fail
        self._status = status_code

    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        raise NotImplementedError

    async def chat_stream(self, messages, *, tools=None, model=None) -> AsyncIterator[StreamEvent]:
        for i in range(self._n):
            yield ContentDeltaEvent(delta=f"t{i}")
        raise LLMTransportError("upstream boom", status_code=self._status)


class _CreateFailingProvider(LLMProvider):
    """chat_stream itself raises before yielding anything. Since
    with_retry retries N times, use a non-retryable status (401)."""

    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        raise NotImplementedError

    def chat_stream(self, messages, *, tools=None, model=None):
        raise LLMTransportError("nope", status_code=401)


# ── #3 ErrorEvent fields ────────────────────────────────────────────────


def test_error_event_carries_status_and_retryable():
    e = ErrorEvent(message="rate limited", status_code=429, retryable=True)
    assert e.status_code == 429
    assert e.retryable is True

    e2 = ErrorEvent(message="plain")
    assert e2.status_code is None
    assert e2.retryable is False


# ── #10 DoneEvent ───────────────────────────────────────────────────────


def test_done_event_context_bag():
    d = DoneEvent(context={"sid": "s1", "tokens": 42})
    assert d.type == "done"
    assert d.context["sid"] == "s1"


async def test_stream_ends_with_done_event():
    provider = _ScriptedProvider([_final_turn("hi")])
    agent = Agent(provider=provider, tool_registry=ToolRegistry(), config=AgentConfig())
    events = [e async for e in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")])]
    assert events[-1].type == "done"
    assert "iterations" in events[-1].context


# ── #6 per-call model_id ────────────────────────────────────────────────


async def test_run_turn_stream_per_call_model_id_overrides_config():
    provider = _ScriptedProvider([_final_turn("hi")])
    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(model="cfg-model"),
    )
    async for _ in agent.run_turn_stream(
        [ChatMessage(role=Role.USER, content="go")],
        model_id="per-call-model",
    ):
        pass
    assert provider.last_model == "per-call-model"


async def test_run_turn_per_call_model_id_overrides_config():
    provider = _ScriptedProvider([])
    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(model="cfg-model", max_iterations=1),
    )
    await agent.run_turn([ChatMessage(role=Role.USER, content="go")], model_id="explicit")
    assert provider.last_model == "explicit"


# ── #5 choose_model hook ────────────────────────────────────────────────


async def test_choose_model_hook_picks_per_turn():
    provider = _ScriptedProvider([_final_turn("hi")])
    called_with: list = []

    def picker(msgs: list[ChatMessage]) -> str:
        called_with.append(msgs)
        return "router-picked"

    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(choose_model=picker),
    )
    async for _ in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")]):
        pass
    assert provider.last_model == "router-picked"
    assert called_with, "picker should have been invoked"


async def test_choose_model_error_falls_back_to_config():
    provider = _ScriptedProvider([_final_turn("hi")])

    def picker(msgs):
        raise RuntimeError("router broken")

    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(model="cfg-default", choose_model=picker),
    )
    async for _ in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")]):
        pass
    assert provider.last_model == "cfg-default"


# ── #7 limit_message_builder ────────────────────────────────────────────


async def test_limit_message_builder_replaces_default():
    provider = _ScriptedProvider([])  # will hit limit since no turns
    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(
            max_iterations=0,
            limit_message_builder=lambda n: f"hit {n}!",
        ),
    )
    turn = await agent.run_turn([ChatMessage(role=Role.USER, content="go")])
    assert turn.reply == "hit 0!"


# ── #8 affirmatives/negatives config ────────────────────────────────────


def test_custom_affirmatives_extend_annotation():
    agent = Agent(
        tool_registry=ToolRegistry(),
        config=AgentConfig(affirmatives={"aye"}),
    )
    agent._pending_question = "Proceed?"
    out = agent._annotate_short_reply("aye")
    assert out is not None
    assert "affirmative" in out
    # Default "yes" is replaced (not extended) — caller must include it.
    assert agent._annotate_short_reply("yes") is None


def test_default_affirmatives_still_work():
    agent = Agent(tool_registry=ToolRegistry(), config=AgentConfig())
    agent._pending_question = "Proceed?"
    assert agent._annotate_short_reply("yes") is not None
    assert agent._annotate_short_reply("no") is not None


# ── #4 on_event trace hook ──────────────────────────────────────────────


async def test_on_event_receives_stream_start():
    events: list[tuple[str, dict]] = []
    provider = _ScriptedProvider([_final_turn("hi")])
    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(on_event=lambda kind, p: events.append((kind, p))),
    )
    async for _ in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")]):
        pass
    kinds = [k for k, _ in events]
    assert "stream_start" in kinds


async def test_on_event_handler_errors_are_swallowed():
    provider = _ScriptedProvider([_final_turn("hi")])

    def bad(kind, p):
        raise RuntimeError("trace broke")

    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(on_event=bad),
    )
    # Should still complete without raising.
    events = [e async for e in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")])]
    assert any(e.type == "done" for e in events)


# ── #9 serialize_event hook ─────────────────────────────────────────────


async def test_serialize_event_converts_to_dict():
    provider = _ScriptedProvider([_final_turn("hi")])
    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(serialize_event=lambda ev: ev.model_dump()),
    )
    out = [e async for e in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")])]
    assert all(isinstance(e, dict) for e in out)
    assert out[-1]["type"] == "done"


# ── #2 streaming-correct retry / mid-stream failure ─────────────────────


async def test_mid_stream_failure_emits_error_then_done():
    """Upstream raises after forwarding some content — caller gets an
    ErrorEvent plus a DoneEvent marking the partial turn."""
    provider = _RaisingStreamProvider(deltas_before_fail=2, status_code=503)
    agent = Agent(provider=provider, tool_registry=ToolRegistry(), config=AgentConfig())

    events = [e async for e in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")])]
    kinds = [e.type for e in events]
    assert "content_delta" in kinds
    assert "error" in kinds
    assert kinds[-1] == "done"
    err = next(e for e in events if isinstance(e, ErrorEvent))
    assert err.status_code == 503
    done = events[-1]
    assert done.context.get("partial") is True


async def test_stream_creation_failure_emits_error_then_done():
    """Non-retryable creation failure surfaces as events rather than
    propagating — the caller may already be iterating."""
    provider = _CreateFailingProvider()
    agent = Agent(provider=provider, tool_registry=ToolRegistry(), config=AgentConfig())

    events = [e async for e in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")])]
    kinds = [e.type for e in events]
    assert kinds[0] == "error"
    assert kinds[-1] == "done"
    err = events[0]
    assert err.status_code == 401


# ── #11 before_llm_call hook ────────────────────────────────────────────


class _CapturingProvider(_ScriptedProvider):
    """Records the messages list passed to each chat_stream call."""

    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        super().__init__(turns)
        self.calls: list[list[ChatMessage]] = []

    async def chat_stream(self, messages, *, tools=None, model=None) -> AsyncIterator[StreamEvent]:
        self.calls.append(list(messages))
        async for e in super().chat_stream(messages, tools=tools, model=model):
            yield e


class _NoopTool(ToolHandler):
    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="noop",
            description="does nothing",
            parameters={"type": "object", "properties": {}},
        )

    async def invoke(self, arguments: dict) -> ToolResult:
        return ToolResult(text="ok")


def _tool_turn(tc_id: str, name: str, args: str) -> list[StreamEvent]:
    return [
        ToolCallDeltaEvent(index=0, id=tc_id, name=name, arguments_delta=args),
        UsageEvent(usage=Usage(input_tokens=5, output_tokens=2)),
        StopEvent(stop_reason=StopReason.TOOL_USE),
    ]


async def test_before_llm_call_hook_rewrites_messages():
    """Sync hook can rewrite the message list; provider receives the modified list."""
    provider = _CapturingProvider([_final_turn("hi")])

    def hook(msgs: list[ChatMessage]) -> list[ChatMessage]:
        # Replace system message content with a marker
        return [
            ChatMessage(role=m.role, content="REWRITTEN" if m.role == Role.SYSTEM else m.content)
            for m in msgs
        ]

    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(before_llm_call=hook),
    )
    async for _ in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")]):
        pass

    assert len(provider.calls) == 1
    sys_msg = next(m for m in provider.calls[0] if m.role == Role.SYSTEM)
    assert sys_msg.content == "REWRITTEN"


async def test_before_llm_call_hook_async():
    """Async hook is awaited correctly and its result is used."""
    provider = _CapturingProvider([_final_turn("hi")])

    async def async_hook(msgs: list[ChatMessage]) -> list[ChatMessage]:
        return [
            ChatMessage(
                role=m.role, content="ASYNC_REWRITTEN" if m.role == Role.SYSTEM else m.content
            )
            for m in msgs
        ]

    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(before_llm_call=async_hook),
    )
    async for _ in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")]):
        pass

    assert len(provider.calls) == 1
    sys_msg = next(m for m in provider.calls[0] if m.role == Role.SYSTEM)
    assert sys_msg.content == "ASYNC_REWRITTEN"


async def test_before_llm_call_hook_called_each_iteration():
    """Hook is called once per loop iteration (tool-call turn + final turn = 2 calls)."""
    tools = ToolRegistry()
    tools.register(_NoopTool())

    provider = _CapturingProvider(
        [
            _tool_turn("tc1", "noop", "{}"),
            _final_turn("done"),
        ]
    )

    call_count = 0

    def hook(msgs: list[ChatMessage]) -> list[ChatMessage]:
        nonlocal call_count
        call_count += 1
        return msgs

    agent = Agent(
        provider=provider,
        tool_registry=tools,
        config=AgentConfig(before_llm_call=hook),
    )
    async for _ in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")]):
        pass

    assert call_count == 2
    assert len(provider.calls) == 2


async def test_before_llm_call_hook_error_emits_error_done():
    """A raising hook surfaces as ErrorEvent then DoneEvent; no uncaught exception."""
    provider = _CapturingProvider([_final_turn("hi")])

    def bad_hook(msgs: list[ChatMessage]) -> list[ChatMessage]:
        raise ValueError("hook exploded")

    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(before_llm_call=bad_hook),
    )

    events = [e async for e in agent.run_turn_stream([ChatMessage(role=Role.USER, content="go")])]
    kinds = [e.type for e in events]
    assert "error" in kinds
    assert kinds[-1] == "done"
    err = next(e for e in events if e.type == "error")
    assert "hook exploded" in err.message
    # Provider should not have been called since hook failed first
    assert len(provider.calls) == 0
