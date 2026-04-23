"""Heartbeat directory scanner and index.

:class:`HeartbeatRegistry` scans a directory tree for ``*.md`` heartbeat
files, loads each via :func:`~loom.heartbeat.loader.load_heartbeat`, and
maintains a name-keyed index of :class:`~loom.heartbeat.types.HeartbeatRecord`
objects.
"""

from __future__ import annotations

from pathlib import Path

from loom.heartbeat.loader import load_heartbeat
from loom.heartbeat.types import HeartbeatRecord


class HeartbeatRegistry:
    def __init__(
        self,
        heartbeats_dir: Path,
        additional_dirs: list[Path] | None = None,
    ) -> None:
        self._heartbeats_dir = heartbeats_dir
        self._additional_dirs = additional_dirs or []
        self._index: dict[str, HeartbeatRecord] = {}

    def scan(self) -> None:
        self._index.clear()
        for d in self._additional_dirs:
            self._scan_dir(d)
        self._scan_dir(self._heartbeats_dir)

    def _scan_dir(self, root: Path) -> None:
        if not root.exists():
            return
        for hb_md in sorted(root.rglob("HEARTBEAT.md")):
            hb_dir = hb_md.parent
            try:
                record = load_heartbeat(hb_dir)
                if record.id not in self._index:
                    self._index[record.id] = record
            except Exception:
                continue

    def get(self, id: str) -> HeartbeatRecord | None:
        return self._index.get(id)

    def list(self) -> list[HeartbeatRecord]:
        return sorted(self._index.values(), key=lambda r: r.id)

    def register(self, record: HeartbeatRecord) -> None:
        self._index[record.id] = record

    def unregister(self, id: str) -> None:
        self._index.pop(id, None)

    def reload(self) -> None:
        self._index.clear()
        self.scan()

    @property
    def heartbeats_dir(self) -> Path:
        return self._heartbeats_dir
