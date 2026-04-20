from __future__ import annotations

from loom.home import AgentHome
from loom.permissions import AgentPermissions
from loom.types import ToolSpec
from loom.tools.base import ToolHandler, ToolResult


class EditIdentityTool(ToolHandler):
    def __init__(self, home: AgentHome, permissions: AgentPermissions) -> None:
        self._home = home
        self._perms = permissions

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="edit_profile",
            description="Edit the agent's SOUL.md, IDENTITY.md, or USER.md profile files.",
            parameters={
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "enum": ["soul", "identity", "user"],
                        "description": "Which profile file to edit",
                    },
                    "content": {
                        "type": "string",
                        "description": "New content for the file (full replacement)",
                    },
                },
                "required": ["file", "content"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        file_name = args.get("file", "")
        content = args.get("content", "")

        if not file_name:
            return ToolResult(text="error: missing required field 'file'")
        if not content:
            return ToolResult(text="error: missing required field 'content'")

        if not self._perms.can_edit_file(file_name):
            return ToolResult(
                text=f"error: permission denied -- cannot edit {file_name}. "
                f"Current permissions do not allow writing to this file."
            )

        writers = {
            "soul": self._home.write_soul,
            "identity": self._home.write_identity,
            "user": self._home.write_user,
        }
        writer = writers.get(file_name)
        if not writer:
            return ToolResult(
                text=f"error: unknown file '{file_name}'. Must be one of: soul, identity, user"
            )

        try:
            writer(content)
            return ToolResult(text=f"Updated {file_name} successfully.")
        except Exception as e:
            return ToolResult(text=f"error: failed to write {file_name}: {e}")
