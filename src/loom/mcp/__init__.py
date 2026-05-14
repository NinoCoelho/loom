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

Multi-server management::

    from loom.mcp import McpManager, McpServerConfig

    configs = [
        McpServerConfig(name="github", transport="streamable-http", url="..."),
        McpServerConfig(name="fs", transport="stdio", command=["npx", "fs-server"]),
    ]
    async with McpManager(configs) as mgr:
        for handler in await mgr.all_tool_handlers():
            registry.register(handler)
"""

from loom.mcp.client import McpClient
from loom.mcp.config import McpServerConfig
from loom.mcp.handler import McpToolHandler
from loom.mcp.manager import McpManager

__all__ = [
    "McpClient",
    "McpManager",
    "McpServerConfig",
    "McpToolHandler",
]
