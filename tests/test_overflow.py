"""Pre-LLM-call context-window overflow detection.

The agent loop must refuse a call when the prompt would exceed the
configured context window, so providers that silently return 200 + empty
content (z.ai GLM, some Qwen endpoints) don't burn iterations on nothing.
The refusal surfaces as a structured ``OverflowEvent`` followed by a
``DoneEvent`` with ``stop_reason=STOP`` and ``context.context_overflow=True``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from loom.llm.base import LLMProvider
from loom.loop import Agent, AgentConfig
from loom.overflow import check_overflow, estimate_input_tokens
from loom.types import (
    ChatMessage,
    ChatResponse,
    ContentDeltaEvent,
    OverflowEvent,
    Role,
    StopEvent,
    StopReason,
    StreamEvent,
    Usage,
    UsageEvent,
)


# ── unit: estimator ────────────────────────────────────────────────────────


class _Msg:
    def __init__(self, content: str, tool_calls=None) -> None:
        self.content = content
        self.tool_calls = tool_calls


def test_estimator_dense_for_non_ascii() -> None:
    """Portuguese / Cyrillic / accented text emits more tokens per char than
    plain English. The estimator must use the dense ratio for these."""
    n_ascii = estimate_input_tokens([_Msg("a" * 600)])
    n_pt = estimate_input_tokens([_Msg("á" * 600)])
    assert n_pt > n_ascii
    assert n_pt - n_ascii >= 40


def test_estimator_dense_for_json() -> None:
    """Same char count, different shape: JSON-leading text gets the dense
    ratio (chars/3) and yields ~33% more tokens than plain ASCII (chars/4)."""
    n = 1200
    plain = _Msg("a" * n)
    json_blob = _Msg("[" + ("a" * (n - 1)))  # leading [ → dense ratio
    assert estimate_input_tokens([json_blob]) > estimate_input_tokens([plain])


def test_check_overflow_skips_when_window_unset() -> None:
    out = check_overflow([_Msg("x" * 10_000_000)], context_window=0)
    assert out.overflowed is False


def test_check_overflow_flags_oversized() -> None:
    out = check_overflow([_Msg("x" * 1_400_000)], context_window=200_000)
    assert out.overflowed is True
    assert out.estimated_input_tokens > 200_000
    assert "compact" in (out.detail or "").lower()


def test_check_overflow_respects_headroom() -> None:
    msg = _Msg("x" * (4 * 199_500))  # ≈ 199.5K tokens
    out = check_overflow([msg], context_window=200_000, output_headroom=2_000)
    assert out.overflowed is True


# ── integration: agent loop ────────────────────────────────────────────────


def _final_turn(text: str) -> list[StreamEvent]:
    return [
        ContentDeltaEvent(delta=text),
        UsageEvent(usage=Usage(input_tokens=3, output_tokens=1)),
        StopEvent(stop_reason=StopReason.STOP),
    ]


class _AssertNeverProvider(LLMProvider):
    """If the overflow guard fires before the LLM call, neither chat nor
    chat_stream should ever be invoked. These methods raise to make a leak
    loud rather than silently mocking out."""

    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        raise AssertionError("LLM must not be called when overflow fires")

    async def chat_stream(
        self, messages, *, tools=None, model=None
    ) -> AsyncIterator[StreamEvent]:
        raise AssertionError("LLM stream must not be called when overflow fires")
        yield  # pragma: no cover — generator type discipline only


class _ScriptedProvider(LLMProvider):
    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns
        self._idx = 0

    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        return ChatResponse(
            message=ChatMessage(role=Role.ASSISTANT, content="ok"),
            usage=Usage(input_tokens=1, output_tokens=1),
            stop_reason=StopReason.STOP,
            model=model or "",
        )

    async def chat_stream(
        self, messages, *, tools=None, model=None
    ) -> AsyncIterator[StreamEvent]:
        events = self._turns[self._idx]
        self._idx += 1
        for e in events:
            yield e


@pytest.mark.asyncio
async def test_overflow_guard_short_circuits_before_llm() -> None:
    """An oversized prompt must trip the guard at iteration 0 — the LLM
    is never dialed, and the consumer sees OverflowEvent + DoneEvent with
    ``context.context_overflow=True``."""

    cfg = AgentConfig(
        max_iterations=4,
        model="test/big",
        context_window=20_000,            # tiny window
        overflow_output_headroom=1_000,
    )
    agent = Agent(provider=_AssertNeverProvider(), config=cfg)

    # 100KB ≈ 33K tokens at chars/3 — well above 20K window.
    big = "x" * 100_000
    history = [ChatMessage(role=Role.USER, content=big)]

    events: list = []
    async for ev in agent.run_turn_stream(history):
        events.append(ev)

    overflow_evs = [e for e in events if isinstance(e, OverflowEvent)]
    assert len(overflow_evs) == 1
    ov = overflow_evs[0]
    assert ov.context_window == 20_000
    assert ov.estimated_input_tokens > 20_000
    assert ov.iteration == 0

    from loom.types import DoneEvent
    done_evs = [e for e in events if isinstance(e, DoneEvent)]
    assert len(done_evs) == 1
    assert done_evs[0].context.get("context_overflow") is True
    assert done_evs[0].stop_reason == StopReason.STOP


@pytest.mark.asyncio
async def test_overflow_guard_skipped_when_window_unset() -> None:
    """Without ``context_window`` the guard never runs, even on a giant
    history. Backwards-compatible default — opt-in only."""

    cfg = AgentConfig(max_iterations=2, model="test/x")
    agent = Agent(
        provider=_ScriptedProvider([_final_turn("ok")]), config=cfg
    )
    history = [ChatMessage(role=Role.USER, content="x" * 5_000_000)]
    events = []
    async for ev in agent.run_turn_stream(history):
        events.append(ev)
    overflow = [e for e in events if isinstance(e, OverflowEvent)]
    assert overflow == []  # never fired


@pytest.mark.asyncio
async def test_overflow_guard_callable_window() -> None:
    """``context_window`` can be a per-model lookup callable. Used by Nexus
    to read each registered model's window from the config registry."""

    def lookup(model_id: str) -> int:
        return {"big": 200_000, "small": 5_000}.get(model_id, 0)

    cfg = AgentConfig(
        max_iterations=2,
        model="small",
        context_window=lookup,
        overflow_output_headroom=500,
    )
    agent = Agent(provider=_AssertNeverProvider(), config=cfg)

    # 30K chars / 4 = 7500 tokens — over the 5K small window's budget.
    history = [ChatMessage(role=Role.USER, content="z" * 30_000)]
    events = []
    async for ev in agent.run_turn_stream(history, model_id="small"):
        events.append(ev)
    assert any(isinstance(e, OverflowEvent) for e in events)


@pytest.mark.asyncio
async def test_overflow_guard_runs_every_iteration() -> None:
    """The guard runs at the top of EVERY iteration, not just the first.
    Simulated by an estimator that returns a small value on the first call
    and a window-busting value on the second — proving the check fires
    before iteration 1's LLM call after content grew during iteration 0."""

    from loom.types import ToolCallDeltaEvent

    call_n = {"i": 0}

    def progressive_estimator(messages) -> int:
        call_n["i"] += 1
        # Iter 0: under budget. Iter 1: over budget.
        return 5_000 if call_n["i"] == 1 else 25_000

    # Iteration 0: emit a tool call (so the loop continues to iter 1).
    # The tool itself doesn't matter — _handle_tool_call returns an error
    # for an unknown tool, but the loop still appends a TOOL message and
    # proceeds to the next iteration. That's all we need to drive iter 1.
    iter0_calls_tool: list[StreamEvent] = [
        ToolCallDeltaEvent(index=0, id="tc1", name="unknown_tool", arguments_delta="{}"),
        UsageEvent(usage=Usage(input_tokens=10, output_tokens=2)),
        StopEvent(stop_reason=StopReason.TOOL_USE),
    ]
    iter1_should_not_happen: list[StreamEvent] = _final_turn("should not reach")

    provider = _ScriptedProvider([iter0_calls_tool, iter1_should_not_happen])
    cfg = AgentConfig(
        max_iterations=4,
        model="test/x",
        context_window=20_000,
        overflow_output_headroom=1_000,
        estimate_input_tokens=progressive_estimator,
    )
    agent = Agent(provider=provider, config=cfg)

    history = [ChatMessage(role=Role.USER, content="trigger")]
    events = []
    async for ev in agent.run_turn_stream(history):
        events.append(ev)

    overflow_evs = [e for e in events if isinstance(e, OverflowEvent)]
    assert len(overflow_evs) == 1
    assert overflow_evs[0].iteration == 1, (
        f"expected iter 1 (post-tool-result), got {overflow_evs[0].iteration}"
    )
    # Provider was dialed once for iter 0, never for iter 1.
    assert provider._idx == 1
    # Estimator was called twice (once per iteration).
    assert call_n["i"] == 2


@pytest.mark.asyncio
async def test_overflow_guard_in_blocking_run_turn() -> None:
    """run_turn (blocking) shares the same guard semantics — returns an
    AgentTurn with the overflow detail as its reply."""

    cfg = AgentConfig(
        max_iterations=4,
        model="test/x",
        context_window=10_000,
        overflow_output_headroom=500,
    )
    agent = Agent(provider=_AssertNeverProvider(), config=cfg)
    history = [ChatMessage(role=Role.USER, content="x" * 100_000)]
    turn = await agent.run_turn(history)
    assert "too large" in turn.reply.lower() or "compact" in turn.reply.lower()
    assert turn.iterations == 0


@pytest.mark.asyncio
async def test_custom_estimator_is_used() -> None:
    """A precise tokeniser passed via ``estimate_input_tokens`` overrides
    the default chars/token heuristic. Lets Nexus plug tiktoken in."""

    calls = {"n": 0}

    def fake_tokeniser(messages) -> int:
        calls["n"] += 1
        return 9_999_999  # always overflow

    cfg = AgentConfig(
        max_iterations=2,
        model="test/x",
        context_window=200_000,
        estimate_input_tokens=fake_tokeniser,
    )
    agent = Agent(provider=_AssertNeverProvider(), config=cfg)
    history = [ChatMessage(role=Role.USER, content="hi")]
    events = []
    async for ev in agent.run_turn_stream(history):
        events.append(ev)
    assert calls["n"] >= 1
    assert any(isinstance(e, OverflowEvent) for e in events)
