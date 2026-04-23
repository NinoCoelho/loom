"""Request/response schemas for the HTTP server API.

* :class:`ChatRequest` — JSON body for the chat endpoint.
* :class:`ChatReply` — JSON response for synchronous chat.
* :class:`RespondPayload` — wrapper for streaming SSE ``data:`` lines.
* :class:`SessionInfo` — active session metadata.
* :class:`SkillInfo` — skill summary for the admin API.
"""

from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    message: str
    session_id: str = "__new__"
    context: dict | None = None
    stream: bool = False


class ChatReply(BaseModel):
    reply: str
    session_id: str
    iterations: int = 0
    skills_touched: list[str] = []
    input_tokens: int = 0
    output_tokens: int = 0
    tool_calls: int = 0
    model: str | None = None


class SessionInfo(BaseModel):
    id: str
    title: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    tool_call_count: int = 0


class SkillInfo(BaseModel):
    name: str
    description: str
    trust: str = "user"


class RespondPayload(BaseModel):
    response: str


class HeartbeatInfo(BaseModel):
    id: str
    name: str
    description: str
    schedule: str
    enabled: bool
    last_check: str | None = None
    last_fired: str | None = None
    last_error: str | None = None


class HeartbeatCreate(BaseModel):
    name: str
    description: str
    schedule: str
    instructions: str = ""
    driver_code: str
