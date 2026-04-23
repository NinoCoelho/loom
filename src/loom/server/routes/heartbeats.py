"""Heartbeat management routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from loom.server.schemas import HeartbeatCreate, HeartbeatInfo


def create_heartbeat_router(
    manager: Any,
    scheduler: Any,
    store: Any,
) -> APIRouter:
    router = APIRouter()

    @router.get("/heartbeats", response_model=list[HeartbeatInfo])
    async def list_heartbeats():
        registry = manager._registry
        runs = {r.heartbeat_id: r for r in store.list_runs()}
        result = []
        for record in registry.list():
            run = runs.get(record.id)
            result.append(
                HeartbeatInfo(
                    id=record.id,
                    name=record.name,
                    description=record.description,
                    schedule=record.schedule,
                    enabled=record.enabled,
                    last_check=run.last_check.isoformat() if run and run.last_check else None,
                    last_fired=run.last_fired.isoformat() if run and run.last_fired else None,
                    last_error=run.last_error if run else None,
                )
            )
        return result

    @router.post("/heartbeats", response_model=dict)
    async def create_heartbeat(body: HeartbeatCreate):
        result = manager.invoke(
            {
                "action": "create",
                "name": body.name,
                "description": body.description,
                "schedule": body.schedule,
                "instructions": body.instructions,
                "driver_code": body.driver_code,
            }
        )
        if result.startswith("error:"):
            raise HTTPException(status_code=400, detail=result)
        return {"result": result}

    @router.delete("/heartbeats/{heartbeat_id}", response_model=dict)
    async def delete_heartbeat(heartbeat_id: str):
        result = manager.invoke({"action": "delete", "name": heartbeat_id})
        if result.startswith("error:"):
            raise HTTPException(status_code=404, detail=result)
        return {"result": result}

    @router.post("/heartbeats/{heartbeat_id}/enable", response_model=dict)
    async def enable_heartbeat(heartbeat_id: str):
        result = manager.invoke({"action": "enable", "name": heartbeat_id})
        if result.startswith("error:"):
            raise HTTPException(status_code=404, detail=result)
        return {"result": result}

    @router.post("/heartbeats/{heartbeat_id}/disable", response_model=dict)
    async def disable_heartbeat(heartbeat_id: str):
        result = manager.invoke({"action": "disable", "name": heartbeat_id})
        if result.startswith("error:"):
            raise HTTPException(status_code=404, detail=result)
        return {"result": result}

    if scheduler:

        @router.post("/heartbeats/{heartbeat_id}/trigger", response_model=dict)
        async def trigger_heartbeat(heartbeat_id: str):
            try:
                turns = await scheduler.trigger(heartbeat_id)
                return {"fired": len(turns), "heartbeat_id": heartbeat_id}
            except KeyError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            except Exception as exc:
                raise HTTPException(status_code=500, detail=str(exc))

    return router
