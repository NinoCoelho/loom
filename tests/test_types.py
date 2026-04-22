from loom.tools.base import ToolResult
from loom.types import (
    ChatMessage,
    ContentPart,
    FilePart,
    ImagePart,
    Role,
    TextPart,
    VideoPart,
)


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

    def test_content_parts_default_none(self):
        r = ToolResult(text="ok")
        assert r.content_parts is None

    def test_content_parts_with_images(self):
        img = ImagePart(source="/tmp/test.png", media_type="image/png")
        r = ToolResult(text="screenshot", content_parts=[img])
        assert len(r.content_parts) == 1
        assert r.content_parts[0].type == "image"


class TestContentPart:
    def test_text_part(self):
        p = TextPart(text="hello")
        assert p.type == "text"
        assert p.text == "hello"

    def test_image_part(self):
        p = ImagePart(source="/path/to/img.png", media_type="image/png")
        assert p.type == "image"
        assert p.source == "/path/to/img.png"

    def test_video_part(self):
        p = VideoPart(source="/path/to/vid.mp4", media_type="video/mp4")
        assert p.type == "video"

    def test_file_part(self):
        p = FilePart(source="/path/to/doc.pdf", media_type="application/pdf")
        assert p.type == "file"


class TestChatMessageContent:
    def test_str_content_backward_compat(self):
        msg = ChatMessage(role=Role.USER, content="hello")
        assert msg.content == "hello"
        assert msg.text_content == "hello"

    def test_none_content(self):
        msg = ChatMessage(role=Role.ASSISTANT, content=None)
        assert msg.content is None
        assert msg.text_content is None

    def test_list_content_text_only(self):
        msg = ChatMessage(
            role=Role.USER,
            content=[TextPart(text="describe this"), ImagePart(source="/img.png")],
        )
        assert isinstance(msg.content, list)
        assert msg.text_content == "describe this"

    def test_list_content_no_text_parts(self):
        msg = ChatMessage(
            role=Role.USER,
            content=[ImagePart(source="/img.png")],
        )
        assert msg.text_content is None

    def test_list_content_multiple_text_parts(self):
        msg = ChatMessage(
            role=Role.USER,
            content=[TextPart(text="hello"), TextPart(text="world")],
        )
        assert msg.text_content == "hello world"

    def test_roundtrip_model_dump_list_content(self):
        msg = ChatMessage(
            role=Role.USER,
            content=[TextPart(text="hi"), ImagePart(source="/x.png", media_type="image/png")],
        )
        dumped = msg.model_dump()
        restored = ChatMessage.model_validate(dumped)
        assert isinstance(restored.content, list)
        assert len(restored.content) == 2
        assert restored.content[0].text == "hi"
        assert restored.content[1].source == "/x.png"
