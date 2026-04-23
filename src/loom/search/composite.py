"""Multi-provider search orchestration.

:class:`CompositeSearchProvider` wraps multiple
:class:`~loom.search.base.SearchProvider` instances and merges their
results using either a concurrent (fire-all, merge, deduplicate) or
fallback (try in order) strategy. URL deduplication normalises
scheme+host+path and de-duplicates across providers.
"""

from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from urllib.parse import urlparse

from loom.search.base import SearchProviderError, SearchResult

logger = logging.getLogger(__name__)


class SearchStrategy(StrEnum):
    """Strategy for combining multiple search providers."""

    CONCURRENT = "concurrent"
    FALLBACK = "fallback"


def _normalize_url(url: str) -> str:
    """Strip query params and fragment, lower-case for dedup."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/").lower()


class CompositeSearchProvider:
    """Orchestrates multiple search providers with a pluggable strategy."""

    def __init__(
        self,
        providers: list,
        *,
        strategy: SearchStrategy = SearchStrategy.CONCURRENT,
    ) -> None:
        """Accept a list of providers and a strategy (default: CONCURRENT).

        Args:
            providers: One or more :class:`~loom.search.base.SearchProvider`
                instances.
            strategy: :class:`SearchStrategy` for combining results.

        Raises:
            ValueError: If *providers* is empty.
        """
        if not providers:
            raise ValueError("At least one SearchProvider is required")
        self._providers = providers
        self._strategy = strategy

    @property
    def name(self) -> str:
        """Provider identifier (``\"composite\"``)."""
        return "composite"

    @property
    def strategy(self) -> SearchStrategy:
        """Active search strategy."""
        return self._strategy

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Dispatch to the configured strategy.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return.

        Returns:
            A deduplicated list of :class:`~loom.search.base.SearchResult`
            instances.

        Raises:
            SearchProviderError: If all providers fail.
        """
        if self._strategy == SearchStrategy.CONCURRENT:
            return await self._search_concurrent(query, max_results)
        return await self._search_fallback(query, max_results)

    async def _search_concurrent(
        self, query: str, max_results: int
    ) -> list[SearchResult]:
        """Fire all providers concurrently, merge and deduplicate results."""
        coros = [p.search(query, max_results) for p in self._providers]
        outcomes = await asyncio.gather(*coros, return_exceptions=True)

        all_results: list[SearchResult] = []
        errors: list[SearchProviderError] = []

        for provider, outcome in zip(self._providers, outcomes):
            pname = provider.name
            if isinstance(outcome, SearchProviderError):
                logger.warning("Search provider %s failed: %s", pname, outcome)
                errors.append(outcome)
            elif isinstance(outcome, Exception):
                logger.warning("Search provider %s failed: %s", pname, outcome)
                errors.append(SearchProviderError(pname, str(outcome)))
            else:
                all_results.extend(outcome)

        if not all_results and errors:
            raise SearchProviderError(
                errors[0].provider,
                f"All {len(errors)} providers failed: "
                + "; ".join(f"[{e.provider}] {e}" for e in errors),
            )

        return _deduplicate(all_results, max_results)

    async def _search_fallback(
        self, query: str, max_results: int
    ) -> list[SearchResult]:
        """Try providers in order, stopping when enough results are collected."""
        all_results: list[SearchResult] = []
        errors: list[SearchProviderError] = []

        for provider in self._providers:
            needed = max_results - len(all_results)
            if needed <= 0:
                break

            try:
                results = await provider.search(query, needed)
                all_results.extend(results)
            except SearchProviderError as exc:
                logger.warning(
                    "Search provider %s failed, trying next: %s",
                    provider.name,
                    exc,
                )
                errors.append(exc)
            except Exception as exc:
                logger.warning(
                    "Search provider %s failed, trying next: %s",
                    provider.name,
                    exc,
                )
                errors.append(SearchProviderError(provider.name, str(exc)))

        if not all_results and errors:
            raise SearchProviderError(
                errors[0].provider,
                f"All {len(errors)} providers failed: "
                + "; ".join(f"[{e.provider}] {e}" for e in errors),
            )

        return _deduplicate(all_results, max_results)


def _deduplicate(
    results: list[SearchResult], max_results: int
) -> list[SearchResult]:
    """Remove duplicate URLs (normalised) and cap at *max_results*."""
    seen: set[str] = set()
    unique: list[SearchResult] = []
    for r in results:
        key = _normalize_url(r.url)
        if key and key not in seen:
            seen.add(key)
            unique.append(r)
            if len(unique) >= max_results:
                break
    return unique
