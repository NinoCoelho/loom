from __future__ import annotations

import json
from dataclasses import dataclass

import httpx
import pytest

from loom.search.base import SearchProviderError, SearchResult
from loom.search.brave import BraveSearchProvider
from loom.search.composite import CompositeSearchProvider, SearchStrategy
from loom.search.google import GoogleSearchProvider
from loom.search.tavily import TavilySearchProvider
from loom.tools.search import WebSearchTool


def _mock_client_factory(transport: httpx.MockTransport):
    original = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    return _factory


@pytest.fixture
def patch_client(monkeypatch):
    def _install(handler):
        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(httpx, "AsyncClient", _mock_client_factory(transport))
        return transport

    return _install


_BRAVE_RESPONSE = {
    "web": {
        "results": [
            {
                "title": "Brave Result 1",
                "url": "https://example.com/brave1",
                "description": "Brave snippet 1",
            },
            {
                "title": "Brave Result 2",
                "url": "https://example.com/brave2",
                "description": "Brave snippet 2",
            },
        ]
    }
}

_TAVILY_RESPONSE = {
    "results": [
        {
            "title": "Tavily Result 1",
            "url": "https://example.com/tavily1",
            "content": "Tavily snippet 1",
            "score": 0.95,
        },
        {
            "title": "Tavily Result 2",
            "url": "https://example.com/tavily2",
            "content": "Tavily snippet 2",
            "score": 0.8,
        },
    ]
}

_GOOGLE_RESPONSE = {
    "items": [
        {
            "title": "Google Result 1",
            "link": "https://example.com/google1",
            "snippet": "Google snippet 1",
        },
        {
            "title": "Google Result 2",
            "link": "https://example.com/google2",
            "snippet": "Google snippet 2",
        },
    ]
}


async def test_brave_provider_parses_results(patch_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "X-Subscription-Token" in request.headers
        assert request.url.params["q"] == "test query"
        return httpx.Response(200, json=_BRAVE_RESPONSE)

    patch_client(handler)
    provider = BraveSearchProvider(api_key="test-key")
    results = await provider.search("test query", max_results=5)

    assert len(results) == 2
    assert results[0].title == "Brave Result 1"
    assert results[0].url == "https://example.com/brave1"
    assert results[0].snippet == "Brave snippet 1"
    assert results[0].source == "brave"


async def test_brave_provider_429_raises(patch_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    patch_client(handler)
    provider = BraveSearchProvider(api_key="test-key")

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("test")

    assert exc_info.value.provider == "brave"
    assert exc_info.value.status_code == 429


async def test_tavily_provider_parses_results(patch_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body["query"] == "test query"
        return httpx.Response(200, json=_TAVILY_RESPONSE)

    patch_client(handler)
    provider = TavilySearchProvider(api_key="test-key")
    results = await provider.search("test query", max_results=5)

    assert len(results) == 2
    assert results[0].title == "Tavily Result 1"
    assert results[0].score == 0.95
    assert results[0].source == "tavily"


async def test_tavily_provider_429_raises(patch_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": {"error": "rate limited"}})

    patch_client(handler)
    provider = TavilySearchProvider(api_key="test-key")

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("test")

    assert exc_info.value.provider == "tavily"
    assert exc_info.value.status_code == 429


async def test_google_provider_parses_results(patch_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "test query"
        assert request.url.params["cx"] == "my-cx"
        return httpx.Response(200, json=_GOOGLE_RESPONSE)

    patch_client(handler)
    provider = GoogleSearchProvider(api_key="test-key", cx="my-cx")
    results = await provider.search("test query", max_results=5)

    assert len(results) == 2
    assert results[0].title == "Google Result 1"
    assert results[0].url == "https://example.com/google1"
    assert results[0].snippet == "Google snippet 1"
    assert results[0].source == "google"


async def test_google_provider_caps_at_10(patch_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert int(request.url.params["num"]) == 10
        return httpx.Response(200, json={"items": []})

    patch_client(handler)
    provider = GoogleSearchProvider(api_key="test-key", cx="my-cx")
    await provider.search("test", max_results=50)


async def test_google_provider_429_raises(patch_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="rate limited")

    patch_client(handler)
    provider = GoogleSearchProvider(api_key="test-key", cx="my-cx")

    with pytest.raises(SearchProviderError) as exc_info:
        await provider.search("test")

    assert exc_info.value.provider == "google"
    assert exc_info.value.status_code == 429


@dataclass
class StubProvider:
    _name: str
    _results: list[SearchResult]
    _error: SearchProviderError | None = None

    @property
    def name(self) -> str:
        return self._name

    async def search(self, query: str, max_results: int = 10) -> list[SearchResult]:
        if self._error:
            raise self._error
        return self._results[:max_results]


def _make_result(source: str, idx: int) -> SearchResult:
    return SearchResult(
        title=f"{source} Result {idx}",
        url=f"https://example.com/{source.lower()}{idx}",
        snippet=f"{source} snippet {idx}",
        source=source,
    )


async def test_composite_concurrent_merges_results():
    p1 = StubProvider("alpha", [_make_result("alpha", 1), _make_result("alpha", 2)])
    p2 = StubProvider("beta", [_make_result("beta", 1)])

    composite = CompositeSearchProvider([p1, p2], strategy=SearchStrategy.CONCURRENT)
    results = await composite.search("test", max_results=10)

    assert len(results) == 3
    assert results[0].source == "alpha"


async def test_composite_concurrent_deduplicates():
    r = SearchResult(title="Same", url="https://example.com/page", snippet="s", source="a")
    p1 = StubProvider("alpha", [r])
    p2 = StubProvider("beta", [r])

    composite = CompositeSearchProvider([p1, p2], strategy=SearchStrategy.CONCURRENT)
    results = await composite.search("test", max_results=10)

    assert len(results) == 1


async def test_composite_concurrent_partial_failure():
    p1 = StubProvider("alpha", [_make_result("alpha", 1)])
    p2 = StubProvider(
        "beta",
        [],
        _error=SearchProviderError("beta", "Rate limited", status_code=429),
    )

    composite = CompositeSearchProvider([p1, p2], strategy=SearchStrategy.CONCURRENT)
    results = await composite.search("test", max_results=10)

    assert len(results) == 1
    assert results[0].source == "alpha"


async def test_composite_concurrent_all_fail():
    p1 = StubProvider(
        "alpha",
        [],
        _error=SearchProviderError("alpha", "fail", status_code=429),
    )
    p2 = StubProvider(
        "beta",
        [],
        _error=SearchProviderError("beta", "down"),
    )

    composite = CompositeSearchProvider([p1, p2], strategy=SearchStrategy.CONCURRENT)

    with pytest.raises(SearchProviderError) as exc_info:
        await composite.search("test")

    assert "2 providers failed" in str(exc_info.value)


async def test_composite_fallback_stops_when_enough():
    p1 = StubProvider("alpha", [_make_result("alpha", i) for i in range(5)])
    p2 = StubProvider("beta", [_make_result("beta", i) for i in range(5)])

    composite = CompositeSearchProvider([p1, p2], strategy=SearchStrategy.FALLBACK)
    results = await composite.search("test", max_results=5)

    assert len(results) == 5
    assert all(r.source == "alpha" for r in results)


async def test_composite_fallback_tries_next_on_error():
    p1 = StubProvider(
        "alpha",
        [],
        _error=SearchProviderError("alpha", "Rate limited", status_code=429),
    )
    p2 = StubProvider("beta", [_make_result("beta", 1)])

    composite = CompositeSearchProvider([p1, p2], strategy=SearchStrategy.FALLBACK)
    results = await composite.search("test", max_results=5)

    assert len(results) == 1
    assert results[0].source == "beta"


async def test_composite_fallback_tries_next_on_insufficient():
    p1 = StubProvider("alpha", [_make_result("alpha", 1)])
    p2 = StubProvider("beta", [_make_result("beta", 2), _make_result("beta", 3)])

    composite = CompositeSearchProvider([p1, p2], strategy=SearchStrategy.FALLBACK)
    results = await composite.search("test", max_results=5)

    assert len(results) == 3
    assert results[0].source == "alpha"
    assert results[1].source == "beta"


async def test_composite_fallback_all_fail():
    p1 = StubProvider(
        "alpha",
        [],
        _error=SearchProviderError("alpha", "fail"),
    )
    p2 = StubProvider(
        "beta",
        [],
        _error=SearchProviderError("beta", "down"),
    )

    composite = CompositeSearchProvider([p1, p2], strategy=SearchStrategy.FALLBACK)

    with pytest.raises(SearchProviderError) as exc_info:
        await composite.search("test")

    assert "2 providers failed" in str(exc_info.value)


async def test_web_search_tool_returns_json():
    provider = StubProvider("stub", [_make_result("stub", 1)])
    tool = WebSearchTool(provider)
    result = await tool.invoke({"query": "test"})

    assert not result.is_error
    items = json.loads(result.text)
    assert len(items) == 1
    assert items[0]["source"] == "stub"
    assert result.metadata["provider"] == "stub"
    assert result.metadata["count"] == 1


async def test_web_search_tool_error_on_empty_query():
    provider = StubProvider("stub", [])
    tool = WebSearchTool(provider)
    result = await tool.invoke({"query": ""})

    assert result.is_error
    assert "query is required" in result.text


async def test_web_search_tool_handles_provider_error():
    provider = StubProvider(
        "stub",
        [],
        _error=SearchProviderError("stub", "Rate limited", status_code=429),
    )
    tool = WebSearchTool(provider)
    result = await tool.invoke({"query": "test"})

    assert result.is_error
    assert "Rate limited" in result.text


async def test_from_config_default_is_ddgs():
    tool = WebSearchTool.from_config()
    assert tool._provider.name == "ddgs"


async def test_from_config_single_provider():
    p = StubProvider("alpha", [])
    tool = WebSearchTool.from_config([p])
    assert tool._provider.name == "alpha"


async def test_from_config_multiple_wraps_composite():
    p1 = StubProvider("alpha", [])
    p2 = StubProvider("beta", [])
    tool = WebSearchTool.from_config([p1, p2], strategy=SearchStrategy.FALLBACK)
    assert tool._provider.name == "composite"


async def test_composite_requires_at_least_one_provider():
    with pytest.raises(ValueError, match="At least one"):
        CompositeSearchProvider([])
