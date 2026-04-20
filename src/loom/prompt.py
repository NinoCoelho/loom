from __future__ import annotations

from typing import Any


def build_system_prompt(
    preamble: str = "",
    context: dict[str, Any] | None = None,
    skill_descriptions: list[tuple[str, str]] | None = None,
    pending_question: str | None = None,
) -> str:
    parts: list[str] = []
    if preamble:
        parts.append(preamble)
    if context:
        ctx_lines = [f"- {k}: {v}" for k, v in context.items()]
        parts.append("## Context\n" + "\n".join(ctx_lines))
    if skill_descriptions:
        lines = [f"- **{name}** -- {desc}" for name, desc in skill_descriptions]
        parts.append("## Available Skills\n" + "\n".join(lines))
    if pending_question:
        parts.append(f"## Pending Question\n{pending_question}")
    return "\n\n".join(parts)
