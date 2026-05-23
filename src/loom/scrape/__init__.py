"""Web scraping subsystem — pluggable providers for fetching and extracting web content.

Ships one concrete provider:

* :class:`ScraplingProvider` — Scrapling-based fetcher with cascade mode
  (HTTP → headless → stealthy) and optional cookie auth via
  :class:`~loom.store.cookies.CookieStore`.

All providers implement the :class:`ScrapeProvider` protocol and return
:class:`ScrapeResult` dataclasses. Errors are surfaced as
:class:`ScrapeProviderError`.

The optional :class:`Blocklist` provides ad/tracker URL filtering that
can be shared across scrape providers and HTTP tools.
"""

from loom.scrape.base import ScrapeProvider, ScrapeProviderError, ScrapeResult
from loom.scrape.blocklist import Blocklist
from loom.scrape.scrapling import ScraplingProvider

__all__ = [
    "Blocklist",
    "ScrapeProvider",
    "ScrapeProviderError",
    "ScrapeResult",
    "ScraplingProvider",
]
