from __future__ import annotations

import json
from pathlib import Path

from loom.store.atomic import atomic_write
from loom.types import ToolSpec
from loom.tools.base import ToolHandler, ToolResult


class MemoryToolHandler(ToolHandler):
    def __init__(self, memory_dir: Path) -> None:
        self._dir = memory_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="memory",
            description="Read, write, and list key-value markdown memory entries.",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write", "list"],
                        "description": "Action to perform",
                    },
                    "key": {
                        "type": "string",
                        "description": "Memory key",
                    },
                    "content": {
                        "type": "string",
                        "description": "Markdown content (for write)",
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
            atomic_write(self._dir / f"{key}.md", content)
            return ToolResult(text=f"Wrote memory: {key}")

        if action == "read":
            key = args.get("key", "")
            path = self._dir / f"{key}.md"
            if not path.exists():
                return ToolResult(text=f"Memory not found: {key}")
            return ToolResult(text=path.read_text())

        if action == "list":
            keys = sorted(p.stem for p in self._dir.glob("*.md"))
            return ToolResult(
                text=json.dumps(keys), metadata={"count": len(keys)}
            )

        return ToolResult(text=f"Unknown memory action: {action}")
