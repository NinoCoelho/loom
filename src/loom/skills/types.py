from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from loom.types import ToolSpec


class SkillMetadata(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str


class Skill(BaseModel):
    name: str
    description: str
    body: str
    source_dir: str
    trust: str = "user"
    metadata: dict = {}


class SkillGuardVerdict(BaseModel):
    level: str
    findings: list[str]


MANAGE_TOOL_SPEC = ToolSpec(
    name="skill_manage",
    description="Create, edit, patch, delete skills, or manage files within a skill directory.",
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "create",
                    "edit",
                    "patch",
                    "delete",
                    "write_file",
                    "remove_file",
                ],
                "description": "The action to perform.",
            },
            "name": {
                "type": "string",
                "description": "Skill name.",
            },
            "description": {
                "type": "string",
                "description": "Skill description (for create).",
            },
            "body": {
                "type": "string",
                "description": "Full body content (for create/edit) or target string (for patch).",
            },
            "file_path": {
                "type": "string",
                "description": (
                    "Relative file path within the skill directory "
                    "(for write_file/remove_file)."
                ),
            },
            "content": {
                "type": "string",
                "description": "File content (for write_file) or replacement string (for patch).",
            },
        },
        "required": ["action", "name"],
    },
)

ACTIVATE_TOOL_SPEC = ToolSpec(
    name="activate_skill",
    description="Activate a skill by name so it is available for use.",
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The name of the skill to activate.",
            },
        },
        "required": ["name"],
    },
)

LIST_TOOL_SPEC = ToolSpec(
    name="list_skills",
    description="List all available skills.",
    parameters={
        "type": "object",
        "properties": {},
    },
)
