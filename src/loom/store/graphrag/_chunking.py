"""Markdown chunking utilities for GraphRAG.

Splits markdown text into overlapping chunks based on heading boundaries.
"""

from __future__ import annotations

import hashlib
import re

from loom.store.graphrag._types import Chunk


def _make_chunk_id(source_path: str, offset: int) -> str:
    raw = f"{source_path}:{offset}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


_FENCE_RE = re.compile(r"^(`{3,}|~{3,})", re.MULTILINE)


def _split_paragraphs_safe(content: str) -> list[str]:
    """Split content on blank lines, but treat fenced code blocks as
    indivisible. A blank line *inside* a fenced block is not a split point.

    This prevents the entity extractor from being fed a chunk that starts
    or ends mid-code, which is the main source of garbage entity names
    like ``int pk PK\\n        int schedule_id FK`` (mermaid ER diagrams)
    and ``Issues](https://github.com/...`` (URL fragments).
    """
    lines = content.split("\n")
    blocks: list[str] = []
    buf: list[str] = []
    in_fence = False
    fence_marker = ""

    def flush() -> None:
        if buf:
            blocks.append("\n".join(buf).strip())
            buf.clear()

    for line in lines:
        stripped = line.lstrip()
        if not in_fence and _FENCE_RE.match(stripped):
            # Starting a new fenced block — close prior paragraph first.
            flush()
            in_fence = True
            fence_marker = stripped[:3]
            buf.append(line)
            continue
        if in_fence:
            buf.append(line)
            if stripped.startswith(fence_marker):
                # Closing fence — keep the whole block as one paragraph.
                flush()
                in_fence = False
                fence_marker = ""
            continue
        if line.strip() == "":
            flush()
        else:
            buf.append(line)
    flush()
    return [b for b in blocks if b]


def chunk_markdown(
    text: str,
    source_path: str,
    *,
    max_size: int = 1000,
    overlap: int = 100,
) -> list[Chunk]:
    if not text.strip():
        return []
    heading_re = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
    splits: list[tuple[str, int]] = []
    last = 0
    for m in heading_re.finditer(text):
        if m.start() > last:
            splits.append((text[last : m.start()], last))
        last = m.start()
    if last < len(text):
        splits.append((text[last:], last))

    if not splits:
        splits = [(text, 0)]

    merged: list[tuple[str, int]] = []
    for content, offset in splits:
        starts_with_heading = bool(re.match(r"^#{1,6}\s+", content))
        if merged and len(content) < 100 and not starts_with_heading:
            prev_content, prev_offset = merged[-1]
            merged[-1] = (prev_content + "\n" + content, prev_offset)
        elif len(content) > max_size:
            paragraphs = _split_paragraphs_safe(content)
            buf = ""
            buf_offset = offset
            for para in paragraphs:
                if buf and len(buf) + len(para) + 2 > max_size:
                    merged.append((buf, buf_offset))
                    buf_offset = max(offset, buf_offset + len(buf) - overlap)
                    buf = para
                else:
                    buf = buf + "\n\n" + para if buf else para
            if buf:
                merged.append((buf, buf_offset))
        else:
            merged.append((content, offset))

    chunks: list[Chunk] = []
    for content, offset in merged:
        content = content.strip()
        if not content:
            continue
        heading_match = re.match(r"^#{1,6}\s+(.+)$", content, re.MULTILINE)
        heading = heading_match.group(1).strip() if heading_match else ""
        chunks.append(
            Chunk(
                id=_make_chunk_id(source_path, offset),
                source_path=source_path,
                heading=heading,
                content=content,
                char_offset=offset,
            )
        )
    return chunks
