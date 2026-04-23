"""Shared utility functions for tool implementations."""

from __future__ import annotations


def truncate_text(
    text: str,
    max_bytes: int,
    marker: str = "\n... [truncated]",
) -> tuple[str, bool]:
    """Truncate *text* to *max_bytes* bytes (UTF-8), appending *marker* if cut.

    Returns ``(truncated_text, was_truncated)``.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    truncated = encoded[:max_bytes].decode("utf-8", errors="replace")
    return truncated + marker, True
