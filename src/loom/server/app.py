from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from loom.loop import Agent, AgentConfig
from loom.llm.base import LLMProvider
from loom.llm.registry import ProviderRegistry
from loom.skills.registry import SkillRegistry
from loom.store.session import SessionStore
from loom.tools.registry import ToolRegistry
from loom.types import ChatMessage, Role
from loom.server.schemas import ChatRequest, ChatReply, SessionInfo, SkillInfo


def create_app(
    agent: Agent,
    sessions: SessionStore,
    skills: SkillRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
    extra_routes: Any = None,
) -> FastAPI:
    app = FastAPI(title="Loom Agent", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _subscribers: dict[str, list[asyncio.Queue]] = {}
    _pending: dict[str, dict[str, asyncio.Future]] = {}

    app.state.agent = agent
    app.state.sessions = sessions
    app.state.skills = skills
    app.state.tools = tool_registry or ToolRegistry()

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @app.post("/chat", response_model=ChatReply)
    async def chat(req: ChatRequest):
        session_id = req.session_id
        if session_id == "__new__":
            session_id = uuid.uuid4().hex[:12]

        session = sessions.get_or_create(session_id)
        history = sessions.get_history(session_id)

        if not history and not session.get("title"):
            sessions.set_title(session_id, req.message[:80])

        history.append(ChatMessage(role=Role.USER, content=req.message))
        turn = await agent.run_turn(history, context=req.context)

        history.append(ChatMessage(role=Role.ASSISTANT, content=turn.reply))
        sessions.replace_history(session_id, history)
        sessions.bump_usage(session_id, turn.input_tokens, turn.output_tokens, turn.tool_calls)

        return ChatReply(
            reply=turn.reply,
            session_id=session_id,
            iterations=turn.iterations,
            skills_touched=turn.skills_touched,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
            tool_calls=turn.tool_calls,
            model=turn.model,
        )

    @app.post("/chat/stream")
    async def chat_stream(req: ChatRequest):
        session_id = req.session_id
        if session_id == "__new__":
            session_id = uuid.uuid4().hex[:12]

        session = sessions.get_or_create(session_id)
        history = sessions.get_history(session_id)

        if not history and not session.get("title"):
            sessions.set_title(session_id, req.message[:80])

        history.append(ChatMessage(role=Role.USER, content=req.message))

        async def _generate():
            content_parts: list[str] = []
            async for event in await agent.run_turn_stream(history, context=req.context):
                if hasattr(event, "delta") and event.type == "content_delta":
                    content_parts.append(event.delta)
                yield f"data: {json.dumps({'type': event.type})}\n\n"

            reply = "".join(content_parts)
            history.append(ChatMessage(role=Role.ASSISTANT, content=reply))
            sessions.replace_history(session_id, history)

        return StreamingResponse(_generate(), media_type="text/event-stream")

    @app.get("/sessions", response_model=list[SessionInfo])
    async def list_sessions():
        return [SessionInfo(**s) for s in sessions.list_sessions()]

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str):
        deleted = sessions.delete_session(session_id)
        return {"deleted": deleted}

    if skills:
        @app.get("/skills", response_model=list[SkillInfo])
        async def list_skills():
            return [SkillInfo(name=s.name, description=s.description, trust=s.trust) for s in skills.list()]

    return app
