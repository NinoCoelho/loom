"""Tavily Search API provider.

Uses :mod:`httpx` for async HTTP. Supports ``basic`` and ``advanced``
search depths. Requires an API key.
"""

from __future__ import annotations

from typing import Any

import httpx

from loom.search.base import SearchProviderError, SearchResult

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class TavilySearchProvider:
    """Search provider backed by the Tavily Search API.

    Supports configurable search depth (``basic`` or ``advanced``).
    """

    def __init__(
        self,
        api_key: str,
        *,
        search_depth: str = "basic",
        timeout: float = 30.0,
    ) -> None:
        """Configure API key, search depth, and timeout.

        Args:
            api_key: Tavily API key.
            search_depth: ``\"basic\"`` or ``\"advanced\"``.
            timeout: HTTP request timeout in seconds.
        """
        self._api_key = api_key
        self._search_depth = search_depth
        self._timeout = timeout

    @property
    def name(self) -> str:
        """Provider identifier (``\"tavily\"``)."""
        return "tavily"

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Query the Tavily API and return scored results.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return.

        Returns:
            A list of :class:`~loom.search.base.SearchResult` instances
            (includes relevance scores).

        Raises:
            SearchProviderError: On HTTP errors or rate-limiting.
        """
        payload: dict[str, Any] = {
            "query": query,
            "max_results": max_results,
            "search_depth": self._search_depth,
            "include_answer": False,
            "include_raw_content": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._api_key}",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(_TAVILY_SEARCH_URL, json=payload, headers=headers)

            if resp.status_code == 429:
                raise SearchProviderError("tavily", "Rate limited", status_code=429)
            resp.raise_for_status()
        except SearchProviderError:
            raise
        except httpx.HTTPStatusError as exc:
            raise SearchProviderError(
                "tavily", str(exc), status_code=exc.response.status_code
            ) from exc
        except httpx.RequestError as exc:
            raise SearchProviderError("tavily", str(exc)) from exc

        data = resp.json()
        items = data.get("results", [])

        results: list[SearchResult] = []
        for item in items:
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("content", ""),
                    source="tavily",
                    score=item.get("score"),
                    raw=item,
                )
            )
        return results
