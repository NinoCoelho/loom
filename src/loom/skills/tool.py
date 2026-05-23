from __future__ import annotations

from loom.skills.manager import SkillManager
from loom.skills.types import MANAGE_TOOL_SPEC
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec

_VALID_ACTIONS = frozenset({
    "create", "edit", "patch", "delete", "write_file", "remove_file",
})


class SkillToolHandler(ToolHandler):
    def __init__(self, manager: SkillManager) -> None:
        self._manager = manager

    @property
    def tool(self) -> ToolSpec:
        return MANAGE_TOOL_SPEC

    async def invoke(self, args: dict) -> ToolResult:
        action = args.get("action", "")
        name = args.get("name", "")

        if action not in _VALID_ACTIONS:
            return ToolResult(
                text=f"error: unknown action {action!r}",
                is_error=True,
            )

        if not name:
            return ToolResult(
                text="error: missing required field 'name'",
                is_error=True,
            )

        handler = getattr(self._manager, action, None)
        try:
            result = handler(args)
        except Exception as exc:
            return ToolResult(text=f"error: {exc}", is_error=True)

        is_error = isinstance(result, str) and result.startswith("error:")
        return ToolResult(text=result, is_error=is_error)
