from __future__ import annotations

import re
from enum import StrEnum


class MessageCategory(StrEnum):
    CODING = "coding"
    REASONING = "reasoning"
    TRIVIAL = "trivial"
    BALANCED = "balanced"


_CODING_PATTERNS = re.compile(
    r"\b(?:def|class|import|from|function|return|async|await|SELECT|INSERT|UPDATE|DELETE|"
    r"traceback|error|exception|bug|fix|debug|compile|syntax|module|package)\b",
    re.I,
)

_REASONING_PATTERNS = re.compile(
    r"\b(?:why|explain|analyze|compare|evaluate|plan|design|architect|should|would|"
    r"pros|cons|trade.?off|implication|strategy|approach|recommend)\b",
    re.I,
)


def classify_message(text: str) -> MessageCategory:
    if len(text) < 80:
        return MessageCategory.TRIVIAL
    if _CODING_PATTERNS.search(text):
        return MessageCategory.CODING
    if _REASONING_PATTERNS.search(text) and len(text) > 40:
        return MessageCategory.REASONING
    return MessageCategory.BALANCED
