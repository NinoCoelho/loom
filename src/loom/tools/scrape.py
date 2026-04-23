"""``web_scrape`` tool — web scraping exposed to the LLM.

Wraps a :class:`~loom.scrape.base.ScrapeProvider`. Supports text, markdown,
and HTML output with optional CSS/XPath extraction.
"""

from __future__ import annotations

from loom.scrape.base import ScrapeProvider, ScrapeProviderError
from loom.scrape.scrapling import ScraplingProvider
from loom.store.cookies import CookieStore
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec


class WebScrapeTool(ToolHandler):
    """:class:`~loom.tools.base.ToolHandler` wrapping a
    :class:`~loom.scrape.base.ScrapeProvider` for web scraping."""

    def __init__(self, provider: ScrapeProvider) -> None:
        """Wrap a scrape provider."""
        self._provider = provider

    @property
    def tool(self) -> ToolSpec:
        """Tool spec: ``web_scrape`` with ``url``, ``output_format``,
        ``css_selector``, and ``xpath`` parameters."""
        return ToolSpec(
            name="web_scrape",
            description=(
                "Scrape a web page and extract its content. "
                "Supports text, markdown, and HTML output formats. "
                "Use css_selector or xpath to extract specific elements."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to scrape",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": ["text", "markdown", "html"],
                        "description": "Output format (default: text)",
                    },
                    "css_selector": {
                        "type": "string",
                        "description": "CSS selector to extract specific elements",
                    },
                    "xpath": {
                        "type": "string",
                        "description": "XPath expression to extract specific elements",
                    },
                },
                "required": ["url"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        """Scrape the URL and return the extracted content."""
        url = args.get("url", "")
        if not url:
            return ToolResult(text="Error: url is required", is_error=True)

        output_format = args.get("output_format", "text")
        css_selector = args.get("css_selector")
        xpath = args.get("xpath")

        try:
            result = await self._provider.scrape(
                url,
                output_format=output_format,
                css_selector=css_selector,
                xpath=xpath,
            )
        except ScrapeProviderError as exc:
            return ToolResult(text=f"Scrape error: {exc}", is_error=True)

        metadata = {
            "url": result.url,
            "content_type": result.content_type,
            "provider": self._provider.name,
        }
        if result.status_code is not None:
            metadata["status_code"] = result.status_code

        return ToolResult(text=result.content, metadata=metadata)

    @classmethod
    def from_config(
        cls,
        *,
        mode: str = "auto",
        cookie_store: CookieStore | None = None,
        headless: bool = True,
        timeout: int = 30,
        max_content_bytes: int = 102400,
    ) -> WebScrapeTool:
        """Factory that creates a
        :class:`~loom.scrape.scrapling.ScraplingProvider`."""
        provider = ScraplingProvider(
            mode=mode,
            cookie_store=cookie_store,
            headless=headless,
            timeout=timeout,
            max_content_bytes=max_content_bytes,
        )
        return cls(provider)
