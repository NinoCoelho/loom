"""HTTP call tool — make GET/POST requests from the agent loop.

Wraps ``httpx.AsyncClient`` with configurable base headers, timeout,
max response size, and an optional pre-request hook for credential
injection or URL rewriting.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx

from loom.tools.base import ToolHandler, ToolResult
from loom.tools.utils import truncate_text
from loom.types import ToolSpec

# Hook signature: takes the effective request dict, returns a (possibly
# modified) request dict of the same shape. Keys: method, url, headers, body.
# Consumers use this to inject credentials, rewrite URLs (e.g. a
# target://name scheme → base_url), or enforce policy checks before the
# request goes on the wire. Raise to cancel the request — the error
# surfaces as ToolResult(text="HTTP error: <msg>"), same envelope as a
# transport error.
PreRequestHook = Callable[[dict], Awaitable[dict]]


class HttpCallTool(ToolHandler):
    def __init__(
        self,
        base_headers: dict | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = 10240,
        pre_request_hook: PreRequestHook | None = None,
    ) -> None:
        self._base_headers = base_headers or {}
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._pre_request_hook = pre_request_hook

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="http_call",
            description="Make an HTTP request and return the response body.",
            parameters={
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST"],
                        "description": "HTTP method",
                    },
                    "url": {
                        "type": "string",
                        "description": "URL to request",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Optional request headers",
                    },
                    "body": {
                        "type": "string",
                        "description": "Optional request body (POST)",
                    },
                },
                "required": ["method", "url"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        method = args.get("method", "GET").upper()
        url = args.get("url", "")
        headers = {**self._base_headers, **(args.get("headers") or {})}
        body = args.get("body")

        if self._pre_request_hook is not None:
            try:
                request = await self._pre_request_hook(
                    {"method": method, "url": url, "headers": headers, "body": body}
                )
            except Exception as e:
                return ToolResult(text=f"HTTP error: {e}")
            method = request.get("method", method).upper()
            url = request.get("url", url)
            headers = request.get("headers", headers)
            body = request.get("body", body)

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                if method == "GET":
                    resp = await client.get(url, headers=headers)
                elif method == "POST":
                    resp = await client.post(url, headers=headers, content=body)
                else:
                    return ToolResult(text=f"Unsupported method: {method}")

                text, _ = truncate_text(resp.text, self._max_response_bytes)
                return ToolResult(
                    text=text,
                    metadata={"status_code": resp.status_code},
                )
        except Exception as e:
            return ToolResult(text=f"HTTP error: {e}")
