"""Web scraping subsystem — core types and protocol for pluggable scrape providers.

A :class:`ScrapeProvider` fetches a URL and returns a :class:`ScrapeResult`
with content in the requested format. Errors are raised as
:class:`ScrapeProviderError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class ScrapeResult:
    """Dataclass representing a scraped page.

    Carries the URL, content, content type, optional HTTP status code,
    cookies, and arbitrary metadata.
    """

    url: str
    content: str
    content_type: str
    status_code: int | None = None
    cookies: dict[str, str] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ScrapeProviderError(Exception):
    """Raised when a scrape provider fails.

    Carries the provider name and an optional HTTP status code.
    """

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
    """Runtime-checkable protocol for pluggable scrape backends.

    Implement the :attr:`name` property and the :meth:`scrape` async method.
    """

    @property
    def name(self) -> str:
        """Provider identifier string."""
        ...

    async def scrape(
        self,
        url: str,
        output_format: str = "text",
        css_selector: str | None = None,
        xpath: str | None = None,
    ) -> ScrapeResult:
        """Fetch a URL and return content in the requested format.

        Optionally extract via *css_selector* or *xpath*.
        """
        ...
