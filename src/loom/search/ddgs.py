from __future__ import annotations

import logging

from loom.search.base import SearchProviderError, SearchResult

logger = logging.getLogger(__name__)


class DuckDuckGoSearchProvider:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: int = 10,
        headers: dict | None = None,
    ) -> None:
        self._proxy = proxy
        self._timeout = timeout
        self._headers = headers

    @property
    def name(self) -> str:
        return "ddgs"

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        try:
            from duckduckgo_search import DDGS
        except ImportError as exc:
            raise SearchProviderError(
                "ddgs",
                "duckduckgo-search is not installed. Install with: pip install loom[search]",
            ) from exc

        try:
            kwargs: dict = {}
            if self._proxy:
                kwargs["proxy"] = self._proxy
            if self._headers:
                kwargs["headers"] = self._headers

            with DDGS(**kwargs) as ddgs:
                raw = ddgs.text(query, max_results=max_results)
        except Exception as exc:
            msg = str(exc)
            status = None
            if "ratelimit" in msg.lower() or "429" in msg:
                status = 429
            raise SearchProviderError("ddgs", msg, status_code=status) from exc

        results: list[SearchResult] = []
        for item in raw:
            results.append(
                SearchResult(
                    title=item.get("title", ""),
                    url=item.get("href", ""),
                    snippet=item.get("body", ""),
                    source="ddgs",
                    raw=item,
                )
            )
        return results
