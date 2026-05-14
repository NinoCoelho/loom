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
        elif cfg.transport == "streamable-http":
            if not cfg.url:
                raise ValueError(
                    f"MCP server '{cfg.name}' requires 'url' for streamable-http transport"
                )
            try:
                from mcp.client.streamable_http import streamablehttp_client
            except ImportError as exc:
                raise ImportError(
                    "Streamable HTTP transport requires mcp >= 1.2. "
                    "Upgrade with: pip install 'loom[mcp]'"
                ) from exc
            self._transport_cm = streamablehttp_client(
                cfg.url, headers=cfg.headers or None,
            )
        else:
            if not cfg.url:
                raise ValueError(f"MCP server '{cfg.name}' requires 'url' for sse transport")
            self._transport_cm = sse_client(cfg.url, headers=cfg.headers or None)

        transport_result = await self._transport_cm.__aenter__()
        # streamable-http may return 3 values (read, write, session_info_fn)
        if isinstance(transport_result, tuple):
            read, write = transport_result[0], transport_result[1]
        else:
            read, write = transport_result
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

    async def list_tools(self, *, namespace: str | None = None) -> list[McpToolHandler]:
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
                    namespace=namespace,
                    meta=tool.meta,
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

    async def list_resources(self) -> list[dict[str, Any]]:
        """Discover resources exposed by the server."""
        self._assert_open()
        result = await self._session.list_resources()
        out: list[dict[str, Any]] = []
        for res in result.resources:
            entry: dict[str, Any] = {"uri": str(res.uri), "name": res.name}
            if getattr(res, "description", None):
                entry["description"] = res.description
            if getattr(res, "mimeType", None):
                entry["mimeType"] = res.mimeType
            out.append(entry)
        return out

    async def read_resource(self, uri: str) -> str:
        """Read a resource by URI and return its text content."""
        self._assert_open()
        result = await self._session.read_resource(uri)
        parts: list[str] = []
        for content in result.contents:
            if hasattr(content, "text") and content.text is not None:
                parts.append(content.text)
            elif hasattr(content, "blob") and content.blob is not None:
                import base64
                parts.append(base64.b64decode(content.blob).decode("utf-8", errors="replace"))
            else:
                parts.append(str(content))
        return "\n".join(parts)

    async def refresh_tools(self, *, namespace: str | None = None) -> list[McpToolHandler]:
        """Re-discover tools — convenience wrapper for re-calling list_tools."""
        return await self.list_tools(namespace=namespace)

    async def list_prompts(self) -> list[dict[str, Any]]:
        """Discover prompt templates exposed by the server."""
        self._assert_open()
        result = await self._session.list_prompts()
        out: list[dict[str, Any]] = []
        for p in result.prompts:
            entry: dict[str, Any] = {"name": p.name}
            if getattr(p, "description", None):
                entry["description"] = p.description
            if getattr(p, "arguments", None):
                entry["arguments"] = [
                    {
                        "name": a.name,
                        "description": getattr(a, "description", ""),
                        "required": getattr(a, "required", False),
                    }
                    for a in p.arguments
                ]
            out.append(entry)
        return out

    async def get_prompt(self, name: str, args: dict[str, str] | None = None) -> str:
        """Render a prompt template and return its text content."""
        self._assert_open()
        result = await self._session.get_prompt(name, arguments=args)
        parts: list[str] = []
        for msg in result.messages:
            content = getattr(msg, "content", None)
            if content is None:
                continue
            if hasattr(content, "text"):
                parts.append(content.text)
            else:
                parts.append(str(content))
        return "\n\n".join(parts)

    def resource_as_tool_spec(
        self, resource: dict[str, Any], *, namespace: str | None = None,
    ) -> tuple[str, str, dict]:
        """Build a (name, description, inputSchema) tuple for a resource.

        Used to expose MCP resources as read-only tools in the ToolRegistry.
        """
        uri = resource.get("uri", "")
        name = uri.rsplit("/", 1)[-1] if "/" in uri else uri
        prefixed = f"{namespace}__resource__{name}" if namespace else f"resource__{name}"
        desc = resource.get("description", f"Read resource: {uri}")
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": f"Resource URI (default: {uri})",
                    "default": uri,
                },
            },
        }
        return prefixed, desc, schema


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
