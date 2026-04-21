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
from loom.server.schemas import (
    ChatRequest,
    ChatReply,
    HeartbeatCreate,
    HeartbeatInfo,
    SessionInfo,
    SkillInfo,
)


def create_app(
    agent: Agent,
    sessions: SessionStore,
    skills: SkillRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
    extra_routes: Any = None,
    heartbeat_manager: Any = None,   # HeartbeatManager | None
    heartbeat_scheduler: Any = None,  # HeartbeatScheduler | None
    heartbeat_store: Any = None,      # HeartbeatStore | None
) -> FastAPI:
    app = FastAPI(title="Loom Agent", version="0.2.0")
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
    app.state.heartbeat_manager = heartbeat_manager
    app.state.heartbeat_scheduler = heartbeat_scheduler
    app.state.heartbeat_store = heartbeat_store

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": "0.2.0"}

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

    if heartbeat_manager and heartbeat_store:
        _register_heartbeat_routes(app, heartbeat_manager, heartbeat_scheduler, heartbeat_store)

    return app


def _register_heartbeat_routes(
    app: FastAPI,
    manager: Any,
    scheduler: Any,
    store: Any,
) -> None:
    from fastapi import HTTPException

    @app.get("/heartbeats", response_model=list[HeartbeatInfo])
    async def list_heartbeats():
        registry = manager._registry
        runs = {r.heartbeat_id: r for r in store.list_runs()}
        result = []
        for record in registry.list():
            run = runs.get(record.id)
            result.append(HeartbeatInfo(
                id=record.id,
                name=record.name,
                description=record.description,
                schedule=record.schedule,
                enabled=record.enabled,
                last_check=run.last_check.isoformat() if run and run.last_check else None,
                last_fired=run.last_fired.isoformat() if run and run.last_fired else None,
                last_error=run.last_error if run else None,
            ))
        return result

    @app.post("/heartbeats", response_model=dict)
    async def create_heartbeat(body: HeartbeatCreate):
        result = manager.invoke({
            "action": "create",
            "name": body.name,
            "description": body.description,
            "schedule": body.schedule,
            "instructions": body.instructions,
            "driver_code": body.driver_code,
        })
        if result.startswith("error:"):
            raise HTTPException(status_code=400, detail=result)
        return {"result": result}

    @app.delete("/heartbeats/{heartbeat_id}", response_model=dict)
    async def delete_heartbeat(heartbeat_id: str):
        result = manager.invoke({"action": "delete", "name": heartbeat_id})
        if result.startswith("error:"):
            raise HTTPException(status_code=404, detail=result)
        return {"result": result}

    @app.post("/heartbeats/{heartbeat_id}/enable", response_model=dict)
    async def enable_heartbeat(heartbeat_id: str):
        result = manager.invoke({"action": "enable", "name": heartbeat_id})
        if result.startswith("error:"):
            raise HTTPException(status_code=404, detail=result)
        return {"result": result}

    @app.post("/heartbeats/{heartbeat_id}/disable", response_model=dict)
    async def disable_heartbeat(heartbeat_id: str):
        result = manager.invoke({"action": "disable", "name": heartbeat_id})
        if result.startswith("error:"):
            raise HTTPException(status_code=404, detail=result)
        return {"result": result}

    if scheduler:
        @app.post("/heartbeats/{heartbeat_id}/trigger", response_model=dict)
        async def trigger_heartbeat(heartbeat_id: str):
            try:
                turns = await scheduler.trigger(heartbeat_id)
                return {"fired": len(turns), "heartbeat_id": heartbeat_id}
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))
