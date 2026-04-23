"""Tool registry — name-to-handler index and dispatcher.

:class:`ToolRegistry` maintains a dictionary of registered
:class:`~loom.tools.base.ToolHandler` instances keyed by tool name.
:meth:`dispatch` looks up a handler and calls it; unknown tool names
return an error :class:`~loom.tools.base.ToolResult` rather than raising.
:meth:`specs` exposes all registered tools as :class:`~loom.types.ToolSpec`
objects for LLM tool-use descriptions.
"""

from __future__ import annotations

from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, ToolHandler] = {}

    def register(self, handler: ToolHandler) -> None:
        self._handlers[handler.tool.name] = handler

    def unregister(self, name: str) -> None:
        self._handlers.pop(name, None)

    async def dispatch(self, name: str, args: dict) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(text=f"Unknown tool: {name}", is_error=True)
        try:
            return await handler.invoke(args)
        except Exception as e:
            return ToolResult(text=f"Tool error ({name}): {e}", is_error=True)

    def specs(self) -> list[ToolSpec]:
        return [h.tool for h in self._handlers.values()]

    def has(self, name: str) -> bool:
        return name in self._handlers

    def list_handlers(self) -> list[str]:
        return list(self._handlers.keys())
