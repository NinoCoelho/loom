"""Config data models and on-disk persistence.

* :class:`ProviderConfig` — per-provider settings (base URL, API key, model).
* :class:`LoomConfig` — top-level config with a default model and provider map.
* :class:`ConfigStore` — reads/writes :class:`LoomConfig` as JSON to disk,
  with atomic writes and graceful fallback on corruption.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel


class ProviderConfig(BaseModel):
    base_url: str = ""
    api_key_env: str = ""
    api_key_inline: str = ""
    provider_type: str = "openai_compat"
    default_model: str = ""


class LoomConfig(BaseModel):
    default_model: str = ""
    max_iterations: int = 32
    system_preamble: str = ""
    providers: dict[str, ProviderConfig] = {}
    models: list[dict] = []


class ConfigStore:
    def __init__(self, config_path: Path) -> None:
        self._path = config_path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> LoomConfig:
        if not self._path.exists():
            return LoomConfig()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return LoomConfig(**data)
        except Exception:
            return LoomConfig()

    def save(self, config: LoomConfig) -> None:
        from loom.store.atomic import atomic_write

        atomic_write(self._path, config.model_dump_json(indent=2))

    def update(self, **kwargs: object) -> LoomConfig:
        config = self.load()
        for k, v in kwargs.items():
            if hasattr(config, k):
                setattr(config, k, v)
        self.save(config)
        return config
