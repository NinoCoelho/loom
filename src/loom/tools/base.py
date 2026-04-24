"""Tool system primitives — :class:`ToolHandler` and :class:`ToolResult`.

:class:`ToolHandler` is the abstract base for all tool backends. Each
handler declares its interface via a :class:`~loom.types.ToolSpec` and
returns a :class:`ToolResult` (text, optional structured content parts,
and an error flag) from :meth:`invoke`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from loom.types import ContentPart, ToolSpec


class ToolResult:
    def __init__(
        self,
        text: str,
        metadata: dict | None = None,
        is_error: bool = False,
        content_parts: list[ContentPart] | None = None,
    ) -> None:
        self.text = text
        self.metadata = metadata or {}
        self.is_error = is_error
        self.content_parts = content_parts

    def __repr__(self) -> str:
        truncated = len(self.text) > 40
        preview = self.text[:40] + "..." if truncated else self.text
        return (
            f"ToolResult(text={preview!r}, is_error={self.is_error}, "
            f"content_parts={len(self.content_parts) if self.content_parts else 0})"
        )

    def to_text(self) -> str:
        return self.text


class ToolHandler(ABC):
    @property
    @abstractmethod
    def tool(self) -> ToolSpec: ...

    @abstractmethod
    async def invoke(self, args: dict) -> ToolResult: ...
