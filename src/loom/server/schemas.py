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
