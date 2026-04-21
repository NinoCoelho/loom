from loom.tools.base import ToolResult


class TestToolResult:
    def test_text(self):
        r = ToolResult(text="hello")
        assert r.to_text() == "hello"

    def test_metadata(self):
        r = ToolResult(text="ok", metadata={"status": 200})
        assert r.metadata["status"] == 200

    def test_default_metadata(self):
        r = ToolResult(text="ok")
        assert r.metadata == {}
