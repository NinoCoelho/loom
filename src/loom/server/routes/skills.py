"""Skill management routes."""

from __future__ import annotations

from fastapi import APIRouter

from loom.server.schemas import SkillInfo
from loom.skills.registry import SkillRegistry


def create_skills_router(skills: SkillRegistry) -> APIRouter:
    router = APIRouter()

    @router.get("/skills", response_model=list[SkillInfo])
    async def list_skills():
        return [
            SkillInfo(name=s.name, description=s.description, trust=s.trust)
            for s in skills.list()
        ]

    return router
