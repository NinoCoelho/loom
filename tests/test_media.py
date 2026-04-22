import pytest

from loom.media import infer_media_type


class TestInferMediaType:
    def test_png(self):
        assert infer_media_type("photo.png") == "image/png"

    def test_jpg(self):
        assert infer_media_type("photo.jpg") == "image/jpeg"

    def test_jpeg(self):
        assert infer_media_type("photo.jpeg") == "image/jpeg"

    def test_gif(self):
        assert infer_media_type("anim.gif") == "image/gif"

    def test_webp(self):
        assert infer_media_type("pic.webp") == "image/webp"

    def test_mp4(self):
        assert infer_media_type("clip.mp4") == "video/mp4"

    def test_webm(self):
        assert infer_media_type("clip.webm") == "video/webm"

    def test_pdf(self):
        assert infer_media_type("doc.pdf") == "application/pdf"

    def test_url_with_extension(self):
        assert infer_media_type("https://example.com/img.png?token=abc") == "image/png"

    def test_url_with_fragment(self):
        assert infer_media_type("https://example.com/img.jpg#section") == "image/jpeg"

    def test_unknown_extension(self):
        mt = infer_media_type("file.xyz1234567890")
        assert mt == "application/octet-stream"

    def test_absolute_path(self):
        assert infer_media_type("/tmp/screenshot.png") == "image/png"
