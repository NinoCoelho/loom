"""Search subsystem — core types and protocol for pluggable search providers.

Defines :class:`SearchResult`, :class:`SearchProviderError`, and the
:class:`SearchProvider` protocol that every concrete provider must
implement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class SearchResult:
    """A single search hit returned by a :class:`SearchProvider`.

    Attributes:
        title: Page title.
        url: Canonical URL of the result.
        snippet: Short text excerpt from the page.
        source: Provider identifier that produced this result.
        score: Optional relevance score (provider-specific).
        raw: Original payload from the upstream API (hidden from repr).
    """

    title: str
    url: str
    snippet: str
    source: str
    score: float | None = None
    raw: dict[str, Any] | None = field(default=None, repr=False)


class SearchProviderError(Exception):
    """Raised when a :class:`SearchProvider` fails.

    Carries the provider name and an optional HTTP status code so callers
    can distinguish rate-limits from other failures.
    """

    def __init__(
        self,
        provider: str,
        message: str,
        status_code: int | None = None,
    ) -> None:
        """Create a provider error.

        Args:
            provider: Name of the provider that failed.
            message: Human-readable error description.
            status_code: Optional HTTP status code (e.g. 429).
        """
        self.provider = provider
        self.status_code = status_code
        super().__init__(f"[{provider}] {message}")


@runtime_checkable
class SearchProvider(Protocol):
    """Runtime-checkable protocol for search backends.

    Implementations must provide a :attr:`name` property and an
    :meth:`async search` coroutine.
    """

    @property
    def name(self) -> str:
        """Provider identifier string (e.g. ``"ddgs"``, ``"brave"``)."""
        ...

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Execute a search query, returning up to *max_results* hits.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return.

        Returns:
            A list of :class:`SearchResult` instances.

        Raises:
            SearchProviderError: If the provider encounters an error.
        """
        ...
