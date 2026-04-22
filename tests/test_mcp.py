"""Tests for the MCP client integration."""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from loom.mcp import McpClient, McpServerConfig, McpToolHandler
from loom.tools.base import ToolResult
from loom.types import ToolSpec

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_tool_stub(name: str, description: str, schema: dict) -> MagicMock:
    """Build a mock object resembling an mcp.types.Tool."""
    t = MagicMock()
    t.name = name
    t.description = description
    t.inputSchema = schema
    return t


def _make_content(text: str) -> MagicMock:
    c = MagicMock()
    c.text = text
    return c


def _make_call_result(text: str, is_error: bool = False) -> MagicMock:
    r = MagicMock()
    r.content = [_make_content(text)]
    r.isError = is_error
    return r


# ── McpServerConfig ───────────────────────────────────────────────────────────


class TestMcpServerConfig:
    def test_defaults(self) -> None:
        cfg = McpServerConfig(name="test")
        assert cfg.transport == "stdio"
        assert cfg.command is None
        assert cfg.url is None
        assert cfg.env == {}
        assert cfg.headers == {}

    def test_stdio_config(self) -> None:
        cfg = McpServerConfig(name="s", transport="stdio", command=["node", "server.js"])
        assert cfg.command == ["node", "server.js"]

    def test_sse_config(self) -> None:
        cfg = McpServerConfig(name="s", transport="sse", url="http://localhost:3000/sse")
        assert cfg.url == "http://localhost:3000/sse"


# ── McpToolHandler ────────────────────────────────────────────────────────────


class TestMcpToolHandler:
    def _make_handler(self) -> tuple[McpToolHandler, AsyncMock]:
        call_fn = AsyncMock(return_value=ToolResult(text="ok"))
        schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
        handler = McpToolHandler(
            name="my_tool",
            description="Does something",
            input_schema=schema,
            call_fn=call_fn,
        )
        return handler, call_fn

    def test_tool_spec(self) -> None:
        handler, _ = self._make_handler()
        spec = handler.tool
        assert isinstance(spec, ToolSpec)
        assert spec.name == "my_tool"
        assert spec.description == "Does something"
        assert spec.parameters["type"] == "object"

    @pytest.mark.asyncio
    async def test_invoke_delegates_to_call_fn(self) -> None:
        handler, call_fn = self._make_handler()
        result = await handler.invoke({"x": "hello"})
        call_fn.assert_awaited_once_with("my_tool", {"x": "hello"})
        assert result.text == "ok"

    @pytest.mark.asyncio
    async def test_invoke_propagates_error_flag(self) -> None:
        call_fn = AsyncMock(return_value=ToolResult(text="boom", is_error=True))
        handler = McpToolHandler("t", "t", {}, call_fn)
        result = await handler.invoke({})
        assert result.is_error is True


# ── McpClient ─────────────────────────────────────────────────────────────────


def _patch_mcp(session_mock: MagicMock) -> patch:
    """Patch the mcp package so McpClient uses our fake session."""

    fake_mcp = ModuleType("mcp")
    fake_mcp.ClientSession = MagicMock(return_value=session_mock)  # type: ignore[attr-defined]

    # stdio transport context manager: returns (read, write)
    fake_stdio = ModuleType("mcp.client.stdio")
    fake_stdio.StdioServerParameters = MagicMock()  # type: ignore[attr-defined]
    transport_cm = MagicMock()
    transport_cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
    transport_cm.__aexit__ = AsyncMock(return_value=None)
    fake_stdio.stdio_client = MagicMock(return_value=transport_cm)  # type: ignore[attr-defined]

    # sse transport context manager
    fake_sse = ModuleType("mcp.client.sse")
    sse_transport_cm = MagicMock()
    sse_transport_cm.__aenter__ = AsyncMock(return_value=(MagicMock(), MagicMock()))
    sse_transport_cm.__aexit__ = AsyncMock(return_value=None)
    fake_sse.sse_client = MagicMock(return_value=sse_transport_cm)  # type: ignore[attr-defined]

    return patch.dict(
        sys.modules,
        {
            "mcp": fake_mcp,
            "mcp.client": ModuleType("mcp.client"),
            "mcp.client.stdio": fake_stdio,
            "mcp.client.sse": fake_sse,
        },
    )


def _make_session() -> MagicMock:
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    session.initialize = AsyncMock()
    return session


class TestMcpClient:
    @pytest.mark.asyncio
    async def test_list_tools_stdio(self) -> None:
        session = _make_session()
        tools_result = MagicMock()
        tools_result.tools = [
            _make_tool_stub(
                "search",
                "Search the web",
                {"type": "object", "properties": {"query": {"type": "string"}}},
            )
        ]
        session.list_tools = AsyncMock(return_value=tools_result)

        cfg = McpServerConfig(name="web", transport="stdio", command=["npx", "web-mcp"])
        with _patch_mcp(session):
            async with McpClient(cfg) as client:
                handlers = await client.list_tools()

        assert len(handlers) == 1
        assert handlers[0].tool.name == "search"
        assert handlers[0].tool.description == "Search the web"

    @pytest.mark.asyncio
    async def test_list_tools_sse(self) -> None:
        session = _make_session()
        tools_result = MagicMock()
        tools_result.tools = [_make_tool_stub("ping", "Ping", {"type": "object"})]
        session.list_tools = AsyncMock(return_value=tools_result)

        cfg = McpServerConfig(name="remote", transport="sse", url="http://localhost:8080/sse")
        with _patch_mcp(session):
            async with McpClient(cfg) as client:
                handlers = await client.list_tools()

        assert handlers[0].tool.name == "ping"

    @pytest.mark.asyncio
    async def test_call_tool_returns_text(self) -> None:
        session = _make_session()
        session.call_tool = AsyncMock(return_value=_make_call_result("result text"))
        session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

        cfg = McpServerConfig(name="s", transport="stdio", command=["server"])
        with _patch_mcp(session):
            async with McpClient(cfg) as client:
                result = await client.call_tool("my_tool", {"a": 1})

        assert result.text == "result text"
        assert result.is_error is False

    @pytest.mark.asyncio
    async def test_call_tool_error_flag(self) -> None:
        session = _make_session()
        session.call_tool = AsyncMock(return_value=_make_call_result("err", is_error=True))
        session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

        cfg = McpServerConfig(name="s", transport="stdio", command=["server"])
        with _patch_mcp(session):
            async with McpClient(cfg) as client:
                result = await client.call_tool("bad_tool", {})

        assert result.is_error is True

    @pytest.mark.asyncio
    async def test_multiple_content_blocks_joined(self) -> None:
        session = _make_session()
        call_result = MagicMock()
        call_result.content = [_make_content("part1"), _make_content("part2")]
        call_result.isError = False
        session.call_tool = AsyncMock(return_value=call_result)
        session.list_tools = AsyncMock(return_value=MagicMock(tools=[]))

        cfg = McpServerConfig(name="s", transport="stdio", command=["server"])
        with _patch_mcp(session):
            async with McpClient(cfg) as client:
                result = await client.call_tool("tool", {})

        assert result.text == "part1\npart2"

    @pytest.mark.asyncio
    async def test_tool_handler_invoke_calls_session(self) -> None:
        session = _make_session()
        tools_result = MagicMock()
        schema = {"type": "object", "properties": {"q": {"type": "string"}}}
        tools_result.tools = [_make_tool_stub("search", "Search", schema)]
        session.list_tools = AsyncMock(return_value=tools_result)
        session.call_tool = AsyncMock(return_value=_make_call_result("found it"))

        cfg = McpServerConfig(name="s", transport="stdio", command=["server"])
        with _patch_mcp(session):
            async with McpClient(cfg) as client:
                handlers = await client.list_tools()
                result = await handlers[0].invoke({"q": "hello"})

        session.call_tool.assert_awaited_once_with("search", {"q": "hello"})
        assert result.text == "found it"

    @pytest.mark.asyncio
    async def test_missing_command_raises(self) -> None:
        session = _make_session()
        cfg = McpServerConfig(name="s", transport="stdio")  # no command
        with _patch_mcp(session):
            with pytest.raises(ValueError, match="command"):
                async with McpClient(cfg):
                    pass

    @pytest.mark.asyncio
    async def test_missing_url_raises(self) -> None:
        session = _make_session()
        cfg = McpServerConfig(name="s", transport="sse")  # no url
        with _patch_mcp(session):
            with pytest.raises(ValueError, match="url"):
                async with McpClient(cfg):
                    pass

    def test_assert_open_raises_when_not_started(self) -> None:
        cfg = McpServerConfig(name="s", transport="stdio", command=["server"])
        client = McpClient(cfg)
        with pytest.raises(RuntimeError, match="context manager"):
            client._assert_open()

    @pytest.mark.asyncio
    async def test_missing_mcp_package_raises_import_error(self) -> None:
        cfg = McpServerConfig(name="s", transport="stdio", command=["server"])
        with patch.dict(
            sys.modules,
            {"mcp": None, "mcp.client.stdio": None, "mcp.client.sse": None},
        ):
            with pytest.raises(ImportError, match="loom\\[mcp\\]"):
                async with McpClient(cfg):
                    pass
