from __future__ import annotations

import asyncio
import itertools
import random
from collections.abc import Callable, Coroutine
from typing import Any

from loom.errors import classify_api_error

_counter = itertools.count()


def jittered_backoff(
    attempt: int,
    base: float = 2.0,
    max_delay: float = 60.0,
    jitter_ratio: float = 0.5,
) -> float:
    raw = min(base * (2**attempt), max_delay)
    jitter = raw * jitter_ratio * random.random()
    return min(raw - raw * jitter_ratio / 2 + jitter, max_delay)


async def with_retry[T](
    coro_factory: Callable[[], Coroutine[Any, Any, T]],
    max_attempts: int = 3,
) -> T:
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await coro_factory()
        except Exception as exc:
            last_error = exc
            classified = classify_api_error(exc)
            if not classified.retryable or attempt == max_attempts - 1:
                raise
            next(_counter)
            delay = jittered_backoff(attempt + next(_counter) % 3)
            await asyncio.sleep(delay)
    if last_error is None:
        raise RuntimeError("with_retry exhausted without capturing an exception")
    raise last_error
