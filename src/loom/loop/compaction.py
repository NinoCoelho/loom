"""Compaction contract — let consumers rescue an overflowing turn.

Loom detects context-window overflow before each LLM call and, historically,
terminated the turn with an ``OverflowEvent``. That left every consumer to
reimplement the same dance around the framework: notice the overflow, shrink
history, retry. This module defines the contract for delegating that rescue
*back* to the consumer.

When an ``AgentConfig.compactor`` is set, the loop calls it with a
``CompactionRequest`` (the messages, the estimate, the zone, the attempt
number) and expects back a ``CompactionResult`` with a possibly-shortened
message list. The loop re-checks overflow and either proceeds or tries again,
up to ``max_compaction_attempts``. With no compactor wired, behavior is
unchanged — the loop still emits ``OverflowEvent`` and stops.

The zone classifier is co-located here so consumers don't need to recompute it
from raw token counts, and so the contract carries a coarse "how full are we"
signal that the compactor can use to scale its aggressiveness (e.g. compact
more in orange than in yellow).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

Zone = Literal["green", "yellow", "orange", "red"]

# Context-fill thresholds, expressed as a fraction of the effective window
# (window minus tools/system overhead). Below green the session is comfortable;
# red means there is no room left for a reply.
_GREEN_THRESHOLD = 0.60
_YELLOW_THRESHOLD = 0.80
_ORANGE_THRESHOLD = 0.90


def classify_zone(
    tokens_used: int,
    context_window: int,
    *,
    tools_overhead: int = 0,
) -> Zone:
    """Map a token usage figure onto a coarse ``Zone`` label.

    ``tools_overhead`` is subtracted from the window first, mirroring the
    budget arithmetic in ``overflow.check_overflow``: the tool JSON-Schema
    payloads consume context budget even though they aren't counted in the
    message-token estimate.
    """
    effective = context_window - tools_overhead
    if effective <= 0:
        return "red"
    pct = tokens_used / effective
    if pct < _GREEN_THRESHOLD:
        return "green"
    if pct < _YELLOW_THRESHOLD:
        return "yellow"
    if pct < _ORANGE_THRESHOLD:
        return "orange"
    return "red"


def zone_thresholds() -> dict[str, float]:
    """Expose the thresholds so callers (UI, tests) can render the scale."""
    return {
        "green": _GREEN_THRESHOLD,
        "yellow": _YELLOW_THRESHOLD,
        "orange": _ORANGE_THRESHOLD,
    }


@dataclass
class CompactionRequest:
    """Everything a compactor needs to decide how aggressively to shrink.

    ``attempt`` is 1-based and increments on each retry within a single turn,
    so a compactor can escalate (e.g. attempt 1 = tool-shrink, attempt 2 =
    summarize, attempt 3 = drop low-relevance messages).
    """

    messages: list[Any]
    estimated_tokens: int
    context_window: int
    zone: Zone
    iteration: int
    attempt: int


@dataclass
class CompactionResult:
    """Outcome of a single compaction pass.

    ``messages`` replaces the working history for subsequent overflow
    re-checks and (if resolved) the LLM call. ``actions`` is a freeform log
    of what was done (e.g. ``["tool_shrink", "summarize"]``) for observability.
    ``still_overflowed`` is advisory — the loop always re-checks authoritatively
    via ``check_overflow`` and treats that as ground truth; this flag merely
    lets a compactor signal "I made progress but know it isn't enough" without
    forcing a full re-scan.
    """

    messages: list[Any]
    tokens_after: int = 0
    actions: list[str] = field(default_factory=list)
    still_overflowed: bool = False


if TYPE_CHECKING:
    # A compactor is just an async callable. Kept as a type alias (not a
    # Protocol) to match the style of ``before_llm_call`` / ``choose_model`` in
    # AgentConfig. Defined under TYPE_CHECKING so consumers reference it as a
    # string annotation: ``compactor: "Compactor | None" = None``.
    Compactor = Callable[[CompactionRequest], Awaitable[CompactionResult]]
