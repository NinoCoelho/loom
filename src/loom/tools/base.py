from __future__ import annotations

from abc import ABC, abstractmethod

from loom.types import ToolSpec


class ToolResult:
    def __init__(self, text: str, metadata: dict | None = None) -> None:
        self.text = text
        self.metadata = metadata or {}

    def to_text(self) -> str:
        return self.text


class ToolHandler(ABC):
    @property
    @abstractmethod
    def tool(self) -> ToolSpec: ...

    @abstractmethod
    async def invoke(self, args: dict) -> ToolResult: ...
