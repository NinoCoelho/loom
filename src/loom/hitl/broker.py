"""Session-scoped HITL broker — Future registry + pub/sub event bus."""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

# Server handlers should set this before invoking the agent loop so the
# broker knows which session a given tool call belongs to.
CURRENT_SESSION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "loom_hitl_current_session_id", default=None
)

TIMEOUT_SENTINEL = "__timeout__"


@dataclass(frozen=True)
class HitlEvent:
    """A single pub/sub event addressed to a session's subscribers."""

    kind: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HitlRequest:
    """Snapshot of a pending ``ask_user`` call for UIs/tests to inspect."""

    session_id: str
    request_id: str
    prompt: str
    kind: str
    choices: list[str] | None = None
    default: str | None = None
    timeout_seconds: int = 300


class HitlBroker:
    """Per-session registry of pending Futures + subscribable event bus.

    Thread-safety note: everything runs on a single asyncio loop. No
    locks — mutations must happen from the loop thread.
    """

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], asyncio.Future[str]] = {}
        self._subscribers: dict[str, list[asyncio.Queue[HitlEvent]]] = {}
        self._requests: dict[tuple[str, str], HitlRequest] = {}

    # ── Pub/sub ──────────────────────────────────────────────────────

    def subscribe(self, session_id: str) -> asyncio.Queue[HitlEvent]:
        """Open a fresh queue that receives every event for ``session_id``."""
        queue: asyncio.Queue[HitlEvent] = asyncio.Queue()
        self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[HitlEvent]) -> None:
        queues = self._subscribers.get(session_id)
        if not queues:
            return
        try:
            queues.remove(queue)
        except ValueError:
            return
        if not queues:
            self._subscribers.pop(session_id, None)

    async def events(self, session_id: str) -> AsyncIterator[HitlEvent]:
        """Async iterator of events for ``session_id`` until the consumer stops."""
        queue = self.subscribe(session_id)
        try:
            while True:
                yield await queue.get()
        finally:
            self.unsubscribe(session_id, queue)

    def publish(self, session_id: str, event: HitlEvent) -> None:
        for q in self._subscribers.get(session_id, []):
            q.put_nowait(event)

    # ── Pending requests ─────────────────────────────────────────────

    def _register(self, session_id: str, request_id: str) -> asyncio.Future[str]:
        key = (session_id, request_id)
        if key in self._pending:
            raise ValueError(f"duplicate request_id: {request_id}")
        fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._pending[key] = fut
        return fut

    def resolve(self, session_id: str, request_id: str, answer: str) -> bool:
        """Complete a pending request with an answer. Returns True if matched."""
        fut = self._pending.pop((session_id, request_id), None)
        self._requests.pop((session_id, request_id), None)
        if fut is None or fut.done():
            return False
        fut.set_result(answer)
        return True

    def cancel_pending(
        self, session_id: str, request_id: str, *, reason: str = "cancelled"
    ) -> bool:
        fut = self._pending.pop((session_id, request_id), None)
        self._requests.pop((session_id, request_id), None)
        if fut is None or fut.done():
            return False
        fut.cancel()
        self.publish(
            session_id,
            HitlEvent(
                kind="user_request_cancelled",
                data={"request_id": request_id, "reason": reason},
            ),
        )
        return True

    def cancel_session(self, session_id: str, *, reason: str = "session_reset") -> int:
        """Cancel every pending request on a session (e.g. on disconnect)."""
        keys = [k for k in self._pending if k[0] == session_id]
        for _, rid in keys:
            self.cancel_pending(session_id, rid, reason=reason)
        return len(keys)

    def pending(self, session_id: str) -> list[HitlRequest]:
        return [r for (sid, _), r in self._requests.items() if sid == session_id]

    # ── High-level ask ───────────────────────────────────────────────

    async def ask(
        self,
        session_id: str,
        prompt: str,
        *,
        kind: str = "confirm",
        choices: list[str] | None = None,
        default: str | None = None,
        timeout_seconds: int = 300,
        yolo: bool = False,
    ) -> str:
        """Publish a ``user_request`` and await the user's answer.

        On timeout returns :data:`TIMEOUT_SENTINEL` and publishes a
        ``user_request_cancelled`` event. Never raises on timeout — the
        calling tool decides how to interpret the sentinel.

        ``yolo`` short-circuits confirm-kind prompts with a synthetic
        ``yes`` and a ``user_request_auto`` event so the transcript
        still records the decision.
        """
        if kind == "confirm" and yolo:
            self.publish(
                session_id,
                HitlEvent(
                    kind="user_request_auto",
                    data={
                        "prompt": prompt,
                        "kind": kind,
                        "answer": "yes",
                        "reason": "yolo",
                    },
                ),
            )
            return "yes"

        request_id = uuid.uuid4().hex
        fut = self._register(session_id, request_id)
        req = HitlRequest(
            session_id=session_id,
            request_id=request_id,
            prompt=prompt,
            kind=kind,
            choices=choices,
            default=default,
            timeout_seconds=timeout_seconds,
        )
        self._requests[(session_id, request_id)] = req

        self.publish(
            session_id,
            HitlEvent(
                kind="user_request",
                data={
                    "request_id": request_id,
                    "prompt": prompt,
                    "kind": kind,
                    "choices": choices,
                    "default": default,
                    "timeout_seconds": timeout_seconds,
                },
            ),
        )

        try:
            return await asyncio.wait_for(fut, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            self._pending.pop((session_id, request_id), None)
            self._requests.pop((session_id, request_id), None)
            self.publish(
                session_id,
                HitlEvent(
                    kind="user_request_cancelled",
                    data={"request_id": request_id, "reason": "timeout"},
                ),
            )
            return TIMEOUT_SENTINEL
        except asyncio.CancelledError:
            # Session torn down mid-wait — already cleaned up in cancel_*.
            return TIMEOUT_SENTINEL
