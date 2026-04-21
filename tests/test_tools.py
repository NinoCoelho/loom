import json

import pytest

from loom.tools.base import ToolHandler, ToolResult
from loom.tools.delegate import DelegateTool
from loom.tools.memory import MemoryToolHandler
from loom.tools.profile import EditIdentityTool
from loom.types import ToolSpec


class EchoTool(ToolHandler):
    @property
    def tool(self):
        return ToolSpec(
            name="echo",
            description="echo",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )

    async def invoke(self, args):
        return ToolResult(text=args.get("text", ""))


class TestToolRegistry:
    def test_register_and_dispatch(self):
        from loom.tools.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(EchoTool())
        assert reg.has("echo")
        assert "echo" in reg.list_handlers()

    @pytest.mark.asyncio
    async def test_dispatch(self):
        from loom.tools.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(EchoTool())
        result = await reg.dispatch("echo", {"text": "hello"})
        assert result.to_text() == "hello"

    @pytest.mark.asyncio
    async def test_dispatch_unknown(self):
        from loom.tools.registry import ToolRegistry

        reg = ToolRegistry()
        result = await reg.dispatch("unknown", {})
        assert "Unknown tool" in result.to_text()

    def test_specs(self):
        from loom.tools.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(EchoTool())
        specs = reg.specs()
        assert len(specs) == 1
        assert specs[0].name == "echo"

    def test_unregister(self):
        from loom.tools.registry import ToolRegistry

        reg = ToolRegistry()
        reg.register(EchoTool())
        reg.unregister("echo")
        assert not reg.has("echo")


class TestEditIdentityTool:
    @pytest.mark.asyncio
    async def test_edit_user_allowed(self, agent_home, perms_full):
        tool = EditIdentityTool(agent_home, perms_full)
        result = await tool.invoke({"file": "user", "content": "# Updated"})
        assert "Updated" in result.to_text()
        assert "Updated" in agent_home.read_user()

    @pytest.mark.asyncio
    async def test_edit_soul_denied(self, agent_home, perms_default):
        tool = EditIdentityTool(agent_home, perms_default)
        result = await tool.invoke({"file": "soul", "content": "hack"})
        assert "denied" in result.to_text()

    @pytest.mark.asyncio
    async def test_edit_identity_allowed(self, agent_home, perms_full):
        tool = EditIdentityTool(agent_home, perms_full)
        result = await tool.invoke({"file": "identity", "content": "# New identity"})
        assert "Updated" in result.to_text()

    @pytest.mark.asyncio
    async def test_missing_file_param(self, agent_home, perms_default):
        tool = EditIdentityTool(agent_home, perms_default)
        result = await tool.invoke({"content": "test"})
        assert "missing" in result.to_text()


class TestMemoryToolHandler:
    @pytest.mark.asyncio
    async def test_write_and_read(self, memory_dir, memory_index):
        from loom.store.memory import MemoryStore

        store = MemoryStore(memory_dir, memory_index)
        try:
            tool = MemoryToolHandler(store)

            r = await tool.invoke(
                {"action": "write", "key": "test", "content": "hello", "category": "notes"}
            )
            assert "Wrote" in r.to_text()

            r = await tool.invoke({"action": "read", "key": "test"})
            assert "hello" in r.to_text()
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_search(self, memory_dir, memory_index):
        from loom.store.memory import MemoryStore

        store = MemoryStore(memory_dir, memory_index)
        try:
            tool = MemoryToolHandler(store)

            await tool.invoke(
                {
                    "action": "write",
                    "key": "grpc",
                    "content": "gRPC microservices",
                    "category": "notes",
                }
            )
            r = await tool.invoke({"action": "search", "query": "gRPC"})
            assert "grpc" in r.to_text()
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_list(self, memory_dir, memory_index):
        from loom.store.memory import MemoryStore

        store = MemoryStore(memory_dir, memory_index)
        try:
            tool = MemoryToolHandler(store)

            await tool.invoke({"action": "write", "key": "a", "content": "A", "category": "notes"})
            await tool.invoke({"action": "write", "key": "b", "content": "B", "category": "notes"})
            r = await tool.invoke({"action": "list"})
            data = json.loads(r.to_text())
            assert len(data) >= 2
        finally:
            store.close()

    @pytest.mark.asyncio
    async def test_delete(self, memory_dir, memory_index):
        from loom.store.memory import MemoryStore

        store = MemoryStore(memory_dir, memory_index)
        try:
            tool = MemoryToolHandler(store)

            await tool.invoke({"action": "write", "key": "del-me", "content": "temp"})
            r = await tool.invoke({"action": "delete", "key": "del-me"})
            assert "Deleted" in r.to_text()
        finally:
            store.close()


class TestDelegateTool:
    @pytest.mark.asyncio
    async def test_unknown_agent(self, tmp_dir):
        from loom.runtime import AgentRuntime

        runtime = AgentRuntime(tmp_dir / "loom")
        try:
            dt = DelegateTool(runtime)
            result = await dt.invoke({"agent": "ghost", "message": "hello"})
            assert "not found" in result.to_text()
        finally:
            runtime.close()

    @pytest.mark.asyncio
    async def test_lists_available_agents(self, tmp_dir):
        from loom.loop import AgentConfig
        from loom.runtime import AgentRuntime

        runtime = AgentRuntime(tmp_dir / "loom")
        try:
            runtime.create_agent("helper", AgentConfig(max_iterations=1))
            dt = DelegateTool(runtime)
            result = await dt.invoke({"agent": "nonexistent", "message": "hi"})
            assert "helper" in result.to_text()
        finally:
            runtime.close()
