"""Data classes shared across the memory store sub-modules."""

from __future__ import annotations

from pathlib import Path


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


class MemoryEntry:
    __slots__ = (
        "key",
        "category",
        "tags",
        "content",
        "created",
        "updated",
        "path",
        "pinned",
        "importance",
        "access_count",
        "last_recalled_at",
    )

    def __init__(
        self,
        key: str,
        category: str = "notes",
        tags: list[str] | None = None,
        content: str = "",
        created: str | None = None,
        updated: str | None = None,
        path: Path | None = None,
        pinned: bool = False,
        importance: int = 1,
        access_count: int = 0,
        last_recalled_at: str | None = None,
    ) -> None:
        self.key = key
        self.category = category
        self.tags = tags or []
        self.content = content
        self.created = created or _utc_now_iso()
        self.updated = updated or self.created
        self.path = path
        self.pinned = pinned
        self.importance = max(0, min(3, importance))
        self.access_count = access_count
        self.last_recalled_at = last_recalled_at


class SearchHit:
    __slots__ = ("key", "category", "snippet", "score")

    def __init__(self, key: str, category: str, snippet: str, score: float) -> None:
        self.key = key
        self.category = category
        self.snippet = snippet
        self.score = score


class RecallHit:
    __slots__ = ("key", "category", "preview", "score", "components")

    def __init__(
        self,
        key: str,
        category: str,
        preview: str,
        score: float,
        components: dict[str, float],
    ) -> None:
        self.key = key
        self.category = category
        self.preview = preview
        self.score = score
        self.components = components
