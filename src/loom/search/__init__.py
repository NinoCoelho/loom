"""Web search subsystem — pluggable providers and multi-provider orchestration.

Ships five concrete providers:

* :class:`DuckDuckGoSearchProvider` — free, no API key (default).
* :class:`BraveSearchProvider` — Brave Search API.
* :class:`TavilySearchProvider` — Tavily Search API (with scored results).
* :class:`GoogleSearchProvider` — Google Custom Search API.
* :class:`CompositeSearchProvider` — wrap multiple providers with
  :class:`SearchStrategy` ``CONCURRENT`` or ``FALLBACK`` merging.

All providers implement the :class:`SearchProvider` protocol and return
:class:`SearchResult` dataclasses. Errors are surfaced as
:class:`SearchProviderError`.
"""

from loom.search.base import SearchProvider, SearchProviderError, SearchResult
from loom.search.brave import BraveSearchProvider
from loom.search.composite import CompositeSearchProvider, SearchStrategy
from loom.search.ddgs import DuckDuckGoSearchProvider
from loom.search.google import GoogleSearchProvider
from loom.search.tavily import TavilySearchProvider

__all__ = [
    "BraveSearchProvider",
    "CompositeSearchProvider",
    "DuckDuckGoSearchProvider",
    "GoogleSearchProvider",
    "SearchProvider",
    "SearchProviderError",
    "SearchResult",
    "SearchStrategy",
    "TavilySearchProvider",
]
