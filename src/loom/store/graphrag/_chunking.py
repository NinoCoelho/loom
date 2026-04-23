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
            paragraphs = re.split(r"\n\n+", content)
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
