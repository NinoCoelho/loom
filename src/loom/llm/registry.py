"""Model-to-provider registry.

:class:`ProviderRegistry` maps a ``model_id`` (the name used internally by loom)
to a ``(provider, upstream_model)`` pair. The first registered model becomes the
default; callers can override per-call via the ``model`` argument.

``upstream_model`` is the actual model string passed to the provider's API
(e.g. ``"claude-3-5-sonnet-20241022"``), which may differ from the ``model_id``
when the registry is used to alias or abstract provider endpoints.
"""

from __future__ import annotations

from loom.llm.base import LLMProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, tuple[LLMProvider, str]] = {}
        self._default_model: str | None = None

    def register(self, model_id: str, provider: LLMProvider, upstream_model: str) -> None:
        self._providers[model_id] = (provider, upstream_model)
        if self._default_model is None:
            self._default_model = model_id

    def resolve(self, model_id: str) -> tuple[LLMProvider, str]:
        if model_id not in self._providers:
            raise KeyError(f"No provider registered for model '{model_id}'")
        return self._providers[model_id]

    def list_models(self) -> list[str]:
        return list(self._providers.keys())

    @property
    def default_model(self) -> str | None:
        return self._default_model

    @default_model.setter
    def default_model(self, model_id: str) -> None:
        if model_id not in self._providers:
            raise KeyError(f"No provider registered for model '{model_id}'")
        self._default_model = model_id
