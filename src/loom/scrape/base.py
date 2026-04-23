from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ScrapeResult:
    url: str
    content: str
    content_type: str
    status_code: int | None = None
    cookies: dict[str, str] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ScrapeProviderError(Exception):
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
class ScrapeProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def scrape(
        self,
        url: str,
        output_format: str = "text",
        css_selector: str | None = None,
        xpath: str | None = None,
    ) -> ScrapeResult: ...
