from __future__ import annotations

import httpx

from loom.types import ToolSpec
from loom.tools.base import ToolHandler, ToolResult


class HttpCallTool(ToolHandler):
    def __init__(
        self,
        base_headers: dict | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = 10240,
    ) -> None:
        self._base_headers = base_headers or {}
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes

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

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                if method == "GET":
                    resp = await client.get(url, headers=headers)
                elif method == "POST":
                    resp = await client.post(url, headers=headers, content=body)
                else:
                    return ToolResult(text=f"Unsupported method: {method}")

                text = resp.text
                if len(text) > self._max_response_bytes:
                    text = text[: self._max_response_bytes] + "\n... [truncated]"
                return ToolResult(
                    text=text,
                    metadata={"status_code": resp.status_code},
                )
        except Exception as e:
            return ToolResult(text=f"HTTP error: {e}")
