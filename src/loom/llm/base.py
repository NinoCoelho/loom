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
