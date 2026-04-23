"""Brave Search API provider.

Uses :mod:`httpx` for async HTTP. Requires an API key
(``X-Subscription-Token`` header).
"""

from __future__ import annotations

import httpx

from loom.search.base import SearchProviderError, SearchResult

_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"


class BraveSearchProvider:
    """Search provider backed by the Brave Search API.

    Requires an API subscription key.
    """

    def __init__(self, api_key: str, *, timeout: float = 15.0) -> None:
        """Configure API key and request timeout.

        Args:
            api_key: Brave Search API subscription token.
            timeout: HTTP request timeout in seconds.
        """
        self._api_key = api_key
        self._timeout = timeout

    @property
    def name(self) -> str:
        """Provider identifier (``\"brave\"``)."""
        return "brave"

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Query the Brave Web Search API and return results.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return.

        Returns:
            A list of :class:`~loom.search.base.SearchResult` instances.

        Raises:
            SearchProviderError: On HTTP errors or rate-limiting.
        """
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }
        params = {
            "q": query,
            "count": max_results,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(_BRAVE_SEARCH_URL, headers=headers, params=params)

            if resp.status_code == 429:
                raise SearchProviderError("brave", "Rate limited", status_code=429)
            resp.raise_for_status()
        except SearchProviderError:
            raise
        except httpx.HTTPStatusError as exc:
            raise SearchProviderError(
                "brave", str(exc), status_code=exc.response.status_code
            ) from exc
        except httpx.RequestError as exc:
            raise SearchProviderError("brave", str(exc)) from exc

        data = resp.json()
        items = data.get("web", {}).get("results", [])

        results: list[SearchResult] = []
        for item in items:
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("url", ""),
                    snippet=item.get("description", ""),
                    source="brave",
                    raw=item,
                )
            )
        return results
