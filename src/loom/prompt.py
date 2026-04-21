from __future__ import annotations

from pathlib import Path
from typing import Any

from loom.home import AgentHome


class PromptSection:
    __slots__ = ("name", "content", "priority", "source", "writable")

    def __init__(
        self,
        name: str,
        content: str,
        priority: int = 50,
        source: Path | None = None,
        writable: bool = False,
    ) -> None:
        self.name = name
        self.content = content
        self.priority = priority
        self.source = source
        self.writable = writable

    def __repr__(self) -> str:
        return f"PromptSection({self.name!r}, prio={self.priority}, writable={self.writable})"


class PromptBuilder:
    def __init__(self) -> None:
        self._sections: dict[str, PromptSection] = {}

    def add(self, section: PromptSection) -> None:
        self._sections[section.name] = section

    def remove(self, name: str) -> None:
        self._sections.pop(name, None)

    def update(self, name: str, content: str) -> None:
        if name in self._sections:
            self._sections[name] = PromptSection(
                name=name,
                content=content,
                priority=self._sections[name].priority,
                source=self._sections[name].source,
                writable=self._sections[name].writable,
            )

    def get(self, name: str) -> PromptSection | None:
        return self._sections.get(name)

    def build(self) -> str:
        ordered = sorted(self._sections.values(), key=lambda s: s.priority)
        parts: list[str] = []
        for s in ordered:
            if s.content.strip():
                parts.append(s.content.strip())
        return "\n\n".join(parts)

    def list_sections(self) -> list[PromptSection]:
        return sorted(self._sections.values(), key=lambda s: s.priority)


def load_identity_sections(
    home: AgentHome,
    permissions: Any | None = None,
) -> list[PromptSection]:
    sections: list[PromptSection] = []

    perms = permissions
    soul_w = perms.soul_writable if perms else False
    identity_w = perms.identity_writable if perms else False
    user_w = perms.user_writable if perms else True

    home.initialize()

    soul = home.read_soul()
    if soul:
        sections.append(
            PromptSection(
                name="soul",
                content=soul,
                priority=10,
                source=home.soul_path,
                writable=soul_w,
            )
        )

    identity = home.read_identity()
    if identity:
        sections.append(
            PromptSection(
                name="identity",
                content=identity,
                priority=20,
                source=home.identity_path,
                writable=identity_w,
            )
        )

    user = home.read_user()
    if user:
        sections.append(
            PromptSection(
                name="user",
                content=user,
                priority=30,
                source=home.user_path,
                writable=user_w,
            )
        )

    return sections


def load_memory_preview(
    recent_memories: list[tuple[str, str]],
    budget: int = 1500,
) -> PromptSection | None:
    if not recent_memories:
        return None

    parts: list[str] = []
    total = 0
    for key, preview in recent_memories:
        chunk = f"### {key}\n{preview[:300]}"
        if total + len(chunk) > budget:
            break
        parts.append(chunk)
        total += len(chunk)

    if not parts:
        return None

    content = "## Recent Memory\n\n" + "\n\n".join(parts)
    return PromptSection(name="memory", content=content, priority=35)


def load_skills_section(
    descriptions: list[tuple[str, str]],
) -> PromptSection | None:
    if not descriptions:
        return None

    lines = [f"- **{name}** -- {desc}" for name, desc in descriptions]
    content = "## Available Skills\n\n" + "\n".join(lines)
    return PromptSection(name="skills", content=content, priority=40)


def load_context_section(context: dict[str, Any] | None) -> PromptSection | None:
    if not context:
        return None
    lines = [f"- {k}: {v}" for k, v in context.items()]
    return PromptSection(name="context", content="## Context\n\n" + "\n".join(lines), priority=50)


def load_pending_section(pending_question: str | None) -> PromptSection | None:
    if not pending_question:
        return None
    return PromptSection(
        name="pending",
        content=f"## Pending Question\n{pending_question}",
        priority=60,
    )
