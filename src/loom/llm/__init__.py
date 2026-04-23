"""LLM provider abstractions and concrete implementations.

Public surface::

    from loom.llm import (
        LLMProvider,          # abstract base
        AnthropicProvider,    # concrete Anthropic + Bedrock provider
        OpenAICompatibleProvider,  # OpenAI + compatible proxies (Ollama, etc.)
        ProviderRegistry,     # model_id → (provider, upstream_model) mapping
        redact_sensitive_text,  # log redaction helper
    )

:class:`LLMProvider` defines the contract all backends must implement:
:meth:`chat` (blocking) and :meth:`chat_stream` (async iterator of
:class:`~loom.types.StreamEvent`). Provider selection is driven by the
:attr:`ProviderRegistry.default_model` or a per-call ``model_id`` argument.
"""

from loom.llm.anthropic import AnthropicProvider
from loom.llm.base import LLMProvider
from loom.llm.openai_compat import OpenAICompatibleProvider
from loom.llm.redact import redact_sensitive_text
from loom.llm.registry import ProviderRegistry

__all__ = [
    "LLMProvider",
    "OpenAICompatibleProvider",
    "AnthropicProvider",
    "ProviderRegistry",
    "redact_sensitive_text",
]
