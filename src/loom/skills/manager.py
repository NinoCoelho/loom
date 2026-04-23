"""Skill CRUD controller with optional rollback and safety guard.

:class:`SkillManager` orchestrates skill create/edit/patch/delete
operations, each following the same pattern: read skill, write backup,
atomically update, reload, and re-register. If the write fails the backup
is restored. An optional :class:`~loom.skills.guard.SkillGuard` blocks
dangerous content from being saved.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import frontmatter

from loom.skills.guard import SkillGuard
from loom.skills.loader import load_skill
from loom.skills.registry import SkillRegistry
from loom.store.atomic import atomic_write


class SkillManager:
    def __init__(self, registry: SkillRegistry, guard: SkillGuard) -> None:
        self._registry = registry
        self._guard = guard

    def invoke(self, args: dict) -> str:
        action = args.get("action", "")
        name = args.get("name", "")
        handler = {
            "create": self._create,
            "edit": self._edit,
            "patch": self._patch,
            "delete": self._delete,
            "write_file": self._write_file,
            "remove_file": self._remove_file,
        }.get(action)

        if handler is None:
            return f"error: unknown action {action!r}"
        if not name and action != "list":
            return "error: missing required field 'name'"

        return handler(args)

    def _skill_dir(self, name: str) -> Path:
        return self._registry._skills_dir / name

    def _resolve(self, name: str, file_path: str | None) -> Path:
        base = self._skill_dir(name).resolve()
        if file_path:
            target = (base / file_path).resolve()
        else:
            target = base
        if not str(target).startswith(str(base)):
            raise ValueError("path traversal outside skill directory")
        return target

    def _scan_content(self, content: str, filename: str = "") -> str | None:
        verdict = self._guard.scan(content, filename)
        if verdict.level == "dangerous":
            lines = "\n".join(f"  - {f}" for f in verdict.findings)
            return f"blocked: dangerous content detected:\n{lines}"
        return None

    def _build_skill_md(self, name: str, description: str, body: str) -> str:
        post = frontmatter.Post(body)
        post.metadata["name"] = name
        post.metadata["description"] = description
        return frontmatter.dumps(post)

    def _create(self, args: dict) -> str:
        name: str = args["name"]
        description: str = args.get("description", "")
        body: str = args.get("body", "")

        if not description:
            return "error: missing required field 'description'"

        skill_dir = self._skill_dir(name)
        skill_md = skill_dir / "SKILL.md"
        content = self._build_skill_md(name, description, body)

        block = self._scan_content(content, "SKILL.md")
        if block:
            return block

        backup: str | None = None
        if skill_md.exists():
            backup = skill_md.read_text(encoding="utf-8")

        try:
            atomic_write(skill_md, content)
            skill = load_skill(skill_dir)
            self._registry.register(skill)
        except Exception:
            if backup is not None:
                atomic_write(skill_md, backup)
            elif skill_md.exists():
                skill_md.unlink()
            return f"error: failed to create skill {name!r}"

        return f"created skill {name!r}"

    def _edit(self, args: dict) -> str:
        name: str = args["name"]
        body: str = args.get("body", "")

        skill_dir = self._skill_dir(name)
        skill_md = skill_dir / "SKILL.md"

        existing = self._registry.get(name)
        if not existing:
            return f"error: skill {name!r} not found"

        content = self._build_skill_md(name, existing.description, body)
        block = self._scan_content(content, "SKILL.md")
        if block:
            return block

        backup = skill_md.read_text(encoding="utf-8")

        try:
            atomic_write(skill_md, content)
            skill = load_skill(skill_dir)
            self._registry.register(skill)
        except Exception:
            atomic_write(skill_md, backup)
            return f"error: failed to edit skill {name!r}"

        return f"edited skill {name!r}"

    def _patch(self, args: dict) -> str:
        name: str = args["name"]
        target: str = args.get("body", "")
        replacement: str = args.get("content", "")

        skill_dir = self._skill_dir(name)
        skill_md = skill_dir / "SKILL.md"

        existing = self._registry.get(name)
        if not existing:
            return f"error: skill {name!r} not found"

        current_body = existing.body
        if target not in current_body:
            return f"error: target string not found in skill {name!r}"

        new_body = current_body.replace(target, replacement, 1)
        content = self._build_skill_md(name, existing.description, new_body)

        block = self._scan_content(content, "SKILL.md")
        if block:
            return block

        backup = skill_md.read_text(encoding="utf-8")

        try:
            atomic_write(skill_md, content)
            skill = load_skill(skill_dir)
            self._registry.register(skill)
        except Exception:
            atomic_write(skill_md, backup)
            return f"error: failed to patch skill {name!r}"

        return f"patched skill {name!r}"

    def _delete(self, args: dict) -> str:
        name: str = args["name"]
        skill_dir = self._skill_dir(name)

        if not skill_dir.exists():
            return f"error: skill {name!r} not found"

        try:
            shutil.rmtree(skill_dir)
        except OSError as exc:
            return f"error: failed to delete skill {name!r}: {exc}"

        self._registry.unregister(name)
        return f"deleted skill {name!r}"

    def _write_file(self, args: dict) -> str:
        name: str = args["name"]
        file_path: str | None = args.get("file_path")
        content: str = args.get("content", "")

        if not file_path:
            return "error: missing required field 'file_path'"

        try:
            target = self._resolve(name, file_path)
        except ValueError:
            return "error: path traversal outside skill directory"

        block = self._scan_content(content, file_path)
        if block:
            return block

        backup: str | None = None
        if target.exists():
            backup = target.read_text(encoding="utf-8")

        try:
            atomic_write(target, content)
        except Exception:
            if backup is not None:
                atomic_write(target, backup)
            return f"error: failed to write {file_path!r}"

        return f"wrote {file_path!r} in skill {name!r}"

    def _remove_file(self, args: dict) -> str:
        name: str = args["name"]
        file_path: str | None = args.get("file_path")

        if not file_path:
            return "error: missing required field 'file_path'"

        try:
            target = self._resolve(name, file_path)
        except ValueError:
            return "error: path traversal outside skill directory"

        if not target.exists():
            return f"error: {file_path!r} not found"

        skill_md = self._skill_dir(name) / "SKILL.md"
        if target.resolve() == skill_md.resolve():
            return "error: cannot remove SKILL.md; use delete action instead"

        try:
            target.unlink()
        except OSError as exc:
            return f"error: failed to remove {file_path!r}: {exc}"

        return f"removed {file_path!r} from skill {name!r}"
