from __future__ import annotations

import json
from pathlib import Path

from loom.store.memory import MemoryStore
from loom.types import ToolSpec
from loom.tools.base import ToolHandler, ToolResult


class MemoryToolHandler(ToolHandler):
    def __init__(self, store: MemoryStore) -> None:
        self._store = store

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="memory",
            description="Read, write, search, list, or delete memory entries. Memory persists across sessions.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write", "search", "list", "delete"],
                        "description": "Action to perform",
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key (for read, write, delete)",
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category for the entry (for write, list)",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for the entry (for write)",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query (for search)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (for search, list)",
                    },
                },
                "required": ["action"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        action = args.get("action", "")

        if action == "write":
            key = args.get("key", "")
            content = args.get("content", "")
            category = args.get("category", "notes")
            tags = args.get("tags", [])
            if not key:
                return ToolResult(text="error: missing required field 'key'")
            if not content:
                return ToolResult(text="error: missing required field 'content'")
            await self._store.write(key, content, category, tags)
            return ToolResult(text=f"Wrote memory: {key}")

        if action == "read":
            key = args.get("key", "")
            if not key:
                return ToolResult(text="error: missing required field 'key'")
            entry = await self._store.read(key)
            if not entry:
                return ToolResult(text=f"Memory not found: {key}")
            meta = json.dumps({"category": entry.category, "tags": entry.tags, "updated": entry.updated})
            return ToolResult(text=entry.content, metadata={"meta": meta})

        if action == "search":
            query = args.get("query", "")
            limit = args.get("limit", 10)
            if not query:
                return ToolResult(text="error: missing required field 'query'")
            hits = await self._store.search(query, limit)
            results = [
                {"key": h.key, "category": h.category, "snippet": h.snippet, "score": h.score}
                for h in hits
            ]
            return ToolResult(text=json.dumps(results, indent=2), metadata={"count": len(results)})

        if action == "list":
            category = args.get("category")
            limit = args.get("limit", 50)
            entries = await self._store.list_entries(category, limit)
            results = [
                {"key": e.key, "category": e.category, "tags": e.tags, "updated": e.updated}
                for e in entries
            ]
            return ToolResult(text=json.dumps(results, indent=2), metadata={"count": len(results)})

        if action == "delete":
            key = args.get("key", "")
            if not key:
                return ToolResult(text="error: missing required field 'key'")
            deleted = await self._store.delete(key)
            if deleted:
                return ToolResult(text=f"Deleted memory: {key}")
            return ToolResult(text=f"Memory not found: {key}")

        return ToolResult(text=f"Unknown memory action: {action}")
