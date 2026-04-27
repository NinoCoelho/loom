"""Pre-LLM-call context-window overflow detection.

Some providers (notably z.ai's GLM and Qwen-style endpoints) silently return
HTTP 200 with empty content when the prompt exceeds the model's context
window. The agent layer can't tell that apart from a model that genuinely
had nothing to say, so it loops forever on retries that hit the same wall.

This module estimates prompt size cheaply (chars/token + per-message
overhead) and lets the agent loop refuse the call up front with a structured
``OverflowEvent`` instead of relying on the silent failure mode.

Token counting is intentionally rough — chars/3 for non-ASCII / JSON-shaped
text, chars/4 for plain English. Real tokenisation belongs to the upstream;
we only need to be roughly right (within ~30%) to flag obvious overflows.

The check is opt-in: the agent only runs it when a context window is
configured for the chosen model.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


# Per-message overhead (role markers, separators, etc).
_PER_MESSAGE_TOKENS = 4

# Chars/token ratios — see module docstring for the rationale.
_CHARS_PER_TOKEN_ASCII = 4
_CHARS_PER_TOKEN_DENSE = 3

# Sample size for the per-message ratio decision. 512 chars is enough to
# detect non-ASCII / JSON shape without paying O(n) per long tool result.
_SAMPLE_LEN = 512
# Fraction of non-ASCII chars in the sample above which we treat the text as
# "dense" and use the lower chars/token ratio.
_NON_ASCII_THRESHOLD = 0.05


@dataclass(frozen=True)
class OverflowCheck:
    overflowed: bool
    estimated_input_tokens: int
    context_window: int
    headroom: int
    detail: str | None = None


def _msg_text(msg: Any) -> str:
    """Best-effort extraction of the textual payload of a ChatMessage-like
    object. Handles loom ChatMessage (str / list / None content) and any
    pydantic model with a ``.content`` attr."""
    content = getattr(msg, "content", None)
    if content is None and isinstance(msg, dict):
        content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(content)


def _chars_per_token(text: str) -> int:
    """Pick chars/token ratio for a text segment.

    JSON-shaped (starts with ``[`` or ``{``) and non-ASCII-heavy text gets
    the denser ratio because tokenisers emit ~30% more tokens per char on
    those inputs. Plain English keeps the looser ratio.
    """
    if not text:
        return _CHARS_PER_TOKEN_ASCII
    sample = text[:_SAMPLE_LEN]
    stripped = sample.lstrip()
    if stripped[:1] in ("[", "{"):
        return _CHARS_PER_TOKEN_DENSE
    non_ascii = sum(1 for c in sample if ord(c) > 127)
    if non_ascii / max(1, len(sample)) > _NON_ASCII_THRESHOLD:
        return _CHARS_PER_TOKEN_DENSE
    return _CHARS_PER_TOKEN_ASCII


def estimate_input_tokens(messages: Iterable[Any]) -> int:
    """Cheap chars-per-token estimate with per-message overhead.

    Tool-call payloads are always JSON, so they get the dense ratio
    unconditionally regardless of the surrounding content's shape.
    """
    total = 0
    n = 0
    for m in messages:
        text = _msg_text(m)
        if text:
            total += len(text) // _chars_per_token(text)
        n += 1
        tcs = getattr(m, "tool_calls", None) or (
            m.get("tool_calls") if isinstance(m, dict) else None
        )
        if tcs:
            try:
                tc_text = json.dumps(tcs, default=str, ensure_ascii=False)
            except (TypeError, ValueError):
                tc_text = " ".join(str(tc) for tc in tcs)
            total += len(tc_text) // _CHARS_PER_TOKEN_DENSE
    return total + n * _PER_MESSAGE_TOKENS


def check_overflow(
    messages: Iterable[Any],
    *,
    context_window: int,
    output_headroom: int = 4096,
    estimator: "Callable[[Iterable[Any]], int] | None" = None,  # type: ignore[name-defined]
) -> OverflowCheck:
    """Return an OverflowCheck describing whether ``messages`` likely fits.

    ``context_window <= 0`` disables the check (caller hasn't configured a
    limit). ``estimator`` lets callers swap in a precise tokenizer when one
    is available; defaults to the cheap chars/token heuristic.
    """
    estimate = estimator or estimate_input_tokens
    est = estimate(messages)
    if context_window <= 0:
        return OverflowCheck(False, est, 0, 0)
    budget = context_window - output_headroom
    if est <= budget:
        return OverflowCheck(False, est, context_window, output_headroom)
    pct = est * 100 // max(1, context_window)
    detail = (
        f"Conversation is too large for this model: ~{est:,} input tokens "
        f"vs. {context_window:,} window ({pct}% of capacity, no room for a "
        f"reply). Compact the history or start a new session."
    )
    return OverflowCheck(True, est, context_window, output_headroom, detail)
