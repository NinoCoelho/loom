from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from loom.tools.base import ToolHandler

_DEFAULT_AFFIRMATIVES = frozenset(
    {
        "yes",
        "y",
        "ok",
        "okay",
        "sure",
        "correct",
        "right",
        "yeah",
        "yep",
        "go ahead",
        "proceed",
        "continue",
        "please",
        "do it",
    }
)
_DEFAULT_NEGATIVES = frozenset(
    {
        "no",
        "n",
        "nope",
        "cancel",
        "stop",
        "don't",
        "dont",
        "negative",
    }
)


class AgentTurn:
    __slots__ = (
        "reply",
        "iterations",
        "skills_touched",
        "messages",
        "input_tokens",
        "output_tokens",
        "tool_calls",
        "model",
    )

    def __init__(
        self,
        reply: str,
        iterations: int = 0,
        skills_touched: list[str] | None = None,
        messages: list[Any] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        tool_calls: int = 0,
        model: str | None = None,
    ) -> None:
        self.reply = reply
        self.iterations = iterations
        self.skills_touched = skills_touched or []
        self.messages = messages or []
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.tool_calls = tool_calls
        self.model = model

    def __repr__(self) -> str:
        truncated = len(self.reply) > 60
        preview = self.reply[:60] + "..." if truncated else self.reply
        return (
            f"AgentTurn(reply={preview!r}, iterations={self.iterations}, "
            f"model={self.model!r}, tool_calls={self.tool_calls})"
        )


class AgentConfig:
    def __init__(
        self,
        max_iterations: int = 32,
        model: str | None = None,
        system_preamble: str = "",
        on_before_turn: Callable[[list[Any]], list[Any]] | None = None,
        on_after_turn: Callable[[AgentTurn], None] | None = None,
        on_tool_result: Callable[[Any, str], None] | None = None,
        extra_tools: list[ToolHandler] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        choose_model: Callable[[list[Any]], str | None] | None = None,
        limit_message_builder: Callable[[int], str] | None = None,
        affirmatives: frozenset[str] | set[str] | None = None,
        negatives: frozenset[str] | set[str] | None = None,
        serialize_event: Callable[[Any], Any] | None = None,
        before_llm_call: Callable[
            [list[Any]], list[Any] | Awaitable[list[Any]]
        ]
        | None = None,
        context_window: int
        | Callable[[str], int]
        | None = None,
        overflow_output_headroom: int = 4096,
        overflow_tools_overhead: int = 0,
        estimate_input_tokens: Callable[[list[Any]], int] | None = None,
    ) -> None:
        self.max_iterations = max_iterations
        self.model = model
        self.system_preamble = system_preamble
        self.on_before_turn = on_before_turn
        self.on_after_turn = on_after_turn
        self.on_tool_result = on_tool_result
        self.extra_tools = extra_tools or []
        self.on_event = on_event
        self.choose_model = choose_model
        self.limit_message_builder = limit_message_builder
        self.affirmatives = (
            frozenset(affirmatives) if affirmatives is not None else _DEFAULT_AFFIRMATIVES
        )
        self.negatives = frozenset(negatives) if negatives is not None else _DEFAULT_NEGATIVES
        self.serialize_event = serialize_event
        self.before_llm_call = before_llm_call
        self.context_window = context_window
        self.overflow_output_headroom = overflow_output_headroom
        self.overflow_tools_overhead = overflow_tools_overhead
        self.estimate_input_tokens = estimate_input_tokens

    def resolve_context_window(self, model_id: str | None) -> int:
        cw = self.context_window
        if cw is None:
            return 0
        if callable(cw):
            try:
                resolved = cw(model_id or "") or 0
            except Exception:
                return 0
            return int(resolved)
        return int(cw)
