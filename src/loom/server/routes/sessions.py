"""Session management routes."""

from __future__ import annotations

from fastapi import APIRouter

from loom.server.schemas import SessionInfo
from loom.store.session import SessionStore


def create_session_router(sessions: SessionStore) -> APIRouter:
    router = APIRouter()

    @router.get("/sessions", response_model=list[SessionInfo])
    async def list_sessions():
        return [SessionInfo(**s) for s in sessions.list_sessions()]

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str):
        deleted = sessions.delete_session(session_id)
        return {"deleted": deleted}

    return router
