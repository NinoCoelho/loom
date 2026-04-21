"""Tests for HttpCallTool (loom.tools.http).

Covers default transport behavior and the optional pre_request_hook
introduced in RFC 0001. Uses httpx.MockTransport to avoid real network I/O.
"""

from __future__ import annotations

import httpx
import pytest

from loom.tools.http import HttpCallTool


def _mock_client_factory(transport: httpx.MockTransport):
    """Monkeypatch target: replaces httpx.AsyncClient to inject our transport."""
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


async def test_get_default_behavior_unchanged(patch_client):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "https://example.com/foo"
        return httpx.Response(200, text="hello")

    patch_client(handler)
    tool = HttpCallTool()
    result = await tool.invoke({"method": "GET", "url": "https://example.com/foo"})
    assert result.text == "hello"
    assert result.metadata == {"status_code": 200}


async def test_base_headers_applied(patch_client):
    seen_headers: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(request.headers)
        return httpx.Response(200, text="ok")

    patch_client(handler)
    tool = HttpCallTool(base_headers={"X-Client": "loom"})
    await tool.invoke({"method": "GET", "url": "https://example.com/"})
    assert seen_headers.get("x-client") == "loom"


async def test_hook_rewrites_url(patch_client):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="rewritten")

    patch_client(handler)

    async def rewrite(req: dict) -> dict:
        return {**req, "url": "https://real.example.com/resolved"}

    tool = HttpCallTool(pre_request_hook=rewrite)
    result = await tool.invoke({"method": "GET", "url": "target://foo/bar"})
    assert calls == ["https://real.example.com/resolved"]
    assert result.text == "rewritten"


async def test_hook_injects_auth_header(patch_client):
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("authorization", ""))
        return httpx.Response(200, text="ok")

    patch_client(handler)

    async def add_bearer(req: dict) -> dict:
        return {
            **req,
            "headers": {**req["headers"], "Authorization": "Bearer abc123"},
        }

    tool = HttpCallTool(pre_request_hook=add_bearer)
    await tool.invoke({"method": "GET", "url": "https://example.com/"})
    assert seen_auth == ["Bearer abc123"]


async def test_hook_exception_becomes_tool_error(patch_client):
    dispatched: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        dispatched.append(request)
        return httpx.Response(200, text="should not run")

    patch_client(handler)

    async def denied(req: dict) -> dict:
        raise PermissionError("policy denies this scope")

    tool = HttpCallTool(pre_request_hook=denied)
    result = await tool.invoke({"method": "GET", "url": "https://example.com/"})
    assert result.text.startswith("HTTP error:")
    assert "policy denies" in result.text
    assert dispatched == []  # request was cancelled before reaching the wire


async def test_hook_preserves_unreturned_fields(patch_client):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content.decode() if request.content else None
        return httpx.Response(200, text="ok")

    patch_client(handler)

    async def only_url(req: dict) -> dict:
        # Return only the changed key; url override is enough, don't touch method/body.
        return {**req, "url": "https://new.example.com/"}

    tool = HttpCallTool(pre_request_hook=only_url)
    await tool.invoke(
        {"method": "POST", "url": "https://old.example.com/", "body": "payload"}
    )
    assert seen["method"] == "POST"
    assert seen["body"] == "payload"


async def test_hook_can_change_method(patch_client):
    seen_methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_methods.append(request.method)
        return httpx.Response(200, text="ok")

    patch_client(handler)

    async def upgrade_to_post(req: dict) -> dict:
        return {**req, "method": "POST", "body": "generated"}

    tool = HttpCallTool(pre_request_hook=upgrade_to_post)
    await tool.invoke({"method": "GET", "url": "https://example.com/"})
    assert seen_methods == ["POST"]


async def test_no_hook_matches_legacy_behavior(patch_client):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, text="created")

    patch_client(handler)
    tool = HttpCallTool()  # pre_request_hook default = None
    result = await tool.invoke(
        {"method": "POST", "url": "https://example.com/", "body": "x"}
    )
    assert result.text == "created"
    assert result.metadata == {"status_code": 201}
