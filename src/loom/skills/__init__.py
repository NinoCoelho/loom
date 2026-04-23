"""Skills system — loading, registration, management, and safety guard.

A **skill** is a self-contained prompt fragment loaded from disk at startup.
Skills are exposed to the LLM as tool calls (``activate_skill``, ``list_skills``,
``manage_skill``) via :class:`SkillRegistry` and executed by :class:`SkillManager`.

Key types:

* :class:`Skill` — the skill descriptor (name, body, trust level, metadata).
* :class:`SkillMetadata` — source location and loaded timestamp.
* :class:`SkillGuardVerdict` — result of inspecting skill content for unsafe
  patterns (``safe``, ``caution``, ``dangerous``).
* :class:`SkillGuard` — the inspector that produces verdicts.

:class:`SkillRegistry` discovers skills by scanning directories;
:class:`SkillManager` provides CRUD operations (create, edit, delete, patch)
with rollback on failure and optional guard enforcement.
"""

from loom.skills.guard import SkillGuard
from loom.skills.loader import load_skill
from loom.skills.manager import SkillManager
from loom.skills.registry import SkillRegistry
from loom.skills.types import (
    ACTIVATE_TOOL_SPEC,
    LIST_TOOL_SPEC,
    MANAGE_TOOL_SPEC,
    Skill,
    SkillGuardVerdict,
    SkillMetadata,
)

__all__ = [
    "Skill",
    "SkillMetadata",
    "SkillGuardVerdict",
    "MANAGE_TOOL_SPEC",
    "ACTIVATE_TOOL_SPEC",
    "LIST_TOOL_SPEC",
    "SkillGuard",
    "SkillRegistry",
    "SkillManager",
    "load_skill",
]
