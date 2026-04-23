"""Media file handling utilities.

Provides functions for inferring media types from file extensions or URLs,
loading file content (local or remote), and encoding to base64 or data URLs.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

_EXT_MEDIA_MAP: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".html": "text/html",
    ".md": "text/markdown",
}


def infer_media_type(source: str) -> str:
    if source.startswith(("http://", "https://")):
        path_part = source.split("?")[0].split("#")[0]
        ext = Path(path_part).suffix.lower()
        if ext in _EXT_MEDIA_MAP:
            return _EXT_MEDIA_MAP[ext]
        guessed, _ = mimetypes.guess_type(source)
        return guessed or "application/octet-stream"
    ext = Path(source).suffix.lower()
    if ext in _EXT_MEDIA_MAP:
        return _EXT_MEDIA_MAP[ext]
    guessed, _ = mimetypes.guess_type(source)
    return guessed or "application/octet-stream"


def load_file_bytes(source: str) -> tuple[bytes, str]:
    if source.startswith(("http://", "https://")):
        import httpx

        resp = httpx.get(source, follow_redirects=True, timeout=30.0)
        resp.raise_for_status()
        media_type = resp.headers.get("content-type", "").split(";")[0].strip()
        if not media_type:
            media_type = infer_media_type(source)
        return resp.content, media_type
    p = Path(source)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {source}")
    data = p.read_bytes()
    media_type = infer_media_type(source)
    return data, media_type


def encode_to_data_url(source: str, media_type: str | None = None) -> str:
    raw_bytes, resolved_mt = load_file_bytes(source)
    mt = media_type or resolved_mt
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{mt};base64,{b64}"


def encode_to_base64(source: str, media_type: str | None = None) -> tuple[str, str]:
    raw_bytes, resolved_mt = load_file_bytes(source)
    mt = media_type or resolved_mt
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    return b64, mt
