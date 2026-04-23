from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str
    score: float | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


class SearchProviderError(Exception):
    def __init__(
        self,
        provider: str,
        message: str,
        status_code: int | None = None,
    ) -> None:
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"[{provider}] {message}")


@runtime_checkable
class SearchProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]: ...
