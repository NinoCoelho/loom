"""Chat routes — synchronous and streaming chat endpoints."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from loom.loop import Agent
from loom.server.events import serialize_event
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
            # Anchor event — lets consumers correlate the stream with a
            # session immediately, before any content arrives.
            yield _sse({"type": "session", "session_id": session_id})

            content_parts: list[str] = []
            skills_activated: list[str] = []
            total_input = 0
            total_output = 0
            total_tool_calls = 0

            async for event in agent.run_turn_stream(
                history, context=req.context, model_id=req.model
            ):
                # Track content for history persistence.
                if hasattr(event, "delta") and getattr(event, "type", None) == "content_delta":
                    content_parts.append(event.delta)

                # Track skill activations from tool_exec_start.
                if getattr(event, "type", None) == "tool_exec_start" and hasattr(event, "name"):
                    if event.name == "activate_skill":
                        # Parse skill name from arguments.
                        try:
                            args = json.loads(event.arguments) if event.arguments else {}
                            skill_name = args.get("name", "")
                            if skill_name and skill_name not in skills_activated:
                                skills_activated.append(skill_name)
                        except json.JSONDecodeError:
                            pass

                # Track usage from done event.
                if getattr(event, "type", None) == "done":
                    total_input = getattr(event, "input_tokens", 0)
                    total_output = getattr(event, "output_tokens", 0)
                    total_tool_calls = getattr(event, "tool_calls", 0)

                yield _sse(serialize_event(event, session_id=session_id))

            reply = "".join(content_parts)
            history.append(ChatMessage(role=Role.ASSISTANT, content=reply))
            sessions.replace_history(session_id, history)
            sessions.bump_usage(session_id, total_input, total_output, total_tool_calls)

        return StreamingResponse(_generate(), media_type="text/event-stream")

    return router


def _sse(data: dict) -> str:
    """Format a dict as an SSE ``data:`` line."""
    return f"data: {json.dumps(data)}\n\n"
