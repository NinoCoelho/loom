"""DuckDuckGo search provider using the ``ddgs`` library.

Runs the synchronous ``ddgs`` client in a background thread via
:func:`asyncio.to_thread`. Falls back from text to news search when
text yields no results. Requires the ``[search]`` optional extra.
"""

from __future__ import annotations

import asyncio
import logging

from loom.search.base import SearchProviderError, SearchResult

logger = logging.getLogger(__name__)


class DuckDuckGoSearchProvider:
    """DuckDuckGo search provider backed by the ``ddgs`` library.

    Runs the synchronous client in a thread so the event loop stays
    unblocked. Falls back from text to news search if text returns
    nothing.
    """

    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: int = 10,
        verify: bool = True,
    ) -> None:
        """Configure proxy, timeout, and TLS verification.

        Args:
            proxy: Optional proxy URL (e.g. ``"socks5://…"``).
            timeout: Request timeout in seconds.
            verify: Whether to verify TLS certificates.
        """
        self._proxy = proxy
        self._timeout = timeout
        self._verify = verify

    @property
    def name(self) -> str:
        """Provider identifier (``\"ddgs\"``)."""
        return "ddgs"

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        """Run a text search; falls back to news if text yields nothing.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return.

        Returns:
            A list of :class:`~loom.search.base.SearchResult` instances.

        Raises:
            SearchProviderError: If ``ddgs`` is not installed or the
                request fails.
        """
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
        """Synchronous ``ddgs`` call (executed in a thread).

        Args:
            query: Search query string.
            max_results: Maximum number of results to return.

        Returns:
            Raw list of dicts from the ``ddgs`` API.
        """
        from ddgs import DDGS

        ddgs = DDGS(proxy=self._proxy, timeout=self._timeout, verify=self._verify)
        raw = ddgs.text(query, max_results=max_results)
        if not raw:
            raw = ddgs.news(query, max_results=max_results)
        return raw or []
