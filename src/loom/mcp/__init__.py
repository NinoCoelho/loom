"""MCP (Model Context Protocol) client integration for Loom.

Optional subpackage; requires the ``[mcp]`` install extra::

    pip install 'loom[mcp]'

Quick start::

    from loom.mcp import McpClient, McpServerConfig

    config = McpServerConfig(
        name="my-server",
        transport="stdio",
        command=["npx", "-y", "my-mcp-server"],
    )

    async with McpClient(config) as client:
        tools = await client.list_tools()
        for tool in tools:
            tool_registry.register(tool)
        # agent runs here; client must stay open
        result = await agent.run_turn(messages)
"""

from loom.mcp.client import McpClient
from loom.mcp.config import McpServerConfig
from loom.mcp.handler import McpToolHandler

__all__ = [
    "McpClient",
    "McpServerConfig",
    "McpToolHandler",
]
