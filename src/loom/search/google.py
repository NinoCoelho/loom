from __future__ import annotations

import httpx

from loom.search.base import SearchProviderError, SearchResult

_GOOGLE_SEARCH_URL = "https://customsearch.googleapis.com/customsearch/v1"


class GoogleSearchProvider:
    def __init__(
        self,
        api_key: str,
        cx: str,
        *,
        timeout: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cx = cx
        self._timeout = timeout

    @property
    def name(self) -> str:
        return "google"

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        effective_max = min(max_results, 10)

        params = {
            "key": self._api_key,
            "cx": self._cx,
            "q": query,
            "num": effective_max,
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(_GOOGLE_SEARCH_URL, params=params)

            if resp.status_code == 429:
                raise SearchProviderError("google", "Rate limited", status_code=429)
            resp.raise_for_status()
        except SearchProviderError:
            raise
        except httpx.HTTPStatusError as exc:
            raise SearchProviderError(
                "google", str(exc), status_code=exc.response.status_code
            ) from exc
        except httpx.RequestError as exc:
            raise SearchProviderError("google", str(exc)) from exc

        data = resp.json()
        items = data.get("items", [])

        results: list[SearchResult] = []
        for item in items:
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    source="google",
                    raw=item,
                )
            )
        return results
