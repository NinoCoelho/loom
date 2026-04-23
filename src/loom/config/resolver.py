"""Config resolution — merging file, environment, and CLI override sources.

Priority (highest to lowest): CLI overrides → environment variables →
config file. :func:`resolve_config` composes the full resolution chain;
:func:`resolve_from_env` reads from environment variables only.

Supported env vars: ``LOOM_LLM_BASE_URL``, ``LOOM_LLM_API_KEY``,
``LOOM_LLM_MODEL``.
"""

from __future__ import annotations

import os

from loom.config.base import LoomConfig

_ENV_MAP = {
    "base_url": "LOOM_LLM_BASE_URL",
    "api_key": "LOOM_LLM_API_KEY",
    "model": "LOOM_LLM_MODEL",
}


def resolve_from_env() -> dict[str, str]:
    result: dict[str, str] = {}
    for field, env_var in _ENV_MAP.items():
        val = os.environ.get(env_var)
        if val:
            result[field] = val
    return result


def resolve_config(
    cli_overrides: dict[str, str] | None = None,
    config: LoomConfig | None = None,
) -> tuple[str, str, str]:
    env = resolve_from_env()

    model = ""
    base_url = ""
    api_key = ""

    if config:
        model = config.default_model
        if config.providers:
            first = next(iter(config.providers.values()))
            base_url = first.base_url
            api_key = first.api_key_inline

    if env.get("model"):
        model = env["model"]
    if env.get("base_url"):
        base_url = env["base_url"]
    if env.get("api_key"):
        api_key = env["api_key"]

    if cli_overrides:
        if cli_overrides.get("model"):
            model = cli_overrides["model"]
        if cli_overrides.get("base_url"):
            base_url = cli_overrides["base_url"]
        if cli_overrides.get("api_key"):
            api_key = cli_overrides["api_key"]

    return base_url, api_key, model
