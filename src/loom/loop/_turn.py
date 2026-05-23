from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar


@dataclass
class TurnState:
    pending_question: str | None = None
    skills_touched: list[str] = field(default_factory=list)
    last_tc_signature: tuple[tuple[str, str], ...] | None = None
    identical_tc_streak: int = 0
    total_input: int = 0
    total_output: int = 0
    total_tool_calls: int = 0
    IDENTICAL_TC_LIMIT: ClassVar[int] = 3
