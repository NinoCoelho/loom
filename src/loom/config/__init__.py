"""Configuration management — resolving, merging, and persisting.

:func:`resolve_config` merges a chain of config sources (file → env → args)
into a single :class:`LoomConfig`. :func:`resolve_from_env` reads from
environment variables only.

:class:`ConfigStore` persists the resolved config to disk; individual
sections (e.g. :class:`ProviderConfig`) are exposed as typed Pydantic models.
"""

from loom.config.base import (
    ConfigStore as ConfigStore,
)
from loom.config.base import (
    LoomConfig as LoomConfig,
)
from loom.config.base import (
    ProviderConfig as ProviderConfig,
)
from loom.config.resolver import (
    resolve_config as resolve_config,
)
from loom.config.resolver import (
    resolve_from_env as resolve_from_env,
)

__all__ = [
    "ConfigStore",
    "LoomConfig",
    "ProviderConfig",
    "resolve_config",
    "resolve_from_env",
]
