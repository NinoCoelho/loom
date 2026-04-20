from __future__ import annotations

import json

from loom.store.vault import VaultProvider
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec


class VaultToolHandler(ToolHandler):
    """Vault tool — delegates to any ``VaultProvider`` implementation.

    Loom ships ``FilesystemVaultProvider`` as a default. Projects with richer
    vaults (kanban, backlinks, tag graph, etc.) should implement
    ``VaultProvider`` and pass their instance here.
    """

    def __init__(self, provider: VaultProvider) -> None:
        self._provider = provider

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="vault",
            description="Interact with the vault store.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["search", "read", "write", "list", "delete"],
                        "description": "Action to perform",
                    },
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {
                        "type": "integer",
                        "description": "Max results for search",
                    },
                    "path": {"type": "string", "description": "Vault path"},
                    "content": {"type": "string", "description": "Content to write"},
                    "metadata": {
                        "type": "object",
                        "description": "Optional metadata for write",
                    },
                    "prefix": {
                        "type": "string",
                        "description": "Prefix for listing",
                    },
                },
                "required": ["action"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        action = args.get("action", "")

        if action == "search":
            query = args.get("query", "")
            limit = args.get("limit", 10)
            results = await self._provider.search(query, limit)
            return ToolResult(
                text=json.dumps(results), metadata={"count": len(results)}
            )

        if action == "read":
            path = args.get("path", "")
            content = await self._provider.read(path)
            return ToolResult(text=content)

        if action == "write":
            path = args.get("path", "")
            content = args.get("content", "")
            metadata = args.get("metadata")
            await self._provider.write(path, content, metadata)
            return ToolResult(text=f"Wrote to {path}")

        if action == "list":
            prefix = args.get("prefix", "")
            entries = await self._provider.list(prefix)
            return ToolResult(
                text=json.dumps(entries), metadata={"count": len(entries)}
            )

        if action == "delete":
            path = args.get("path", "")
            await self._provider.delete(path)
            return ToolResult(text=f"Deleted {path}")

        return ToolResult(text=f"Unknown vault action: {action}")
