from __future__ import annotations

import json
import re
from pathlib import Path

import frontmatter

from loom.skills.types import Skill

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def load_skill(skill_dir: Path) -> Skill:
    skill_md = skill_dir / "SKILL.md"
    raw = skill_md.read_text(encoding="utf-8")
    post = frontmatter.loads(raw)

    name: str = post.metadata.get("name", "")
    description: str = post.metadata.get("description", "")
    body: str = post.content

    dir_name = skill_dir.name
    if not _NAME_RE.match(dir_name):
        raise ValueError(f"invalid skill directory name: {dir_name!r}")
    if name != dir_name:
        raise ValueError(
            f"skill name {name!r} does not match directory name {dir_name!r}"
        )
    if not (1 <= len(description) <= 1024):
        raise ValueError(
            f"description must be 1-1024 chars, got {len(description)}"
        )

    trust = _resolve_trust(skill_dir)
    metadata = {
        k: v
        for k, v in post.metadata.items()
        if k not in ("name", "description")
    }

    return Skill(
        name=name,
        description=description,
        body=body,
        source_dir=str(skill_dir),
        trust=trust,
        metadata=metadata,
    )


def _resolve_trust(skill_dir: Path) -> str:
    meta_path = skill_dir / "_meta.json"
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            return data.get("trust", "user")
        except (json.JSONDecodeError, OSError):
            pass
    if "seed_skills" in skill_dir.parts:
        return "builtin"
    return "user"
