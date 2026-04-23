"""MCP tool handler — wraps a single MCP tool as a Loom ToolHandler.

:class:`McpToolHandler` adapts one MCP tool definition to the
:class:`~loom.tools.base.ToolHandler` interface. The MCP tool's JSON-RPC
request/response schema is translated to a
:class:`~loom.types.ToolSpec` for LLM consumption.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec


class McpToolHandler(ToolHandler):
    """Wraps a single MCP tool as a Loom ToolHandler.

    The call_fn is provided by McpClient so that the handler remains
    decoupled from transport details and avoids circular imports.
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict,
        call_fn: Callable[[str, dict], Awaitable[ToolResult]],
    ) -> None:
        self._spec = ToolSpec(
            name=name,
            description=description,
            parameters=input_schema,
        )
        self._call_fn = call_fn

    @property
    def tool(self) -> ToolSpec:
        return self._spec

    async def invoke(self, args: dict) -> ToolResult:
        return await self._call_fn(self._spec.name, args)
