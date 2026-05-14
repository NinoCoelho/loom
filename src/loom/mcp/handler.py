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

    When *namespace* is provided the tool's public name becomes
    ``{namespace}__{name}`` while :meth:`invoke` still sends the
    original name to the MCP server.  This lets multiple MCP servers
    expose tools with the same base name without colliding in the
    ToolRegistry.
    """

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict,
        call_fn: Callable[[str, dict], Awaitable[ToolResult]],
        *,
        namespace: str | None = None,
        meta: dict | None = None,
    ) -> None:
        self._original_name = name
        self._namespace = namespace
        self._meta = meta
        prefixed = f"{namespace}__{name}" if namespace else name
        self._spec = ToolSpec(
            name=prefixed,
            description=description,
            parameters=input_schema,
            meta=meta,
        )
        self._call_fn = call_fn

    @property
    def original_name(self) -> str:
        return self._original_name

    @property
    def namespace(self) -> str | None:
        return self._namespace

    @property
    def meta(self) -> dict | None:
        return self._meta

    @property
    def tool(self) -> ToolSpec:
        return self._spec

    async def invoke(self, args: dict) -> ToolResult:
        return await self._call_fn(self._original_name, args)
