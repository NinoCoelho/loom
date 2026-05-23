"""Backup and restore tool for agent home directories.

Provides a :class:`BackupTool` that exports all agent data (SOUL.md,
IDENTITY.md, USER.md, skills, config, sessions, memory, and vault files)
into a versioned JSON archive, and restores from such an archive.

Only keys defined in the backup registry are exported/restored — no
arbitrary data injection.

Usage::

    tool = BackupTool(agent_home, session_store, memory_store)
    archive = await tool.invoke({"action": "export"})
    restore_result = await tool.invoke({"action": "import", "data": archive})
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loom.store.atomic import atomic_write
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec

logger = logging.getLogger(__name__)

BACKUP_VERSION = 1

_BACKUP_TEXT_FILES = [
    ("soul", "SOUL.md"),
    ("identity", "IDENTITY.md"),
    ("user", "USER.md"),
]

_BACKUP_DIRECTORIES = [
    ("skills", "skills"),
    ("vault", "vault"),
    ("memory", "memory"),
]

_BACKUP_TOOL_SPEC = ToolSpec(
    name="backup",
    description=(
        "Export or import a complete backup of the agent's home directory, "
        "including personality files (SOUL.md, IDENTITY.md, USER.md), skills, "
        "vault, memory, and session history."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["export", "import"],
                "description": "'export' creates a backup, 'import' restores from one.",
            },
            "data": {
                "type": "object",
                "description": "Backup data object (required for 'import' action).",
            },
        },
        "required": ["action"],
    },
)


class BackupTool(ToolHandler):
    """Agent home backup and restore tool.

    Parameters
    ----------
    agent_home : Path
        Root path of the agent home directory.
    session_store : SessionStore, optional
        Session store to include in backups.
    memory_store : MemoryStore, optional
        Memory store to include in backups.
    """

    def __init__(
        self,
        agent_home: Path,
        session_store: Any | None = None,
        memory_store: Any | None = None,
    ) -> None:
        self._home = agent_home
        self._session_store = session_store
        self._memory_store = memory_store

    @property
    def tool(self) -> ToolSpec:
        return _BACKUP_TOOL_SPEC

    async def invoke(self, args: dict[str, Any]) -> ToolResult:
        action = args.get("action")
        if action == "export":
            return await self._export()
        if action == "import":
            data = args.get("data")
            if not isinstance(data, dict):
                return ToolResult(text="`data` is required for import", is_error=True)
            return await self._restore(data)
        return ToolResult(text=f"Unknown action: {action}", is_error=True)

    async def _export(self) -> ToolResult:
        backup: dict[str, Any] = {
            "version": BACKUP_VERSION,
            "agent": self._home.name,
            "exported_at": datetime.now(UTC).isoformat(),
        }

        for key, filename in _BACKUP_TEXT_FILES:
            path = self._home / filename
            if path.exists():
                try:
                    backup[key] = path.read_text()
                except OSError as exc:
                    logger.warning("Failed to read %s: %s", path, exc)

        for key, dirname in _BACKUP_DIRECTORIES:
            dir_path = self._home / dirname
            if dir_path.is_dir():
                files: dict[str, str] = {}
                for fp in sorted(dir_path.rglob("*")):
                    if fp.is_file():
                        try:
                            rel = str(fp.relative_to(dir_path))
                            content = fp.read_text(encoding="utf-8", errors="replace")
                            files[rel] = content
                        except OSError:
                            pass
                if files:
                    backup[key] = files

        config_path = self._home / "config.json"
        if config_path.exists():
            try:
                backup["config"] = json.loads(config_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        if self._session_store is not None:
            try:
                backup["sessions"] = self._session_store.export_all()
            except Exception as exc:
                logger.warning("Failed to export sessions: %s", exc)

        if self._memory_store is not None:
            try:
                entries = self._memory_store.list_all()
                backup["memory_entries"] = [
                    {"text": e.text, "metadata": e.metadata, "created_at": e.created_at}
                    for e in entries
                ]
            except Exception as exc:
                logger.warning("Failed to export memory: %s", exc)

        return ToolResult(
            text=json.dumps(backup, ensure_ascii=False),
            metadata={"version": BACKUP_VERSION, "agent": self._home.name},
        )

    async def _restore(self, data: dict[str, Any]) -> ToolResult:
        version = data.get("version", 0)
        if version > BACKUP_VERSION:
            return ToolResult(
                text=f"Backup version {version} is newer than supported {BACKUP_VERSION}",
                is_error=True,
            )

        restored: list[str] = []

        for key, filename in _BACKUP_TEXT_FILES:
            content = data.get(key)
            if isinstance(content, str):
                path = self._home / filename
                atomic_write(path, content)
                restored.append(filename)

        for key, dirname in _BACKUP_DIRECTORIES:
            files = data.get(key)
            if isinstance(files, dict):
                dir_path = self._home / dirname
                dir_path.mkdir(parents=True, exist_ok=True)
                count = 0
                for rel, content in files.items():
                    safe_rel = Path(rel)
                    if safe_rel.is_absolute() or ".." in safe_rel.parts:
                        continue
                    dest = dir_path / safe_rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    atomic_write(dest, content)
                    count += 1
                if count:
                    restored.append(f"{dirname}/ ({count} files)")

        config = data.get("config")
        if isinstance(config, dict):
            config_path = self._home / "config.json"
            atomic_write(config_path, json.dumps(config, indent=2))
            restored.append("config.json")

        sessions = data.get("sessions")
        if sessions and self._session_store is not None:
            try:
                self._session_store.import_all(sessions)
                restored.append("sessions")
            except Exception as exc:
                logger.warning("Failed to restore sessions: %s", exc)

        memory_entries = data.get("memory_entries")
        if memory_entries and self._memory_store is not None:
            try:
                for entry in memory_entries:
                    self._memory_store.store(
                        text=entry.get("text", ""),
                        metadata=entry.get("metadata"),
                    )
                restored.append(f"memory ({len(memory_entries)} entries)")
            except Exception as exc:
                logger.warning("Failed to restore memory: %s", exc)

        summary = ", ".join(restored) if restored else "nothing to restore"
        return ToolResult(text=f"Restored: {summary}")


__all__ = ["BackupTool", "BACKUP_VERSION"]
