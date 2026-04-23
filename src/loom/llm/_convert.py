"""Shared message/tool conversion helpers for LLM providers.

Both :class:`~loom.llm.anthropic.AnthropicProvider` and
:class:`~loom.llm.openai_compat.OpenAICompatibleProvider` need to convert
Loom's :class:`~loom.types.ToolSpec` into provider-specific tool schemas.
This module centralises that mapping.
"""

from __future__ import annotations

from typing import Any

from loom.types import ToolSpec


def convert_tools_openai(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Convert Loom tool specs to OpenAI-compatible function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def convert_tools_anthropic(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Convert Loom tool specs to Anthropic tool-use format."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in tools
    ]
