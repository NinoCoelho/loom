"""Compaction contract — the rescue path that lets a consumer pull an
overflowing turn back from the brink instead of terminating it.

Phase 1 covers:
* ``classify_zone`` — coarse fill-level mapping (green/yellow/orange/red).
* ``resolve_overflow`` — the executor method that runs the compactor up to
  ``max_compaction_attempts`` times, re-checking after each pass.
* The four loop-level outcomes: rescue, exhausted, compactor-raised, and
  backward-compat (no compactor ⇒ identical to the old hard-stop).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from loom.llm.base import LLMProvider
from loom.loop import Agent, AgentConfig
from loom.loop.compaction import (
    CompactionRequest,
    CompactionResult,
    classify_zone,
    zone_thresholds,
)
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

# ── unit: zone classifier ──────────────────────────────────────────────────


def test_zone_green_under_60_pct() -> None:
    assert classify_zone(10_000, 100_000) == "green"


def test_zone_yellow_60_to_80_pct() -> None:
    assert classify_zone(70_000, 100_000) == "yellow"


def test_zone_orange_80_to_90_pct() -> None:
    assert classify_zone(85_000, 100_000) == "orange"


def test_zone_red_at_90_plus_pct() -> None:
    assert classify_zone(95_000, 100_000) == "red"


def test_zone_subtracts_tools_overhead() -> None:
    # 60K / (100K - 20K overhead) = 0.75 → yellow (not green at 0.6 raw).
    assert classify_zone(60_000, 100_000, tools_overhead=20_000) == "yellow"


def test_zone_red_when_window_consumed_by_overhead() -> None:
    # No effective budget left at all.
    assert classify_zone(1, 100, tools_overhead=200) == "red"


def test_zone_thresholds_complete() -> None:
    t = zone_thresholds()
    assert t["green"] < t["yellow"] < t["orange"] < 1.0


def test_request_carries_attempt_and_zone() -> None:
    req = CompactionRequest(
        messages=[],
        estimated_tokens=12_000,
        context_window=20_000,
        zone="yellow",
        iteration=3,
        attempt=2,
    )
    assert req.attempt == 2
    assert req.zone == "yellow"


def test_result_defaults() -> None:
    res = CompactionResult(messages=[])
    assert res.actions == []
    assert res.still_overflowed is False
    assert res.tokens_after == 0


# ── fixtures: scripted provider + compactor ────────────────────────────────


def _final_turn(text: str) -> list[StreamEvent]:
    return [
        ContentDeltaEvent(delta=text),
        UsageEvent(usage=Usage(input_tokens=3, output_tokens=1)),
        StopEvent(stop_reason=StopReason.STOP),
    ]


class _ScriptedProvider(LLMProvider):
    """Replays canned stream turns and records how often it was dialed."""

    def __init__(self, turns: list[list[StreamEvent]]) -> None:
        self._turns = turns
        self._idx = 0

    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        return ChatResponse(
            message=ChatMessage(role=Role.ASSISTANT, content="non-stream"),
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


class _AssertNeverProvider(LLMProvider):
    async def chat(self, messages, *, tools=None, model=None) -> ChatResponse:
        raise AssertionError("LLM must not be called")

    async def chat_stream(
        self, messages, *, tools=None, model=None
    ) -> AsyncIterator[StreamEvent]:
        raise AssertionError("LLM stream must not be called")
        yield  # pragma: no cover


def _make_rescuing_compactor(calls: list[CompactionRequest]):
    """A compactor that, on the first call, replaces the giant history with a
    single short message so the re-check passes."""

    async def _compactor(req: CompactionRequest) -> CompactionResult:
        calls.append(req)
        shrunk = [ChatMessage(role=Role.USER, content="compact")]
        return CompactionResult(
            messages=shrunk,
            tokens_after=10,
            actions=["tool_shrink"],
            still_overflowed=False,
        )

    return _compactor


def _make_stubborn_compactor(calls: list[CompactionRequest]):
    """A compactor that claims success but returns the same oversized payload
    each time, so the loop's authoritative re-check keeps failing."""

    async def _compactor(req: CompactionRequest) -> CompactionResult:
        calls.append(req)
        return CompactionResult(
            messages=req.messages,
            tokens_after=req.estimated_tokens,
            actions=["noop"],
            still_overflowed=False,
        )

    return _compactor


# ── integration: loop outcomes ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compactor_rescues_overflowing_turn() -> None:
    """A compactor that shrinks history on attempt 1 lets the turn proceed to
    a normal LLM call — no OverflowEvent, recovered reply delivered."""

    calls: list[CompactionRequest] = []
    cfg = AgentConfig(
        max_iterations=4,
        model="test/x",
        context_window=20_000,
        overflow_output_headroom=1_000,
        compactor=_make_rescuing_compactor(calls),
    )
    provider = _ScriptedProvider([_final_turn("recovered")])
    agent = Agent(provider=provider, config=cfg)

    # 100KB ≈ 33K tokens — well over the 20K window.
    history = [ChatMessage(role=Role.USER, content="x" * 100_000)]
    events: list = []
    async for ev in agent.run_turn_stream(history):
        events.append(ev)

    assert not any(isinstance(e, OverflowEvent) for e in events)
    assert any(
        isinstance(e, ContentDeltaEvent) and e.delta == "recovered" for e in events
    )
    # Compactor dialed exactly once (attempt 1 resolved it).
    assert len(calls) == 1
    assert calls[0].attempt == 1
    assert calls[0].zone == "red"
    # LLM dialed exactly once after the rescue.
    assert provider._idx == 1


@pytest.mark.asyncio
async def test_compactor_exhausts_and_overflows() -> None:
    """When the compactor can't get under budget within the attempt budget,
    the loop falls back to the OverflowEvent hard-stop (today's behavior)."""

    calls: list[CompactionRequest] = []
    cfg = AgentConfig(
        max_iterations=4,
        model="test/x",
        context_window=20_000,
        overflow_output_headroom=1_000,
        max_compaction_attempts=2,
        compactor=_make_stubborn_compactor(calls),
    )
    agent = Agent(provider=_AssertNeverProvider(), config=cfg)
    history = [ChatMessage(role=Role.USER, content="x" * 100_000)]
    events: list = []
    async for ev in agent.run_turn_stream(history):
        events.append(ev)

    overflow = [e for e in events if isinstance(e, OverflowEvent)]
    assert len(overflow) == 1
    # The stubborn compactor was tried exactly max_compaction_attempts times.
    assert len(calls) == 2
    assert [c.attempt for c in calls] == [1, 2]


@pytest.mark.asyncio
async def test_compactor_raise_is_caught() -> None:
    """A buggy consumer compactor that raises must not crash the loop — the
    turn degrades to the OverflowEvent instead, with no swallowed state."""

    async def _boom(req: CompactionRequest) -> CompactionResult:
        raise RuntimeError("consumer bug")

    cfg = AgentConfig(
        max_iterations=2,
        model="test/x",
        context_window=20_000,
        overflow_output_headroom=1_000,
        compactor=_boom,
    )
    agent = Agent(provider=_AssertNeverProvider(), config=cfg)
    history = [ChatMessage(role=Role.USER, content="x" * 100_000)]
    events: list = []
    async for ev in agent.run_turn_stream(history):
        events.append(ev)

    assert any(isinstance(e, OverflowEvent) for e in events)


@pytest.mark.asyncio
async def test_no_compactor_is_backward_compatible() -> None:
    """With ``compactor=None`` the loop behaves exactly as before: an
    oversized prompt surfaces OverflowEvent at iteration 0 and the LLM is
    never dialed."""

    cfg = AgentConfig(
        max_iterations=4,
        model="test/x",
        context_window=20_000,
        overflow_output_headroom=1_000,
    )
    agent = Agent(provider=_AssertNeverProvider(), config=cfg)
    history = [ChatMessage(role=Role.USER, content="x" * 100_000)]
    events: list = []
    async for ev in agent.run_turn_stream(history):
        events.append(ev)

    overflow = [e for e in events if isinstance(e, OverflowEvent)]
    assert len(overflow) == 1
    assert overflow[0].iteration == 0


@pytest.mark.asyncio
async def test_compaction_events_emitted() -> None:
    """``before_compaction`` / ``after_compaction`` events fire so consumers
    (and the UI) can observe compaction activity."""

    calls: list[CompactionRequest] = []
    trace: list[tuple[str, dict]] = []

    def on_event(kind: str, payload: dict) -> None:
        trace.append((kind, payload))

    cfg = AgentConfig(
        max_iterations=4,
        model="test/x",
        context_window=20_000,
        overflow_output_headroom=1_000,
        compactor=_make_rescuing_compactor(calls),
        on_event=on_event,
    )
    provider = _ScriptedProvider([_final_turn("ok")])
    agent = Agent(provider=provider, config=cfg)
    history = [ChatMessage(role=Role.USER, content="x" * 100_000)]
    async for _ in agent.run_turn_stream(history):
        pass

    kinds = [k for k, _ in trace]
    assert "before_compaction" in kinds
    assert "after_compaction" in kinds
    after = next(p for k, p in trace if k == "after_compaction")
    assert after["actions"] == ["tool_shrink"]


@pytest.mark.asyncio
async def test_compactor_rescues_blocking_run_turn() -> None:
    """The blocking ``run_turn`` path shares the rescue semantics."""

    calls: list[CompactionRequest] = []
    cfg = AgentConfig(
        max_iterations=4,
        model="test/x",
        context_window=20_000,
        overflow_output_headroom=1_000,
        compactor=_make_rescuing_compactor(calls),
    )
    agent = Agent(provider=_ScriptedProvider([_final_turn("recovered")]), config=cfg)
    history = [ChatMessage(role=Role.USER, content="x" * 100_000)]
    turn = await agent.run_turn(history)

    assert len(calls) == 1
    assert "recovered" not in turn.reply  # scripted provider returns "non-stream" in chat()
    assert turn.iterations >= 1
