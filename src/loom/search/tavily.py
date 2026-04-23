from __future__ import annotations

from typing import Any

import httpx

from loom.search.base import SearchProviderError, SearchResult

_TAVILY_SEARCH_URL = "https://api.tavily.com/search"


class TavilySearchProvider:
    def __init__(
        self,
        api_key: str,
        *,
        search_depth: str = "basic",
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._search_depth = search_depth
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "tavily"

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
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
