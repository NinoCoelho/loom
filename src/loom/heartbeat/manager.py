from __future__ import annotations

import shutil
from pathlib import Path

import frontmatter

from loom.heartbeat.cron import parse_schedule
from loom.heartbeat.loader import load_heartbeat
from loom.heartbeat.registry import HeartbeatRegistry
from loom.heartbeat.store import HeartbeatStore
from loom.heartbeat.types import validate_heartbeat_id
from loom.store.atomic import atomic_write


class HeartbeatManager:
    """CRUD operations for heartbeats on disk + registry sync."""

    def __init__(self, registry: HeartbeatRegistry, store: HeartbeatStore) -> None:
        self._registry = registry
        self._store = store

    def invoke(self, args: dict) -> str:
        action = args.get("action", "")
        handler = {
            "create": self._create,
            "delete": self._delete,
            "enable": self._enable,
            "disable": self._disable,
            "list": self._list,
        }.get(action)
        if handler is None:
            return f"error: unknown action {action!r}"
        return handler(args)

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _create(self, args: dict) -> str:
        name: str = args.get("name", "")
        description: str = args.get("description", "")
        schedule: str = args.get("schedule", "")
        instructions: str = args.get("instructions", "")
        driver_code: str = args.get("driver_code", "")

        if not name:
            return "error: 'name' is required"
        if not description:
            return "error: 'description' is required"
        if not schedule:
            return "error: 'schedule' is required"
        if not driver_code:
            return "error: 'driver_code' is required"

        try:
            validate_heartbeat_id(name)
        except ValueError as e:
            return f"error: {e}"

        try:
            parse_schedule(schedule)
        except ValueError as e:
            return f"error: invalid schedule — {e}"

        hb_dir = self._registry.heartbeats_dir / name
        hb_md = hb_dir / "HEARTBEAT.md"
        driver_py = hb_dir / "driver.py"

        # Build HEARTBEAT.md content
        post = frontmatter.Post(instructions)
        post.metadata["name"] = name
        post.metadata["description"] = description
        post.metadata["schedule"] = schedule
        post.metadata["enabled"] = True
        hb_content = frontmatter.dumps(post)

        backup_md: str | None = hb_md.read_text("utf-8") if hb_md.exists() else None
        backup_py: str | None = driver_py.read_text("utf-8") if driver_py.exists() else None

        try:
            atomic_write(hb_md, hb_content)
            atomic_write(driver_py, driver_code)
            record = load_heartbeat(hb_dir)
            self._registry.register(record)
        except Exception as exc:
            # Roll back on failure
            if backup_md is not None:
                atomic_write(hb_md, backup_md)
            elif hb_md.exists():
                hb_md.unlink(missing_ok=True)
            if backup_py is not None:
                atomic_write(driver_py, backup_py)
            elif driver_py.exists():
                driver_py.unlink(missing_ok=True)
            return f"error: failed to create heartbeat {name!r} — {exc}"

        return f"created heartbeat {name!r}"

    def _delete(self, args: dict) -> str:
        name: str = args.get("name", "")
        if not name:
            return "error: 'name' is required"

        hb_dir = self._registry.heartbeats_dir / name
        if not hb_dir.exists():
            return f"error: heartbeat {name!r} not found"

        try:
            shutil.rmtree(hb_dir)
        except OSError as exc:
            return f"error: failed to delete heartbeat {name!r} — {exc}"

        self._registry.unregister(name)
        self._store.delete_all(name)
        return f"deleted heartbeat {name!r}"

    def _enable(self, args: dict) -> str:
        return self._set_enabled(args, True)

    def _disable(self, args: dict) -> str:
        return self._set_enabled(args, False)

    def _set_enabled(self, args: dict, enabled: bool) -> str:
        name: str = args.get("name", "")
        if not name:
            return "error: 'name' is required"

        record = self._registry.get(name)
        if record is None:
            return f"error: heartbeat {name!r} not found"

        hb_md = record.source_dir / "HEARTBEAT.md"
        raw = hb_md.read_text("utf-8")
        post = frontmatter.loads(raw)
        post.metadata["enabled"] = enabled
        atomic_write(hb_md, frontmatter.dumps(post))

        # Reload into registry to reflect the change
        try:
            updated = load_heartbeat(record.source_dir)
            self._registry.register(updated)
        except Exception as exc:
            return f"error: updated file but failed to reload — {exc}"

        state = "enabled" if enabled else "disabled"
        return f"{state} heartbeat {name!r}"

    def _list(self, _args: dict) -> str:
        records = self._registry.list()
        if not records:
            return "no heartbeats registered"
        lines: list[str] = []
        for r in records:
            runs = [
                run for run in self._store.list_runs() if run.heartbeat_id == r.id
            ]
            last_check = runs[0].last_check.isoformat() if runs and runs[0].last_check else "never"
            last_fired = runs[0].last_fired.isoformat() if runs and runs[0].last_fired else "never"
            status = "enabled" if r.enabled else "disabled"
            lines.append(
                f"- {r.id} [{status}] schedule={r.schedule!r} "
                f"last_check={last_check} last_fired={last_fired}"
            )
        return "\n".join(lines)
