"""Abstract LLM provider interface.

:class:`LLMProvider` is the abstract base for all LLM backends. Implementors
must provide :meth:`chat` (blocking) and optionally override :meth:`chat_stream`
(async iterator) when streaming is supported.

The two-method split lets callers choose the mode that fits their use case:
:meth:`chat` returns a complete :class:`~loom.types.ChatResponse`; :meth:`chat_stream`
yields a sequence of :class:`~loom.types.StreamEvent` objects ending with a
:class:`~loom.types.DoneEvent`. When streaming is not implemented the default
raises :exc:`NotImplementedError`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from loom.types import ChatMessage, ChatResponse, StreamEvent, ToolSpec


class LLMProvider(ABC):
    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> ChatResponse: ...

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        raise NotImplementedError("Streaming not supported by this provider")
