"""MCP server bridge — expose a Loom ToolRegistry as an MCP server.

Optional subpackage; requires the ``[mcp]`` install extra.
Uses the MCP Python SDK's ``FastMCP`` to create a server that:
- Advertises all tools from the registry via ``tools/list``
- Dispatches ``tools/call`` to the matching ``ToolHandler``

Usage::

    from loom.mcp.server_bridge import McpServerBridge
    from loom.tools.registry import ToolRegistry

    bridge = McpServerBridge(registry, name="my-agent")
    bridge.run(host="0.0.0.0", port=9000)

Or mount on an existing ASGI app::

    asgi_app = bridge.asgi_app()
"""

from __future__ import annotations

import logging
from typing import Any

from loom.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class McpServerBridge:
    """Exposes a Loom ``ToolRegistry`` as an MCP server (Streamable HTTP).

    Each registered ``ToolHandler`` is advertised as an MCP tool.
    Incoming ``tools/call`` requests are dispatched to the handler's
    ``invoke()`` method and the result is returned as text content.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        name: str = "loom-agent",
        version: str = "0.1.0",
        expose: list[str] | None = None,
    ) -> None:
        self._registry = registry
        self._name = name
        self._version = version
        self._expose = expose
        self._mcp: Any = None

    def _build_server(self) -> Any:
        """Build and return a FastMCP server instance."""
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as exc:
            raise ImportError(
                "The 'mcp' package is required for MCP server mode. "
                "Install it with: pip install 'loom[mcp]'"
            ) from exc

        mcp = FastMCP(self._name, version=self._version)
        handlers = self._registry.list_handlers()

        for handler in handlers:
            spec = handler.tool
            if self._expose is not None and spec.name not in self._expose:
                continue

            tool_name = spec.name
            tool_desc = spec.description or ""
            tool_schema = spec.parameters or {"type": "object", "properties": {}}

            _handler = handler

            async def _tool_fn(args: dict, *, _h: Any = _handler) -> str:
                try:
                    result = await _h.invoke(args)
                    return result.text
                except Exception as e:
                    return f"Error: {e}"

            mcp.add_tool(
                _tool_fn,
                name=tool_name,
                description=tool_desc,
            )

        self._mcp = mcp
        return mcp

    def run(self, *, host: str = "127.0.0.1", port: int = 9000, **kwargs: Any) -> None:
        """Start the MCP server (blocking)."""
        mcp = self._mcp or self._build_server()
        logger.info("[mcp-server] starting %s on %s:%d", self._name, host, port)
        mcp.run(transport="streamable-http", host=host, port=port, **kwargs)

    def asgi_app(self) -> Any:
        """Return the ASGI application for mounting on an existing server."""
        mcp = self._mcp or self._build_server()
        return mcp.streamable_http_app()
