"""Trusted binary registry — hash-verified allowlist for executables.

When an agent requests execution of an external binary (e.g. via the
terminal tool or a download tool), the registry checks that the binary's
SHA-256 hash matches a previously approved entry. New binaries are added
only through explicit ``trust()`` calls, never automatically.

The registry is persisted as a JSON file in the agent home directory.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from loom.store.atomic import atomic_write

logger = logging.getLogger(__name__)


@dataclass
class BinaryEntry:
    name: str
    path: str
    sha256: str
    trusted_at: str = ""


class BinaryRegistry:
    """Hash-based trusted binary allowlist.

    Usage::

        reg = BinaryRegistry(home / "trusted_binaries.json")
        reg.load()

        # Trust a binary (calculates hash automatically)
        entry = reg.trust("/usr/local/bin/ffmpeg")

        # Check before execution
        if reg.is_trusted("/usr/local/bin/ffmpeg"):
            ...
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._entries: dict[str, BinaryEntry] = {}

    @property
    def store_path(self) -> Path:
        return self._path

    def load(self) -> None:
        if not self._path.exists():
            self._entries = {}
            return
        try:
            raw = self._path.read_text()
            data = json.loads(raw)
            self._entries = {}
            for item in data.get("binaries", []):
                entry = BinaryEntry(
                    name=item.get("name", ""),
                    path=item.get("path", ""),
                    sha256=item.get("sha256", ""),
                    trusted_at=item.get("trusted_at", ""),
                )
                if entry.path and entry.sha256:
                    self._entries[entry.path] = entry
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load binary registry: %s", exc)
            self._entries = {}

    def save(self) -> None:
        binaries = [
            {
                "name": e.name,
                "path": e.path,
                "sha256": e.sha256,
                "trusted_at": e.trusted_at,
            }
            for e in self._entries.values()
        ]
        atomic_write(self._path, json.dumps({"binaries": binaries}, indent=2))

    @staticmethod
    def hash_file(path: str | Path) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def trust(self, binary_path: str | Path, name: str | None = None) -> BinaryEntry:
        p = Path(binary_path).resolve()
        sha256 = self.hash_file(p)
        from datetime import UTC, datetime

        entry = BinaryEntry(
            name=name or p.name,
            path=str(p),
            sha256=sha256,
            trusted_at=datetime.now(UTC).isoformat(),
        )
        self._entries[str(p)] = entry
        self.save()
        logger.info("Trusted binary: %s (sha256=%s…)", p, sha256[:16])
        return entry

    def untrust(self, binary_path: str | Path) -> bool:
        p = str(Path(binary_path).resolve())
        if p not in self._entries:
            return False
        del self._entries[p]
        self.save()
        return True

    def is_trusted(self, binary_path: str | Path) -> bool:
        p = Path(binary_path).resolve()
        if not p.exists():
            return False
        entry = self._entries.get(str(p))
        if entry is None:
            return False
        try:
            current_hash = self.hash_file(p)
            return current_hash == entry.sha256
        except OSError:
            return False

    def get_entry(self, binary_path: str | Path) -> BinaryEntry | None:
        return self._entries.get(str(Path(binary_path).resolve()))

    def list_trusted(self) -> list[BinaryEntry]:
        return list(self._entries.values())

    def check_or_error(self, binary_path: str | Path) -> tuple[bool, str]:
        p = Path(binary_path).resolve()
        if not p.exists():
            return False, f"Binary not found: {p}"
        entry = self._entries.get(str(p))
        if entry is None:
            return False, f"Binary not in trusted registry: {p}"
        try:
            current_hash = self.hash_file(p)
        except OSError as exc:
            return False, f"Cannot hash binary: {exc}"
        if current_hash != entry.sha256:
            return False, (
                f"Binary hash mismatch for {p}: "
                f"expected {entry.sha256[:16]}… got {current_hash[:16]}…. "
                f"The file may have been modified or replaced."
            )
        return True, ""


__all__ = ["BinaryRegistry", "BinaryEntry"]
