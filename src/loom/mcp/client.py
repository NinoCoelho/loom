"""MCP client — connect to an MCP server and expose its tools as ToolHandlers.

Optional subpackage; requires the ``[mcp]`` install extra.
The module is importable without the extra, but ``McpClient.__aenter__``
will raise ``ImportError`` on first use if it is missing.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from loom.mcp.config import McpServerConfig
from loom.mcp.handler import McpToolHandler
from loom.tools.base import ToolResult
from loom.types import ImagePart

logger = logging.getLogger(__name__)


class McpClient:
    """Async context manager that connects to one MCP server.

    Usage::

        config = McpServerConfig(name="my-server", command=["npx", "my-mcp-server"])
        async with McpClient(config) as client:
            tools = await client.list_tools()
            for tool in tools:
                registry.register(tool)
            # keep the context alive while the agent runs
    """

    def __init__(self, config: McpServerConfig) -> None:
        self._config = config
        self._session: Any = None
        self._transport_cm: Any = None

    async def __aenter__(self) -> McpClient:
        try:
            from mcp import ClientSession
            from mcp.client.sse import sse_client
            from mcp.client.stdio import StdioServerParameters, stdio_client
        except ImportError as exc:
            raise ImportError(
                "The 'mcp' package is required for MCP support. "
                "Install it with: pip install 'loom[mcp]'"
            ) from exc

        cfg = self._config
        if cfg.transport == "stdio":
            if not cfg.command:
                raise ValueError(f"MCP server '{cfg.name}' requires 'command' for stdio transport")
            params = StdioServerParameters(
                command=cfg.command[0],
                args=cfg.command[1:],
                env=cfg.env or None,
            )
            self._transport_cm = stdio_client(params)
        else:
            if not cfg.url:
                raise ValueError(f"MCP server '{cfg.name}' requires 'url' for sse transport")
            self._transport_cm = sse_client(cfg.url, headers=cfg.headers or None)

        read, write = await self._transport_cm.__aenter__()
        self._session = ClientSession(read, write)
        await self._session.__aenter__()
        await self._session.initialize()
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._session is not None:
            await self._session.__aexit__(*args)
            self._session = None
        if self._transport_cm is not None:
            await self._transport_cm.__aexit__(*args)
            self._transport_cm = None

    def _assert_open(self) -> None:
        if self._session is None:
            raise RuntimeError("McpClient is not open — use it as an async context manager")

    async def list_tools(self) -> list[McpToolHandler]:
        """Discover tools from the server and return them as ToolHandlers."""
        self._assert_open()
        result = await self._session.list_tools()
        handlers: list[McpToolHandler] = []
        for tool in result.tools:
            schema = tool.inputSchema
            if not isinstance(schema, dict):
                schema = schema.model_dump()
            handlers.append(
                McpToolHandler(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=schema,
                    call_fn=self.call_tool,
                )
            )
        return handlers

    async def call_tool(self, name: str, args: dict) -> ToolResult:
        """Invoke a tool by name and return a ToolResult."""
        self._assert_open()
        result = await self._session.call_tool(name, args)
        parts: list[str] = []
        content_parts: list[ImagePart] = []
        for content in result.content:
            if hasattr(content, "text") and content.text is not None:
                parts.append(content.text)
            elif hasattr(content, "data") and hasattr(content, "mimeType"):
                img_dir = Path(tempfile.gettempdir()) / "loom-mcp-images"
                img_dir.mkdir(parents=True, exist_ok=True)
                import base64
                import uuid

                ext = _mime_to_ext(content.mimeType)
                fname = img_dir / f"{uuid.uuid4().hex[:12]}{ext}"
                fname.write_bytes(base64.b64decode(content.data))
                ip = ImagePart(source=str(fname), media_type=content.mimeType)
                content_parts.append(ip)
                parts.append(f"[image saved: {fname}]")
            else:
                parts.append(json.dumps(content.model_dump()))
        return ToolResult(
            text="\n".join(parts),
            is_error=bool(result.isError),
            content_parts=content_parts or None,
        )


_MIME_EXT_MAP: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


def _mime_to_ext(mime: str) -> str:
    return _MIME_EXT_MAP.get(mime, ".bin")
