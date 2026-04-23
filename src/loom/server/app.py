"""FastAPI HTTP server for the agent chat API and SSE event stream.

:func:`create_app` returns an ASGI application with routes for chat,
sessions, skills, and heartbeat management, split into dedicated
router modules under :mod:`loom.server.routes`.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from loom.loop import Agent
from loom.server.routes import (
    create_chat_router,
    create_heartbeat_router,
    create_session_router,
    create_skills_router,
)
from loom.skills.registry import SkillRegistry
from loom.store.session import SessionStore
from loom.tools.registry import ToolRegistry


def create_app(
    agent: Agent,
    sessions: SessionStore,
    skills: SkillRegistry | None = None,
    tool_registry: ToolRegistry | None = None,
    extra_routes: Any = None,
    heartbeat_manager: Any = None,
    heartbeat_scheduler: Any = None,
    heartbeat_store: Any = None,
) -> FastAPI:
    app = FastAPI(title="Loom Agent", version="0.2.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    app.include_router(create_chat_router(agent, sessions))
    app.include_router(create_session_router(sessions))

    if skills:
        app.include_router(create_skills_router(skills))

    if heartbeat_manager and heartbeat_store:
        app.include_router(
            create_heartbeat_router(heartbeat_manager, heartbeat_scheduler, heartbeat_store)
        )

    return app
