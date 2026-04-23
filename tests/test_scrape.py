from __future__ import annotations

import pytest

from loom.scrape.base import ScrapeProviderError, ScrapeResult
from loom.scrape.scrapling import (
    ScraplingProvider,
    _extract_by_selector,
    _extract_domain,
    _html_to_markdown,
    _looks_like_auth_failure,
    _looks_like_block,
    _truncate,
)
from loom.store.cookies import FilesystemCookieStore
from loom.tools.scrape import WebScrapeTool


@pytest.fixture
def cookie_dir(tmp_dir):
    d = tmp_dir / "cookies"
    d.mkdir()
    return d


@pytest.fixture
def cookie_store(cookie_dir):
    return FilesystemCookieStore(cookie_dir)


async def test_cookie_store_save_and_get(cookie_store, cookie_dir):
    await cookie_store.save_cookies("example.com", {"sid": "abc123", "uid": "42"})
    cookies = await cookie_store.get_cookies("example.com")
    assert cookies == {"sid": "abc123", "uid": "42"}

    path = cookie_dir / "example.com.cookies.txt"
    assert path.exists()
    text = path.read_text()
    assert ".example.com" in text
    assert "sid" in text


async def test_cookie_store_get_missing(cookie_store):
    cookies = await cookie_store.get_cookies("unknown.com")
    assert cookies is None


async def test_cookie_store_list_domains(cookie_store):
    await cookie_store.save_cookies("a.com", {"x": "1"})
    await cookie_store.save_cookies("b.com", {"y": "2"})
    domains = await cookie_store.list_domains()
    assert domains == ["a.com", "b.com"]


async def test_cookie_store_overwrite(cookie_store):
    await cookie_store.save_cookies("example.com", {"sid": "old"})
    await cookie_store.save_cookies("example.com", {"sid": "new"})
    cookies = await cookie_store.get_cookies("example.com")
    assert cookies == {"sid": "new"}


def test_extract_domain():
    assert _extract_domain("https://example.com/path") == "example.com"
    assert _extract_domain("https://sub.example.com:8080/p") == "sub.example.com"


def test_truncate_under_limit():
    assert _truncate("hello", 100) == "hello"


def test_truncate_over_limit():
    long_str = "x" * 2000
    result = _truncate(long_str, 100)
    assert result.endswith("[truncated]")
    assert len(result.encode("utf-8")) <= 200


def test_looks_like_block():
    assert _looks_like_block("cf-challenge blah", 403)
    assert _looks_like_block("Just a Moment...", 503)
    assert not _looks_like_block("Hello World", 200)
    assert not _looks_like_block("Normal page content here", 403)


def test_looks_like_auth_failure():
    assert _looks_like_auth_failure("unauthorized", 401)
    assert _looks_like_auth_failure('<form action="/login">', 403)
    assert _looks_like_auth_failure("access denied", 403)
    assert not _looks_like_auth_failure("cf-challenge enabled", 403)
    assert not _looks_like_auth_failure("Hello World", 200)


def test_html_to_markdown():
    html = "<h1>Title</h1><p>Hello <strong>world</strong></p><ul><li>one</li><li>two</li></ul>"
    md = _html_to_markdown(html)
    assert "Title" in md
    assert "**world**" in md
    assert "- one" in md
    assert "- two" in md


def test_extract_content_no_selector():
    html = "<html><body><h1>Hello</h1><p>World</p></body></html>"
    result = _extract_by_selector(html, None, None)
    assert result == html


async def test_scrapling_provider_invalid_mode():
    with pytest.raises(ValueError, match="Invalid mode"):
        ScraplingProvider(mode="invalid")


async def test_scrapling_provider_import_error():
    import unittest.mock

    with unittest.mock.patch.dict("sys.modules", {"scrapling.fetchers": None, "scrapling": None}):
        provider = ScraplingProvider(mode="fetcher")
        with pytest.raises(ScrapeProviderError, match="not installed"):
            await provider._fetch("https://example.com", "text", "fetcher")


async def test_web_scrape_tool_returns_content():
    class StubProvider:
        @property
        def name(self):
            return "stub"

        async def scrape(self, url, output_format="text", css_selector=None, xpath=None):
            return ScrapeResult(
                url=url,
                content="Hello World",
                content_type="text",
                status_code=200,
            )

    tool = WebScrapeTool(StubProvider())
    result = await tool.invoke({"url": "https://example.com"})
    assert not result.is_error
    assert result.text == "Hello World"
    assert result.metadata["status_code"] == 200
    assert result.metadata["provider"] == "stub"


async def test_web_scrape_tool_error_on_empty_url():
    class StubProvider:
        @property
        def name(self):
            return "stub"

        async def scrape(self, url, **kw):
            return ScrapeResult(url=url, content="", content_type="text")

    tool = WebScrapeTool(StubProvider())
    result = await tool.invoke({"url": ""})
    assert result.is_error
    assert "url is required" in result.text


async def test_web_scrape_tool_handles_provider_error():
    class FailProvider:
        @property
        def name(self):
            return "fail"

        async def scrape(self, url, **kw):
            raise ScrapeProviderError("fail", "Connection refused")

    tool = WebScrapeTool(FailProvider())
    result = await tool.invoke({"url": "https://example.com"})
    assert result.is_error
    assert "Connection refused" in result.text


async def test_from_config_creates_provider():
    tool = WebScrapeTool.from_config(mode="fetcher")
    assert tool._provider.name == "scrapling"
    assert tool._provider._mode == "fetcher"


async def test_from_config_with_cookie_store(cookie_store):
    tool = WebScrapeTool.from_config(cookie_store=cookie_store)
    assert tool._provider._cookie_store is cookie_store


async def test_cookie_auth_fallback_flow(cookie_store):
    class FakeProvider:
        @property
        def name(self):
            return "fake"

        async def scrape(self, url, output_format="text", css_selector=None, xpath=None):
            return ScrapeResult(
                url=url, content="ok data", content_type="text", status_code=200
            )

    await cookie_store.save_cookies("example.com", {"session": "xyz"})
    cookies = await cookie_store.get_cookies("example.com")
    assert cookies == {"session": "xyz"}


def test_netscape_round_trip(cookie_dir):
    store = FilesystemCookieStore(cookie_dir)
    original = {"sid": "abc", "token": "def", "pref": "dark"}
    _sync(store.save_cookies("test.com", original))
    loaded = _sync(store.get_cookies("test.com"))
    assert loaded == original


def _sync(coro):
    import asyncio

    return asyncio.get_event_loop().run_until_complete(coro)
