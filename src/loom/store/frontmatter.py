"""YAML frontmatter parsing and manipulation for markdown files.

Used by :mod:`loom.store.memory`, :mod:`loom.store.vault`, and
:mod:`loom.store.keychain` — any component that stores structured data
in markdown files with ``---``-delimited YAML headers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from loom.store.atomic import atomic_write


def parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split a markdown string into ``(frontmatter_dict, body_text)``.

    Returns ``({}, raw)`` if no valid ``---``-delimited block is found.
    """
    fm: dict[str, Any] = {}
    body = raw
    if raw.startswith("---"):
        end = raw.find("---", 3)
        if end != -1:
            try:
                fm = yaml.safe_load(raw[3:end]) or {}
            except Exception:
                pass
            body = raw[end + 3 :].strip()
    return fm, body


def build_frontmatter(fm: dict[str, Any], body: str) -> str:
    """Render a complete markdown string with YAML frontmatter header."""
    fm_str = yaml.dump(fm, default_flow_style=False).strip()
    return f"---\n{fm_str}\n---\n{body}"


def rewrite_frontmatter(path: Path, updates: dict[str, Any]) -> None:
    """Merge *updates* into the YAML frontmatter of the file at *path*.

    Reads the file, parses existing frontmatter, merges ``updates`` into it,
    and writes back atomically. No-op if the file does not exist.
    """
    if not path.exists():
        return
    raw = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(raw)
    fm.update(updates)
    atomic_write(path, build_frontmatter(fm, body))
