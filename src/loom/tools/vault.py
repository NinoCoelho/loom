from __future__ import annotations

import json
from typing import Any, Protocol

from loom.types import ToolSpec
from loom.tools.base import ToolHandler, ToolResult


class VaultStore(Protocol):
    async def search(self, query: str, limit: int) -> list[dict[str, Any]]: ...
    async def read(self, path: str) -> str: ...
    async def write(
        self, path: str, content: str, metadata: dict | None = None
    ) -> None: ...
    async def list(self, prefix: str) -> list[str]: ...


class VaultToolHandler(ToolHandler):
    def __init__(self, store: VaultStore) -> None:
        self._store = store

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
                        "enum": ["search", "read", "write", "list"],
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
            results = await self._store.search(query, limit)
            return ToolResult(
                text=json.dumps(results), metadata={"count": len(results)}
            )

        if action == "read":
            path = args.get("path", "")
            content = await self._store.read(path)
            return ToolResult(text=content)

        if action == "write":
            path = args.get("path", "")
            content = args.get("content", "")
            metadata = args.get("metadata")
            await self._store.write(path, content, metadata)
            return ToolResult(text=f"Wrote to {path}")

        if action == "list":
            prefix = args.get("prefix", "")
            entries = await self._store.list(prefix)
            return ToolResult(
                text=json.dumps(entries), metadata={"count": len(entries)}
            )

        return ToolResult(text=f"Unknown vault action: {action}")
