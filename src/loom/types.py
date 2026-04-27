from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Discriminator, Tag


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(StrEnum):
    STOP = "stop"
    TOOL_USE = "tool_use"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    UNKNOWN = "unknown"


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict


class Usage(BaseModel):
    """Token usage for one provider round-trip.

    ``cache_read_tokens`` / ``cache_write_tokens`` are optional fields
    reported by providers that support prompt caching (Anthropic native,
    Anthropic-on-Bedrock, some OpenRouter proxies). Providers that
    don't report caching leave them at 0."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    source: str
    media_type: str = ""


class VideoPart(BaseModel):
    type: Literal["video"] = "video"
    source: str
    media_type: str = ""


class FilePart(BaseModel):
    type: Literal["file"] = "file"
    source: str
    media_type: str = ""


def _content_part_discriminator(v: object) -> str:
    if isinstance(v, dict):
        return v.get("type", "text")
    return getattr(v, "type", "text")


ContentPart = Annotated[
    Annotated[TextPart, Tag("text")]
    | Annotated[ImagePart, Tag("image")]
    | Annotated[VideoPart, Tag("video")]
    | Annotated[FilePart, Tag("file")],  # noqa: UP007
    Discriminator(_content_part_discriminator),
]


class ChatMessage(BaseModel):
    role: Role
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    @property
    def text_content(self) -> str | None:
        if self.content is None:
            return None
        if isinstance(self.content, str):
            return self.content
        parts = [p.text for p in self.content if isinstance(p, TextPart)]
        return " ".join(parts) or None


class ChatResponse(BaseModel):
    message: ChatMessage
    usage: Usage
    stop_reason: StopReason
    model: str


class ContentDeltaEvent(BaseModel):
    type: Literal["content_delta"] = "content_delta"
    delta: str


class ToolCallDeltaEvent(BaseModel):
    type: Literal["tool_call_delta"] = "tool_call_delta"
    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str | None = None


class UsageEvent(BaseModel):
    type: Literal["usage"] = "usage"
    usage: Usage


class StopEvent(BaseModel):
    type: Literal["stop"] = "stop"
    stop_reason: StopReason


class ToolExecStartEvent(BaseModel):
    """Emitted right before a tool call dispatches to its handler."""

    type: Literal["tool_exec_start"] = "tool_exec_start"
    tool_call_id: str
    name: str
    arguments: str


class ToolExecResultEvent(BaseModel):
    """Emitted after a tool call returns (or raises)."""

    type: Literal["tool_exec_result"] = "tool_exec_result"
    tool_call_id: str
    name: str
    text: str
    is_error: bool = False


class LimitReachedEvent(BaseModel):
    """Emitted when the agent loop exits due to max_iterations."""

    type: Literal["limit_reached"] = "limit_reached"
    iterations: int


class ErrorEvent(BaseModel):
    """Emitted for non-fatal turn-level errors surfaced to consumers.

    ``status_code`` is the upstream HTTP status when known (e.g. 429, 503).
    ``retryable`` flags transient failures callers may choose to retry;
    it's advisory — the agent itself does not retry after emitting this.
    """

    type: Literal["error"] = "error"
    message: str
    reason: str | None = None
    status_code: int | None = None
    retryable: bool = False


class OverflowEvent(BaseModel):
    """Emitted when the agent loop refuses an LLM call because the prompt
    would exceed the model's context window.

    Distinct from ``ErrorEvent``: this is a predictable, preventable
    condition with structured info (estimated tokens, window) so callers
    can offer a "compact history" affordance instead of a generic retry.

    Always followed by a ``DoneEvent`` so consumers can tear down the
    same way they would for a normal completion.
    """

    type: Literal["context_overflow"] = "context_overflow"
    message: str
    estimated_input_tokens: int
    context_window: int
    headroom: int
    iteration: int


class DoneEvent(BaseModel):
    """Terminal marker for a streaming turn.

    Typed fields carry the data every consumer needs (model, token usage,
    iteration count, stop reason).  The freeform ``context`` dict remains
    available for extra metadata that doesn't warrant a dedicated field.

    ``context`` is **not** included in the SSE output by default — only
    the typed fields and ``stop_reason`` are serialised.  Consumers that
    need ``context`` should access it from the Pydantic model directly.
    """

    type: Literal["done"] = "done"
    model: str = ""
    iterations: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    stop_reason: StopReason | None = None
    session_id: str | None = None
    skills_touched: list[str] = []
    context: dict = {}


StreamEvent = (
    ContentDeltaEvent,
    ToolCallDeltaEvent,
    UsageEvent,
    StopEvent,
    ToolExecStartEvent,
    ToolExecResultEvent,
    LimitReachedEvent,
    ErrorEvent,
    OverflowEvent,
    DoneEvent,
)
"""Union type for all events yielded by :meth:`LLMProvider.chat_stream`.

Consumers should ``isinstance`` against individual event types or check
``event.type`` to discriminate.  The tuple form allows static type-checkers
to narrow correctly."""
