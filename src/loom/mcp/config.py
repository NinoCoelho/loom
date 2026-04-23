"""MCP server connection configuration.

:class:`McpServerConfig` describes a single MCP server: its name,
transport (``stdio`` or ``http``), command or URL, and optional
environment variables. Passed to :class:`~loom.mcp.client.McpClient`
to open a connection.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class McpServerConfig(BaseModel):
    """Configuration for a single MCP server connection."""

    name: str
    transport: Literal["stdio", "sse"] = "stdio"

    # stdio transport — launch a subprocess
    command: list[str] | None = None
    env: dict[str, str] = {}

    # sse transport — connect to a running server
    url: str | None = None
    headers: dict[str, str] = {}
