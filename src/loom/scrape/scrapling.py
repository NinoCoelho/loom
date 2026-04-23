"""Scrapling-based web scrape provider with cascade fetching and cookie authentication fallback.

Supports three fetcher modes (``fetcher`` — plain HTTP, ``dynamic`` —
headless browser, ``stealthy`` — anti-detection browser) and an ``auto``
mode that cascades through them on block detection. Optional
:class:`~loom.store.cookies.CookieStore` enables cookie-based auth retry.
Requires the ``[scrape]`` optional extra.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlparse

from loom.scrape.base import ScrapeProviderError, ScrapeResult
from loom.store.cookies import CookieStore

logger = logging.getLogger(__name__)

_BLOCK_PATTERNS = re.compile(
    r"cf-challenge|checking your browser|please wait"
    r"|are you a robot|cf-browser-verification"
    r"|just a moment|enable javascript"
    r"|attention required|ray id",
    re.IGNORECASE,
)

_AUTH_PATTERNS = re.compile(
    r'<form[^>]*(?:login|signin|sign-in|password|authenticate)'
    r'|["\'](?:sign[_ ]?in|log[_ ]?in|authenticate)["\']'
    r"|unauthorized|access denied",
    re.IGNORECASE,
)


def _extract_domain(url: str) -> str:
    """Extract the hostname from a URL."""
    return urlparse(url).hostname or ""


def _looks_like_block(content: str, status_code: int | None) -> bool:
    """Detect anti-bot / Cloudflare challenge pages."""
    if status_code in (403, 503):
        return bool(_BLOCK_PATTERNS.search(content))
    if status_code == 200 and len(content) < 500:
        return bool(_BLOCK_PATTERNS.search(content))
    return False


def _looks_like_auth_failure(content: str, status_code: int | None) -> bool:
    """Detect login/authentication wall responses."""
    if status_code == 401:
        return True
    if status_code == 403 and not _looks_like_block(content, status_code):
        return bool(_AUTH_PATTERNS.search(content))
    if status_code in (200, 302) and len(content) < 2000:
        return bool(_AUTH_PATTERNS.search(content))
    return False


def _truncate(content: str, max_bytes: int) -> str:
    """Truncate content to a byte limit with a sentinel."""
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    return encoded[:max_bytes].decode("utf-8", errors="replace") + "\n... [truncated]"


def _extract_by_selector(html_content: str, css_selector: str | None, xpath: str | None) -> str:
    """Extract content from HTML using CSS selectors or XPath."""
    if not css_selector and not xpath:
        return html_content

    try:
        from scrapling.parser import Selector
    except ImportError as exc:
        raise ScrapeProviderError(
            "scrapling", "scrapling is not installed. Install with: pip install loom[scrape]"
        ) from exc

    sel = Selector(html_content)
    elements = []
    if css_selector:
        elements = sel.css(css_selector)
    elif xpath:
        elements = sel.xpath(xpath)

    if not elements:
        return ""

    if hasattr(elements, "getall"):
        return "\n---\n".join(str(e) for e in elements.getall())

    texts = []
    for el in elements:
        text = el.get_all_text() if hasattr(el, "get_all_text") else str(el)
        if text.strip():
            texts.append(text.strip())
    return "\n---\n".join(texts)


def _html_to_markdown(html: str) -> str:
    """Crude HTML-to-markdown conversion.

    Strips ``<style>``, ``<script>``, ``<head>`` blocks and converts
    common tags to their Markdown equivalents.
    """
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL)
    text = re.sub(r"<head>.*?</head>", "", text, flags=re.DOTALL)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</?p[^>]*>", "\n", text)
    text = re.sub(r"<h[1-6][^>]*>", "\n## ", text)
    text = re.sub(r"</h[1-6]>", "\n", text)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"<strong[^>]*>", "**", text)
    text = re.sub(r"</strong>", "**", text)
    text = re.sub(r"<em[^>]*>", "*", text)
    text = re.sub(r"</em>", "*", text)
    text = re.sub(r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>", r"[\1](", text)
    text = re.sub(r"</a>", ")", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class ScraplingProvider:
    """Scrape provider backed by the Scrapling library.

    Supports cascade fetching (``auto`` mode) and cookie-based
    authentication fallback via :class:`~loom.store.cookies.CookieStore`.
    """

    def __init__(
        self,
        *,
        mode: str = "auto",
        cookie_store: CookieStore | None = None,
        headless: bool = True,
        timeout: int = 30,
        max_content_bytes: int = 102400,
    ) -> None:
        """Configure fetcher mode, cookie store, headless flag, timeout, and max content size."""
        if mode not in ("auto", "fetcher", "dynamic", "stealthy"):
            raise ValueError(f"Invalid mode: {mode}")
        self._mode = mode
        self._cookie_store = cookie_store
        self._headless = headless
        self._timeout = timeout
        self._max_content_bytes = max_content_bytes

    @property
    def name(self) -> str:
        """Provider identifier."""
        return "scrapling"

    async def scrape(
        self,
        url: str,
        output_format: str = "text",
        css_selector: str | None = None,
        xpath: str | None = None,
    ) -> ScrapeResult:
        """Fetch URL, detect blocks/auth, retry with cookies, convert
        to the requested format."""
        result = await self._cascade_fetch(url, "html")

        if (
            _looks_like_auth_failure(result.content, result.status_code)
            and self._cookie_store
        ):
            domain = _extract_domain(url)
            if domain:
                cookies = await self._cookie_store.get_cookies(domain)
                if cookies:
                    logger.info("Auth failure detected, retrying with cookies for %s", domain)
                    retry = await self._cascade_fetch(url, "html", cookies=cookies)
                    if not _looks_like_auth_failure(retry.content, retry.status_code):
                        if retry.cookies:
                            await self._cookie_store.save_cookies(domain, retry.cookies)
                        result = retry

        if (
            self._cookie_store
            and result.cookies
            and result.status_code in (200, 201)
        ):
            domain = _extract_domain(url)
            if domain:
                await self._cookie_store.save_cookies(domain, result.cookies)

        html_content = result.content

        if css_selector or xpath:
            result.content = _extract_by_selector(html_content, css_selector, xpath)
            result.content_type = "text"
        elif output_format != "html":
            try:
                from scrapling.parser import Selector
            except ImportError:
                pass
            else:
                sel = Selector(html_content)
                if output_format == "text":
                    result.content = sel.get_all_text()
                    result.content_type = "text"
                elif output_format == "markdown":
                    result.content = _html_to_markdown(html_content)
                    result.content_type = "markdown"

        result.content = _truncate(result.content, self._max_content_bytes)

        return result

    async def _cascade_fetch(
        self,
        url: str,
        output_format: str,
        cookies: dict[str, str] | None = None,
    ) -> ScrapeResult:
        """Auto mode: try fetcher → dynamic → stealthy; otherwise use
        the configured mode directly."""
        if self._mode != "auto":
            return await self._fetch(url, output_format, self._mode, cookies)

        result = await self._fetch(url, output_format, "fetcher", cookies)
        if not _looks_like_block(result.content, result.status_code):
            return result

        logger.info("HTTP fetch blocked, falling back to dynamic browser")
        result = await self._fetch(url, output_format, "dynamic", cookies)
        if not _looks_like_block(result.content, result.status_code):
            return result

        logger.info("Dynamic fetch blocked, falling back to stealthy browser")
        return await self._fetch(url, output_format, "stealthy", cookies)

    async def _fetch(
        self,
        url: str,
        output_format: str,
        mode: str,
        cookies: dict[str, str] | None = None,
    ) -> ScrapeResult:
        """Execute a single fetch using the specified Scrapling fetcher class."""
        try:
            if mode == "fetcher":
                from scrapling.fetchers import Fetcher

                page = await asyncio.to_thread(
                    Fetcher.get, url, timeout=self._timeout, cookies=cookies
                )
            elif mode == "dynamic":
                from scrapling.fetchers import DynamicFetcher

                page = await asyncio.to_thread(
                    DynamicFetcher.fetch,
                    url,
                    headless=self._headless,
                    timeout=self._timeout,
                    cookies=cookies,
                )
            elif mode == "stealthy":
                from scrapling.fetchers import StealthyFetcher

                page = await asyncio.to_thread(
                    StealthyFetcher.fetch,
                    url,
                    headless=self._headless,
                    timeout=self._timeout,
                    cookies=cookies,
                )
            else:
                raise ScrapeProviderError("scrapling", f"Unknown mode: {mode}")
        except ImportError as exc:
            raise ScrapeProviderError(
                "scrapling",
                "scrapling is not installed. Install with: pip install loom[scrape]",
            ) from exc
        except Exception as exc:
            msg = str(exc)
            status = None
            if hasattr(exc, "status_code"):
                status = exc.status_code
            raise ScrapeProviderError("scrapling", msg, status_code=status) from exc

        if hasattr(page, "body") and isinstance(page.body, bytes):
            content = page.body.decode("utf-8", errors="replace")
        else:
            content = str(page)
        resp_cookies = None
        if hasattr(page, "cookies") and page.cookies:
            resp_cookies = (
                dict(page.cookies)
                if not isinstance(page.cookies, dict)
                else page.cookies
            )

        content = _truncate(content, self._max_content_bytes)

        return ScrapeResult(
            url=url,
            content=content,
            content_type="html",
            status_code=getattr(page, "status", None),
            cookies=resp_cookies,
        )
