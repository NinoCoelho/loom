"""``web_search`` tool — web search exposed to the LLM.

Wraps a :class:`~loom.search.base.SearchProvider` and returns results as JSON.

Results are returned as JSON with title, URL, snippet, source, and optional
score.
"""

from __future__ import annotations

import json

from loom.search.base import SearchProvider, SearchProviderError
from loom.search.composite import CompositeSearchProvider, SearchStrategy
from loom.search.ddgs import DuckDuckGoSearchProvider
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec


class WebSearchTool(ToolHandler):
    """:class:`~loom.tools.base.ToolHandler` wrapping a
    :class:`~loom.search.base.SearchProvider` for web search."""

    def __init__(self, provider: SearchProvider) -> None:
        """Wrap a search provider."""
        self._provider = provider

    @property
    def tool(self) -> ToolSpec:
        """Tool spec: ``web_search`` with ``query`` and optional
        ``max_results`` parameters."""
        return ToolSpec(
            name="web_search",
            description=(
                "Search the web for information. Returns a list of results "
                "with title, url, snippet, and source provider."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results (default 10)",
                    },
                },
                "required": ["query"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        """Execute the search and return JSON-formatted results."""
        query = args.get("query", "")
        if not query:
            return ToolResult(text="Error: query is required", is_error=True)

        max_results = args.get("max_results", 10)

        try:
            results = await self._provider.search(query, max_results)
        except SearchProviderError as exc:
            return ToolResult(text=f"Search error: {exc}", is_error=True)

        items = []
        for r in results:
            item: dict = {
                "title": r.title,
                "url": r.url,
                "snippet": r.snippet,
                "source": r.source,
            }
            if r.score is not None:
                item["score"] = r.score
            items.append(item)

        return ToolResult(
            text=json.dumps(items, ensure_ascii=False),
            metadata={"count": len(items), "provider": self._provider.name},
        )

    @classmethod
    def from_config(
        cls,
        providers: list[SearchProvider] | None = None,
        *,
        strategy: SearchStrategy = SearchStrategy.CONCURRENT,
    ) -> WebSearchTool:
        """Factory — :class:`~loom.search.ddgs.DuckDuckGoSearchProvider`
        by default, or :class:`~loom.search.composite.CompositeSearchProvider`
        when multiple providers are given."""
        if not providers:
            return cls(DuckDuckGoSearchProvider())

        if len(providers) == 1:
            return cls(providers[0])

        composite = CompositeSearchProvider(providers, strategy=strategy)
        return cls(composite)
