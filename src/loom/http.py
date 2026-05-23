"""Shared HTTP session with in-memory TTL cache and concurrency limiting.

:class:`CachedSession` wraps ``httpx.AsyncClient`` with a configurable
per-request cache (keyed on method + URL) and a semaphore that caps the
number of simultaneous in-flight requests.

Any tool or provider making HTTP calls can share a single
``CachedSession`` instance to benefit from caching and rate limiting.

Usage::

    session = CachedSession(max_concurrency=8, cache_ttl=300)
    resp = await session.get("https://api.example.com/data")
    await session.close()
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    data: dict[str, Any]
    expires_at: float


class CachedSession:
    """Async HTTP session with TTL cache and concurrency limiting.

    Parameters
    ----------
    max_concurrency : int
        Maximum number of simultaneous in-flight HTTP requests.
    cache_ttl : int
        Default cache time-to-live in seconds. Set to 0 to disable caching.
    max_cache_entries : int
        Maximum number of cached responses. When exceeded, stale entries
        are evicted first, then LRU.
    default_headers : dict, optional
        Headers included in every request.
    timeout : float
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        *,
        max_concurrency: int = 8,
        cache_ttl: int = 300,
        max_cache_entries: int = 200,
        default_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._cache_ttl = cache_ttl
        self._max_cache_entries = max_cache_entries
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers=default_headers or {},
            follow_redirects=True,
        )
        self._cache: dict[str, _CacheEntry] = {}

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache_ttl: int | None = None,
        cache_key: str | None = None,
    ) -> httpx.Response:
        """Send a GET request with optional caching."""
        ttl = cache_ttl if cache_ttl is not None else self._cache_ttl
        key = cache_key or self._make_key("GET", url, params)

        if ttl > 0:
            cached = self._get_cached(key)
            if cached is not None:
                return _CachedResponse(cached)

        await self._semaphore.acquire()
        try:
            resp = await self._client.get(url, params=params, headers=headers)
        finally:
            self._semaphore.release()

        if ttl > 0 and resp.status_code < 400:
            self._set_cached(key, resp, ttl)

        return resp

    async def post(
        self,
        url: str,
        *,
        content: bytes | str | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
        cache_ttl: int | None = None,
        cache_key: str | None = None,
    ) -> httpx.Response:
        """Send a POST request with optional caching."""
        ttl = cache_ttl if cache_ttl is not None else 0
        key = cache_key or self._make_key("POST", url, {"json": json})

        if ttl > 0:
            cached = self._get_cached(key)
            if cached is not None:
                return _CachedResponse(cached)

        await self._semaphore.acquire()
        try:
            resp = await self._client.post(
                url, content=content, json=json, headers=headers
            )
        finally:
            self._semaphore.release()

        if ttl > 0 and resp.status_code < 400:
            self._set_cached(key, resp, ttl)

        return resp

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> CachedSession:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def clear_cache(self) -> None:
        self._cache.clear()

    def _make_key(self, method: str, url: str, extra: Any = None) -> str:
        raw = f"{method}:{url}:{extra}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _get_cached(self, key: str) -> dict[str, Any] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if time.monotonic() >= entry.expires_at:
            del self._cache[key]
            return None
        return entry.data

    def _set_cached(self, key: str, resp: httpx.Response, ttl: int) -> None:
        try:
            body = resp.text
        except Exception:
            return

        if len(self._cache) >= self._max_cache_entries:
            self._evict()

        self._cache[key] = _CacheEntry(
            data={
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body": body,
            },
            expires_at=time.monotonic() + ttl,
        )

    def _evict(self) -> None:
        now = time.monotonic()
        stale = [k for k, v in self._cache.items() if now >= v.expires_at]
        for k in stale:
            del self._cache[k]
        if not stale and self._cache:
            oldest_key = min(self._cache, key=lambda k: self._cache[k].expires_at)
            del self._cache[oldest_key]


class _CachedResponse:
    """Lightweight response wrapper that replays a cached result."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.status_code: int = data["status_code"]
        self.headers: dict = data.get("headers", {})
        self._text: str = data.get("body", "")

    @property
    def text(self) -> str:
        return self._text

    def json(self) -> Any:
        return json.loads(self._text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("GET", "cached"),
                response=httpx.Response(self.status_code),
            )


__all__ = ["CachedSession"]
