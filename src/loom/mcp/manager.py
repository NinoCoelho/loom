"""MCP manager — lifecycle for multiple MCP server connections.

:class:`McpManager` owns a set of :class:`~loom.mcp.client.McpClient`
instances, connects them all on enter, tears them down on exit, and
provides a single :meth:`all_tool_handlers` call that returns every
tool from every connected server (with optional namespace prefixes).

Usage::

    configs = [
        McpServerConfig(name="github", transport="streamable-http", url="..."),
        McpServerConfig(name="fs", transport="stdio", command=["npx", "fs-server"]),
    ]
    async with McpManager(configs) as mgr:
        for handler in await mgr.all_tool_handlers():
            registry.register(handler)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from loom.mcp.client import McpClient
from loom.mcp.config import McpServerConfig
from loom.mcp.handler import McpToolHandler

logger = logging.getLogger(__name__)

# Type aliases for callback signatures.
SamplingFn = Callable[..., Awaitable[str]]
ElicitationFn = Callable[[str, dict], Awaitable[dict | None]]


@dataclass
class ServerStatus:
    name: str
    transport: str
    connected: bool
    tool_count: int = 0
    error: str = ""


class McpManager:
    """Manages multiple MCP server connections as a single unit."""

    def __init__(
        self,
        configs: list[McpServerConfig],
        *,
        namespace_prefix: str | None = None,
        sampling_fn: SamplingFn | None = None,
        elicitation_fn: ElicitationFn | None = None,
    ) -> None:
        self._configs: dict[str, McpServerConfig] = {c.name: c for c in configs}
        self._clients: dict[str, McpClient] = {}
        self._namespace_prefix = namespace_prefix
        self._sampling_fn = sampling_fn
        self._elicitation_fn = elicitation_fn

    async def __aenter__(self) -> McpManager:
        for name, config in self._configs.items():
            try:
                client = McpClient(config)
                await client.__aenter__()
                self._clients[name] = client
                logger.info("[mcp] connected to server %r (%s)", name, config.transport)
            except Exception:
                logger.exception("[mcp] failed to connect server %r", name)
        return self

    async def __aexit__(self, *args: Any) -> None:
        errors: list[str] = []
        for name, client in self._clients.items():
            try:
                await client.__aexit__(*args)
            except Exception:
                logger.exception("[mcp] error closing server %r", name)
                errors.append(name)
        self._clients.clear()
        if errors:
            logger.warning("[mcp] errors closing servers: %s", ", ".join(errors))

    def _namespace(self, server_name: str) -> str | None:
        if self._namespace_prefix is not None:
            return f"{self._namespace_prefix}__{server_name}"
        return server_name

    async def all_tool_handlers(self) -> list[McpToolHandler]:
        """Return every tool from every connected server, namespaced."""
        handlers: list[McpToolHandler] = []
        for name, client in self._clients.items():
            try:
                server_handlers = await client.list_tools(namespace=self._namespace(name))
                handlers.extend(server_handlers)
            except Exception:
                logger.exception("[mcp] failed to list tools from %r", name)
        return handlers

    async def all_resource_specs(self) -> list[dict[str, Any]]:
        """Return every resource from every connected server."""
        resources: list[dict[str, Any]] = []
        for name, client in self._clients.items():
            try:
                server_resources = await client.list_resources()
                for r in server_resources:
                    r["_server"] = name
                resources.extend(server_resources)
            except Exception:
                logger.exception("[mcp] failed to list resources from %r", name)
        return resources

    async def all_prompt_specs(self) -> list[dict[str, Any]]:
        """Return every prompt template from every connected server."""
        prompts: list[dict[str, Any]] = []
        for name, client in self._clients.items():
            try:
                server_prompts = await client.list_prompts()
                for p in server_prompts:
                    p["_server"] = name
                prompts.extend(server_prompts)
            except Exception:
                logger.exception("[mcp] failed to list prompts from %r", name)
        return prompts

    async def read_resource(self, server_name: str, uri: str) -> str:
        """Read a resource from a specific server."""
        client = self._clients.get(server_name)
        if client is None:
            raise ValueError(f"MCP server {server_name!r} is not connected")
        return await client.read_resource(uri)

    async def get_prompt(
        self, server_name: str, prompt_name: str, args: dict[str, str] | None = None,
    ) -> str:
        """Render a prompt template from a specific server."""
        client = self._clients.get(server_name)
        if client is None:
            raise ValueError(f"MCP server {server_name!r} is not connected")
        return await client.get_prompt(prompt_name, args)

    async def refresh_tools(self, server_name: str) -> list[McpToolHandler]:
        """Re-discover tools from a specific server."""
        client = self._clients.get(server_name)
        if client is None:
            raise ValueError(f"MCP server {server_name!r} is not connected")
        return await client.refresh_tools(namespace=self._namespace(server_name))

    async def reconnect(self, server_name: str) -> None:
        """Reconnect a specific server."""
        if server_name not in self._configs:
            raise ValueError(f"Unknown MCP server: {server_name!r}")
        if server_name in self._clients:
            try:
                await self._clients[server_name].__aexit__(None, None, None)
            except Exception:
                logger.exception("[mcp] error closing %r during reconnect", server_name)
            del self._clients[server_name]
        client = McpClient(self._configs[server_name])
        await client.__aenter__()
        self._clients[server_name] = client
        logger.info("[mcp] reconnected server %r", server_name)

    async def disconnect(self, server_name: str) -> None:
        """Disconnect a specific server."""
        client = self._clients.pop(server_name, None)
        if client is not None:
            await client.__aexit__(None, None, None)

    @property
    def server_names(self) -> list[str]:
        return list(self._configs.keys())

    @property
    def connected_servers(self) -> list[str]:
        return list(self._clients.keys())

    def server_statuses(self) -> list[ServerStatus]:
        statuses: list[ServerStatus] = []
        for name, config in self._configs.items():
            client = self._clients.get(name)
            if client is None:
                statuses.append(ServerStatus(
                    name=name, transport=config.transport, connected=False,
                ))
            else:
                try:
                    count = len(client.list_tools.__wrapped__)  # type: ignore[attr-defined]
                except Exception:
                    count = 0
                statuses.append(ServerStatus(
                    name=name, transport=config.transport, connected=True, tool_count=count,
                ))
        return statuses

    def is_connected(self, name: str) -> bool:
        return name in self._clients

    @property
    def sampling_fn(self) -> SamplingFn | None:
        return self._sampling_fn

    @sampling_fn.setter
    def sampling_fn(self, fn: SamplingFn | None) -> None:
        self._sampling_fn = fn

    @property
    def elicitation_fn(self) -> ElicitationFn | None:
        return self._elicitation_fn

    @elicitation_fn.setter
    def elicitation_fn(self, fn: ElicitationFn | None) -> None:
        self._elicitation_fn = fn
