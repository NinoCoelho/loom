from __future__ import annotations

from enum import Enum
from typing import Literal, Union

from pydantic import BaseModel


class Role(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class StopReason(str, Enum):
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
    input_tokens: int
    output_tokens: int


class ChatMessage(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


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
    """Emitted for non-fatal turn-level errors surfaced to consumers."""

    type: Literal["error"] = "error"
    message: str
    reason: str | None = None


StreamEvent = Union[
    ContentDeltaEvent,
    ToolCallDeltaEvent,
    UsageEvent,
    StopEvent,
    ToolExecStartEvent,
    ToolExecResultEvent,
    LimitReachedEvent,
    ErrorEvent,
]
