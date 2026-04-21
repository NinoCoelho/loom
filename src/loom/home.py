from __future__ import annotations

from pathlib import Path

from loom.store.atomic import atomic_write

_DEFAULT_SOUL = """\
# Soul

You are a helpful, resourceful AI agent. You act first and explain later.
You use tools to accomplish tasks. When uncertain, you ask.
"""

_DEFAULT_IDENTITY = """\
# Identity

Name: Assistant
Role: General-purpose AI agent
Tone: Concise, direct, professional
"""

_DEFAULT_USER = """\
# User Preferences

(No preferences recorded yet. The agent will learn and update this file over time.)
"""


class AgentHome:
    def __init__(self, path: Path, name: str | None = None) -> None:
        self.path = path
        self.name = name or path.name

    @property
    def soul_path(self) -> Path:
        return self.path / "SOUL.md"

    @property
    def identity_path(self) -> Path:
        return self.path / "IDENTITY.md"

    @property
    def user_path(self) -> Path:
        return self.path / "USER.md"

    @property
    def skills_dir(self) -> Path:
        return self.path / "skills"

    @property
    def memory_dir(self) -> Path:
        return self.path / "memory"

    @property
    def vault_dir(self) -> Path:
        return self.path / "vault"

    @property
    def config_path(self) -> Path:
        return self.path / "config.json"

    @property
    def sessions_db(self) -> Path:
        return self.path / "sessions.sqlite"

    @property
    def memory_index_db(self) -> Path:
        return self.path / "_index" / "memory.sqlite"

    def initialize(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        self.skills_dir.mkdir(exist_ok=True)
        self.memory_dir.mkdir(exist_ok=True)
        self.vault_dir.mkdir(exist_ok=True)
        (self.path / "_index").mkdir(exist_ok=True)

        if not self.soul_path.exists():
            atomic_write(self.soul_path, _DEFAULT_SOUL)
        if not self.identity_path.exists():
            atomic_write(self.identity_path, _DEFAULT_IDENTITY)
        if not self.user_path.exists():
            atomic_write(self.user_path, _DEFAULT_USER)

    def validate(self) -> list[str]:
        issues: list[str] = []
        if not self.path.exists():
            issues.append(f"Agent home does not exist: {self.path}")
            return issues
        for name, path in [
            ("SOUL.md", self.soul_path),
            ("IDENTITY.md", self.identity_path),
            ("USER.md", self.user_path),
        ]:
            if not path.exists():
                issues.append(f"Missing {name}")
        for name, path in [
            ("skills", self.skills_dir),
            ("memory", self.memory_dir),
        ]:
            if not path.exists():
                issues.append(f"Missing {name}/ directory")
        return issues

    def read_soul(self) -> str:
        return self.soul_path.read_text() if self.soul_path.exists() else ""

    def read_identity(self) -> str:
        return self.identity_path.read_text() if self.identity_path.exists() else ""

    def read_user(self) -> str:
        return self.user_path.read_text() if self.user_path.exists() else ""

    def write_soul(self, content: str) -> None:
        atomic_write(self.soul_path, content)

    def write_identity(self, content: str) -> None:
        atomic_write(self.identity_path, content)

    def write_user(self, content: str) -> None:
        atomic_write(self.user_path, content)
