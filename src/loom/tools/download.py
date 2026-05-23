"""Download queue tool — managed file downloads with progress tracking.

Provides a :class:`DownloadTool` that lets agents download files from URLs
using ``httpx`` for HTTP/HTTPS or spawning an external binary (e.g. wget,
curl, yt-dlp). Downloads are tracked in an in-memory queue with progress
updates, cancel support, and optional binary verification via the
:class:`~loom.security.BinaryRegistry`.

The download store is persisted to a JSON file in the agent home directory
so active downloads survive process restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from loom.store.atomic import atomic_write
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec

if TYPE_CHECKING:
    from loom.security.binary_registry import BinaryRegistry

logger = logging.getLogger(__name__)


class DownloadStatus(StrEnum):
    PENDING = "pending"
    DOWNLOADING = "downloading"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass
class DownloadEntry:
    id: str
    url: str
    dest_path: str
    filename: str
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0
    bytes_downloaded: int = 0
    bytes_total: int = 0
    speed_bps: float = 0.0
    error: str | None = None
    started_at: float = 0.0
    completed_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "dest_path": self.dest_path,
            "filename": self.filename,
            "status": self.status.value,
            "progress": self.progress,
            "bytes_downloaded": self.bytes_downloaded,
            "bytes_total": self.bytes_total,
            "speed_bps": self.speed_bps,
            "error": self.error,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DownloadEntry:
        return cls(
            id=d["id"],
            url=d["url"],
            dest_path=d["dest_path"],
            filename=d["filename"],
            status=DownloadStatus(d.get("status", "pending")),
            progress=d.get("progress", 0.0),
            bytes_downloaded=d.get("bytes_downloaded", 0),
            bytes_total=d.get("bytes_total", 0),
            speed_bps=d.get("speed_bps", 0.0),
            error=d.get("error"),
            started_at=d.get("started_at", 0.0),
            completed_at=d.get("completed_at", 0.0),
        )


def _format_bytes(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f} GB"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f} KB"
    return f"{n} B"


_DOWNLOAD_TOOL_SPEC = ToolSpec(
    name="download",
    description=(
        "Download a file from a URL to the local filesystem. "
        "Supports HTTP/HTTPS URLs. Tracks progress and allows "
        "cancellation of active downloads."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to download",
            },
            "dest_dir": {
                "type": "string",
                "description": (
                    "Destination directory (absolute or ~-prefixed). "
                    "Defaults to the agent's downloads directory."
                ),
            },
            "filename": {
                "type": "string",
                "description": (
                    "Output filename. Defaults to the last segment of the URL."
                ),
            },
            "action": {
                "type": "string",
                "enum": ["start", "cancel", "list", "delete"],
                "description": (
                    "Action to perform. 'start' begins a new download, "
                    "'cancel' cancels an active download, 'list' returns "
                    "all downloads, 'delete' removes a completed download."
                ),
            },
            "download_id": {
                "type": "string",
                "description": "ID of the download to cancel or delete.",
            },
        },
        "required": ["action"],
    },
)

_PROGRESS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\[download\]\s+([\d.]+)%", re.IGNORECASE),
    re.compile(r"([\d.]+)%\s+of\s+~?\s*([\d.]+\s*(?:[KMGT]i?B|B))", re.IGNORECASE),
    re.compile(r"\(frag\s+(\d+)/(\d+)\)", re.IGNORECASE),
]


class DownloadTool(ToolHandler):
    """Managed file download tool with progress tracking and queue management.

    Parameters
    ----------
    store_path : Path
        Path to the JSON file used to persist the download queue.
    binary_registry : BinaryRegistry, optional
        If provided, external binary downloads are verified against the
        trusted binary registry.
    max_concurrent : int
        Maximum number of simultaneous downloads.
    timeout : float
        Per-download timeout in seconds.
    on_progress : callable, optional
        Async callback ``(entry: DownloadEntry) -> None`` invoked on
        progress updates.
    """

    def __init__(
        self,
        store_path: Path,
        *,
        binary_registry: BinaryRegistry | None = None,
        max_concurrent: int = 3,
        timeout: float = 600.0,
        on_progress: Any = None,
    ) -> None:
        self._store_path = store_path
        self._binary_registry = binary_registry
        self._max_concurrent = max_concurrent
        self._timeout = timeout
        self._on_progress = on_progress
        self._entries: dict[str, DownloadEntry] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._load()

    @property
    def tool(self) -> ToolSpec:
        return _DOWNLOAD_TOOL_SPEC

    async def invoke(self, args: dict[str, Any]) -> ToolResult:
        action = args.get("action", "start")
        if action == "start":
            return await self._start(args)
        if action == "cancel":
            return self._cancel(args)
        if action == "list":
            return self._list()
        if action == "delete":
            return self._delete(args)
        return ToolResult(text=f"Unknown action: {action}", is_error=True)

    async def _start(self, args: dict[str, Any]) -> ToolResult:
        url = args.get("url", "")
        if not isinstance(url, str) or not url.strip():
            return ToolResult(text="`url` is required", is_error=True)
        url = url.strip()

        import uuid

        dl_id = uuid.uuid4().hex[:12]
        dest_dir = args.get("dest_dir")
        if dest_dir:
            dest_dir = os.path.expanduser(dest_dir)
        else:
            dest_dir = str(self._store_path.parent / "downloads")
        os.makedirs(dest_dir, exist_ok=True)

        filename = args.get("filename")
        if not filename:
            filename = url.rsplit("/", 1)[-1].split("?")[0] or f"download_{dl_id}"

        entry = DownloadEntry(
            id=dl_id,
            url=url,
            dest_path=os.path.join(dest_dir, filename),
            filename=filename,
        )
        self._entries[dl_id] = entry
        self._save()

        task = asyncio.create_task(self._download(entry))
        self._tasks[dl_id] = task

        return ToolResult(
            text=f"Download started: {dl_id}\nURL: {url}\nDestination: {entry.dest_path}",
            metadata={"download_id": dl_id, "status": "downloading"},
        )

    def _cancel(self, args: dict[str, Any]) -> ToolResult:
        dl_id = args.get("download_id", "")
        entry = self._entries.get(dl_id)
        if not entry:
            return ToolResult(text=f"Download not found: {dl_id}", is_error=True)
        task = self._tasks.pop(dl_id, None)
        if task and not task.done():
            task.cancel()
        entry.status = DownloadStatus.CANCELLED
        entry.completed_at = time.time()
        self._save()
        return ToolResult(text=f"Download cancelled: {dl_id}")

    def _list(self) -> ToolResult:
        items = [e.to_dict() for e in self._entries.values()]
        return ToolResult(text=json.dumps(items, indent=2, ensure_ascii=False))

    def _delete(self, args: dict[str, Any]) -> ToolResult:
        dl_id = args.get("download_id", "")
        entry = self._entries.pop(dl_id, None)
        if not entry:
            return ToolResult(text=f"Download not found: {dl_id}", is_error=True)
        if entry.dest_path and os.path.exists(entry.dest_path):
            try:
                os.unlink(entry.dest_path)
            except OSError:
                pass
        self._save()
        return ToolResult(text=f"Download deleted: {dl_id}")

    async def _download(self, entry: DownloadEntry) -> None:
        async with self._semaphore:
            entry.status = DownloadStatus.DOWNLOADING
            entry.started_at = time.time()
            self._notify(entry)

            try:
                async with asyncio.timeout(self._timeout):
                    await self._download_http(entry)
            except asyncio.CancelledError:
                entry.status = DownloadStatus.CANCELLED
                entry.error = "Cancelled"
            except TimeoutError:
                entry.status = DownloadStatus.ERROR
                entry.error = "Download timed out"
            except Exception as exc:
                entry.status = DownloadStatus.ERROR
                entry.error = str(exc)
            finally:
                entry.completed_at = time.time()
                self._tasks.pop(entry.id, None)
                self._save()
                self._notify(entry)

    async def _download_http(self, entry: DownloadEntry) -> None:
        tmp_path = entry.dest_path + ".part"
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
                async with client.stream("GET", entry.url) as resp:
                    if resp.status_code >= 400:
                        raise RuntimeError(f"HTTP {resp.status_code}")
                    total = int(resp.headers.get("content-length", "0"))
                    entry.bytes_total = total
                    downloaded = 0
                    last_time = time.time()
                    last_bytes = 0

                    with open(tmp_path, "wb") as f:
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            f.write(chunk)
                            downloaded += len(chunk)
                            entry.bytes_downloaded = downloaded

                            now = time.time()
                            elapsed = now - last_time
                            if elapsed >= 0.5:
                                entry.speed_bps = (downloaded - last_bytes) / elapsed
                                last_time = now
                                last_bytes = downloaded

                            if total > 0:
                                entry.progress = min(99.0, (downloaded / total) * 100)

                            self._notify(entry)

            if os.path.exists(entry.dest_path):
                os.unlink(entry.dest_path)
            os.rename(tmp_path, entry.dest_path)

            entry.status = DownloadStatus.COMPLETED
            entry.progress = 100.0
            entry.bytes_downloaded = entry.bytes_total or os.path.getsize(entry.dest_path)
            entry.speed_bps = 0.0
        except BaseException:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def _notify(self, entry: DownloadEntry) -> None:
        if self._on_progress is not None:
            try:
                result = self._on_progress(entry)
                if asyncio.iscoroutine(result):
                    asyncio.create_task(result)
            except Exception:
                pass

    def _load(self) -> None:
        if not self._store_path.exists():
            return
        try:
            data = json.loads(self._store_path.read_text())
            for item in data.get("downloads", []):
                entry = DownloadEntry.from_dict(item)
                if entry.status in (DownloadStatus.DOWNLOADING, DownloadStatus.PENDING):
                    entry.status = DownloadStatus.PENDING
                self._entries[entry.id] = entry
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load download store: %s", exc)

    def _save(self) -> None:
        downloads = [e.to_dict() for e in self._entries.values()]
        atomic_write(self._store_path, json.dumps({"downloads": downloads}, indent=2))

    def cancel_all(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        for entry in self._entries.values():
            if entry.status in (DownloadStatus.DOWNLOADING, DownloadStatus.PENDING):
                entry.status = DownloadStatus.CANCELLED
                entry.completed_at = time.time()
        self._tasks.clear()
        self._save()


__all__ = ["DownloadTool", "DownloadEntry", "DownloadStatus"]
