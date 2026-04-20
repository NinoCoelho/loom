from __future__ import annotations

import json
from pathlib import Path

from loom.store.atomic import atomic_write


class SecretsStore:
    def __init__(self, secrets_path: Path) -> None:
        self._path = secrets_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write({})
        self._path.chmod(0o600)

    def _read(self) -> dict[str, str]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, str]) -> None:
        atomic_write(self._path, json.dumps(data, indent=2))

    def get(self, key: str) -> str | None:
        return self._read().get(key)

    def set(self, key: str, value: str) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def delete(self, key: str) -> bool:
        data = self._read()
        if key in data:
            del data[key]
            self._write(data)
            return True
        return False

    def list_keys(self) -> list[str]:
        return list(self._read().keys())
