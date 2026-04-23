"""Heartbeat file loader — frontmatter parsing and driver construction.

:func:`load_heartbeat` reads a ``*.md`` heartbeat file, parses YAML
frontmatter via :mod:`frontmatter`, and dynamically imports the Python
module referenced in the ``run`` key. Returns a
:class:`~loom.heartbeat.types.HeartbeatRecord` with a callable
:attr:`~loom.heartbeat.types.HeartbeatRecord.driver`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import frontmatter

from loom.heartbeat.types import HeartbeatDriver, HeartbeatRecord, validate_heartbeat_id


def load_heartbeat(heartbeat_dir: Path) -> HeartbeatRecord:
    """Load a heartbeat from a directory containing HEARTBEAT.md and driver.py."""
    hb_md = heartbeat_dir / "HEARTBEAT.md"
    driver_py = heartbeat_dir / "driver.py"

    if not hb_md.exists():
        raise FileNotFoundError(f"HEARTBEAT.md not found in {heartbeat_dir}")
    if not driver_py.exists():
        raise FileNotFoundError(f"driver.py not found in {heartbeat_dir}")

    raw = hb_md.read_text(encoding="utf-8")
    post = frontmatter.loads(raw)

    name: str = post.metadata.get("name", "")
    description: str = post.metadata.get("description", "")
    schedule: str = post.metadata.get("schedule", "")
    enabled: bool = bool(post.metadata.get("enabled", True))
    instructions: str = post.content

    dir_name = heartbeat_dir.name
    validate_heartbeat_id(dir_name)

    if name != dir_name:
        raise ValueError(f"heartbeat name {name!r} does not match directory name {dir_name!r}")
    if not description:
        raise ValueError("description is required in HEARTBEAT.md frontmatter")
    if not schedule:
        raise ValueError("schedule is required in HEARTBEAT.md frontmatter")

    driver = _load_driver(driver_py)

    return HeartbeatRecord(
        id=dir_name,
        name=name,
        description=description,
        schedule=schedule,
        enabled=enabled,
        instructions=instructions,
        source_dir=heartbeat_dir,
        driver=driver,
    )


def _load_driver(driver_path: Path) -> HeartbeatDriver:
    module_name = f"_loom_heartbeat_driver_{driver_path.parent.name}"
    # Remove stale cached module so reloads pick up file changes.
    sys.modules.pop(module_name, None)

    spec = importlib.util.spec_from_file_location(module_name, driver_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load driver from {driver_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]

    driver_cls = getattr(module, "Driver", None)
    if driver_cls is None:
        raise AttributeError("driver.py must define a class named 'Driver'")
    if not (isinstance(driver_cls, type) and issubclass(driver_cls, HeartbeatDriver)):
        raise TypeError("Driver must subclass HeartbeatDriver")

    return driver_cls()
