"""Chat routes — synchronous and streaming chat endpoints."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from loom.loop import Agent
from loom.server.schemas import ChatReply, ChatRequest
from loom.store.session import SessionStore
from loom.types import ChatMessage, Role


def create_chat_router(agent: Agent, sessions: SessionStore) -> APIRouter:
    router = APIRouter()

    @router.post("/chat", response_model=ChatReply)
    async def chat(req: ChatRequest):
        session_id = req.session_id
        if session_id == "__new__":
            session_id = uuid.uuid4().hex[:12]

        session = sessions.get_or_create(session_id)
        history = sessions.get_history(session_id)

        if not history and not session.get("title"):
            sessions.set_title(session_id, req.message[:80])

        history.append(ChatMessage(role=Role.USER, content=req.message))
        turn = await agent.run_turn(history, context=req.context, model_id=req.model)

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

    @router.post("/chat/stream")
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
            async for event in await agent.run_turn_stream(
                history, context=req.context, model_id=req.model
            ):
                if hasattr(event, "delta") and event.type == "content_delta":
                    content_parts.append(event.delta)
                yield f"data: {json.dumps({'type': event.type})}\n\n"

            reply = "".join(content_parts)
            history.append(ChatMessage(role=Role.ASSISTANT, content=reply))
            sessions.replace_history(session_id, history)

        return StreamingResponse(_generate(), media_type="text/event-stream")

    return router
