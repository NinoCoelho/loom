from __future__ import annotations

from pathlib import Path

from loom.skills.loader import load_skill
from loom.skills.types import Skill


class SkillRegistry:
    def __init__(
        self,
        skills_dir: Path,
        additional_dirs: list[Path] | None = None,
    ) -> None:
        self._skills_dir = skills_dir
        self._additional_dirs = additional_dirs or []
        self._index: dict[str, Skill] = {}

    def scan(self) -> None:
        self._index.clear()
        for d in self._additional_dirs:
            self._scan_dir(d)
        self._scan_dir(self._skills_dir)

    def _scan_dir(self, root: Path) -> None:
        if not root.exists():
            return
        for skill_md in sorted(root.rglob("SKILL.md")):
            skill_dir = skill_md.parent
            try:
                skill = load_skill(skill_dir)
                if skill.name not in self._index:
                    self._index[skill.name] = skill
            except Exception:
                continue

    def descriptions(self) -> list[tuple[str, str]]:
        return [(s.name, s.description) for s in sorted(self._index.values(), key=lambda s: s.name)]

    def get(self, name: str) -> Skill | None:
        return self._index.get(name)

    def list(self) -> list[Skill]:
        return sorted(self._index.values(), key=lambda s: s.name)

    def register(self, skill: Skill) -> None:
        self._index[skill.name] = skill

    def unregister(self, name: str) -> None:
        self._index.pop(name, None)

    def reload(self) -> None:
        self._index.clear()
        self.scan()

    @property
    def skills_dir(self) -> Path:
        return self._skills_dir

    @property
    def additional_dirs(self) -> list[Path]:
        return list(self._additional_dirs)
