from __future__ import annotations

from loom.heartbeat.manager import HeartbeatManager
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec


class HeartbeatToolHandler(ToolHandler):
    """LLM-facing tool that lets the agent create, list, and manage heartbeats."""

    def __init__(self, manager: HeartbeatManager) -> None:
        self._manager = manager

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="manage_heartbeat",
            description=(
                "Create, delete, enable, disable, or list heartbeats. "
                "A heartbeat is a recurring scheduled task: the driver detects events "
                "and the agent processes them. "
                "Actions: create | delete | enable | disable | list."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "delete", "enable", "disable", "list"],
                        "description": "Operation to perform.",
                    },
                    "name": {
                        "type": "string",
                        "description": (
                            "Heartbeat identifier (slug: [a-zA-Z0-9_-], max 64 chars). "
                            "Must match the directory name."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "One-line description of what this heartbeat does.",
                    },
                    "schedule": {
                        "type": "string",
                        "description": (
                            "When to run. Accepts cron (e.g. '*/5 * * * *'), "
                            "@daily / @hourly shorthands, or natural language "
                            "('every 5 minutes', 'every hour', 'every 2 days')."
                        ),
                    },
                    "instructions": {
                        "type": "string",
                        "description": (
                            "Markdown instructions for the agent when an event fires. "
                            "Describe what to do with the event payload."
                        ),
                    },
                    "driver_code": {
                        "type": "string",
                        "description": (
                            "Python source for driver.py. Must define a class named "
                            "'Driver' that subclasses HeartbeatDriver and implements "
                            "async check(self, state: dict) -> tuple[list[HeartbeatEvent], dict]."
                        ),
                    },
                },
                "required": ["action"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        try:
            result = self._manager.invoke(args)
            is_error = result.startswith("error:")
            return ToolResult(text=result, is_error=is_error)
        except Exception as exc:
            return ToolResult(text=f"error: {exc}", is_error=True)
