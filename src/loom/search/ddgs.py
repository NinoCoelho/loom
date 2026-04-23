from __future__ import annotations

import asyncio
import logging

from loom.search.base import SearchProviderError, SearchResult

logger = logging.getLogger(__name__)


class DuckDuckGoSearchProvider:
    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: int = 10,
        verify: bool = True,
    ) -> None:
        self._proxy = proxy
        self._timeout = timeout
        self._verify = verify

    @property
    def name(self) -> str:
        return "ddgs"

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        try:
            from ddgs import DDGS  # noqa: F401
        except ImportError as exc:
            raise SearchProviderError(
                "ddgs",
                "ddgs is not installed. Install with: pip install 'loom[search]'",
            ) from exc

        try:
            raw = await asyncio.to_thread(self._run, query, max_results)
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
                    url=item.get("href") or item.get("url", ""),
                    snippet=item.get("body", ""),
                    source="ddgs",
                    raw=item,
                )
            )
        return results

    def _run(self, query: str, max_results: int) -> list[dict]:
        from ddgs import DDGS

        ddgs = DDGS(proxy=self._proxy, timeout=self._timeout, verify=self._verify)
        raw = ddgs.text(query, max_results=max_results)
        if not raw:
            raw = ddgs.news(query, max_results=max_results)
        return raw or []
