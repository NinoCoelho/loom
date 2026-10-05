"""Heartbeat scheduler — asyncio loop driving registered heartbeats.

:class:`HeartbeatScheduler` ticks at a fixed interval and, for every due
heartbeat, runs ``driver.check()`` in an **isolated task** with a
per-driver timeout and skip-if-running semantics:

* one slow or hung driver can never block the tick loop or starve the
  other drivers (head-of-line blocking was the historical failure mode);
* a driver that exceeds ``driver_timeout`` is cancelled and the timeout
  recorded as ``last_error`` — the loop keeps going;
* overlapping runs of the same driver are suppressed (skip-if-already-
  running), which also makes the manual ``trigger()`` path race-free
  against the loop's own tick.

The scheduler stamps ``last_tick_at`` after every iteration so liveness
can be inspected externally (:func:`status`). Large wall-clock jumps
between ticks (system sleep) are detected and logged; interval schedules
catch up naturally on the first tick after wake.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from loom.heartbeat.cron import is_due, parse_schedule
from loom.heartbeat.registry import HeartbeatRegistry
from loom.heartbeat.store import HeartbeatStore
from loom.heartbeat.types import HeartbeatEvent, HeartbeatRecord, HeartbeatRunRecord
from loom.loop import AgentTurn
from loom.types import ChatMessage, Role

logger = logging.getLogger(__name__)

# Signature: (instructions, messages) → AgentTurn
RunFn = Callable[[str, list[ChatMessage]], Awaitable[AgentTurn]]


class HeartbeatScheduler:
    """Asyncio background scheduler that ticks registered heartbeats.

    ``run_fn`` is the only integration point with the agent layer:

        async def run_fn(instructions: str, messages: list[ChatMessage]) -> AgentTurn:
            ...

    The scheduler calls ``driver.check(state)`` for each due heartbeat and,
    for every returned event, invokes ``run_fn`` with the heartbeat's
    instructions and a single-message conversation describing the event.
    State is persisted via HeartbeatStore between ticks.

    Args:
        driver_timeout: hard cap (seconds) on a single ``driver.check()``
            call. Drivers that legitimately need longer should detach their
            work (``asyncio.create_task``) and return immediately.
    """

    def __init__(
        self,
        registry: HeartbeatRegistry,
        store: HeartbeatStore,
        run_fn: RunFn,
        tick_interval: float = 60.0,
        sessions: Any = None,  # SessionStore | None — stored for callers, not used internally
        driver_timeout: float = 300.0,
    ) -> None:
        self._registry = registry
        self._store = store
        self._run_fn = run_fn
        self._tick_interval = tick_interval
        self._driver_timeout = driver_timeout
        self.sessions = sessions
        self._task: asyncio.Task | None = None
        self._inflight: dict[str, asyncio.Task] = {}
        self._last_tick_at: datetime | None = None
        self._tick_count: int = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._task = asyncio.create_task(self._loop(), name="heartbeat-scheduler")
        return self._task

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        for task in self._inflight.values():
            if not task.done():
                task.cancel()
        self._inflight.clear()

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    @property
    def last_tick_at(self) -> datetime | None:
        return self._last_tick_at

    def status(self) -> dict[str, Any]:
        """Liveness snapshot for health endpoints / watchdogs."""
        now = datetime.now(UTC)
        stalled = (
            self._last_tick_at is not None
            and (now - self._last_tick_at).total_seconds() > self._tick_interval * 5
        )
        return {
            "running": self.running,
            "tick_interval": self._tick_interval,
            "tick_count": self._tick_count,
            "last_tick_at": self._last_tick_at.isoformat() if self._last_tick_at else None,
            "stalled": stalled,
            "inflight": sorted(
                hid for hid, t in self._inflight.items() if not t.done()
            ),
            "driver_timeout": self._driver_timeout,
        }

    # ------------------------------------------------------------------
    # manual trigger
    # ------------------------------------------------------------------

    async def trigger(self, heartbeat_id: str, instance_id: str = "default") -> list[AgentTurn]:
        record = self._registry.get(heartbeat_id)
        if record is None:
            raise KeyError(f"heartbeat {heartbeat_id!r} not found")
        if self._is_inflight(heartbeat_id, instance_id):
            logger.warning("heartbeat %r already running; manual trigger skipped", heartbeat_id)
            return []
        run = self._store.get_run(heartbeat_id, instance_id)
        return await self._fire_isolated(record, run, instance_id)

    # ------------------------------------------------------------------
    # internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        logger.info("heartbeat scheduler started (tick=%.0fs)", self._tick_interval)
        previous_now = datetime.now(UTC)
        while True:
            try:
                await asyncio.sleep(self._tick_interval)
                now = datetime.now(UTC)
                gap = (now - previous_now).total_seconds()
                if gap > self._tick_interval * 3:
                    # Wall clock jumped far past the expected tick — the
                    # machine likely slept. Interval schedules catch up
                    # naturally on this tick; log for observability.
                    logger.info(
                        "heartbeat scheduler: sleep/wake detected (gap %.0fs > 3x tick) — catching up",
                        gap,
                    )
                previous_now = now
                await self._tick()
                self._last_tick_at = datetime.now(UTC)
                self._tick_count += 1
            except asyncio.CancelledError:
                logger.info("heartbeat scheduler stopped")
                return
            except Exception:
                logger.exception("unexpected error in heartbeat scheduler tick")

    async def _tick(self) -> None:
        now = datetime.now(UTC)
        for record in self._registry.list():
            if not record.enabled:
                continue
            try:
                schedule = parse_schedule(record.schedule)
            except ValueError:
                logger.warning(
                    "heartbeat %r has unparseable schedule %r", record.id, record.schedule
                )
                continue

            run = self._store.get_run(record.id)
            last_check = run.last_check if run else None

            if not is_due(schedule, last_check, now):
                continue
            if self._is_inflight(record.id, "default"):
                logger.debug("heartbeat %r still running from previous tick; skipping", record.id)
                continue

            task = asyncio.create_task(
                self._fire_isolated(record, run),
                name=f"heartbeat-fire-{record.id}",
            )
            self._inflight[record.id] = task
            task.add_done_callback(lambda t, hid=record.id: self._inflight.pop(hid, None))

    def _is_inflight(self, heartbeat_id: str, instance_id: str = "default") -> bool:
        if instance_id != "default":
            key = f"{heartbeat_id}::{instance_id}"
            task = self._inflight.get(key)
        else:
            task = self._inflight.get(heartbeat_id)
        return bool(task and not task.done())

    async def _fire_isolated(
        self,
        record: HeartbeatRecord,
        run: HeartbeatRunRecord | None,
        instance_id: str = "default",
    ) -> list[AgentTurn]:
        """Run one driver's check under a timeout; never raises."""
        try:
            return await asyncio.wait_for(
                self._fire(record, run, instance_id),
                timeout=self._driver_timeout,
            )
        except asyncio.TimeoutError:
            err = f"driver.check timed out after {self._driver_timeout:.0f}s"
            logger.error("heartbeat %r: %s", record.id, err)
            try:
                self._store.touch_fired(record.id, instance_id, error=err)
            except Exception:
                logger.exception("failed to record timeout for heartbeat %r", record.id)
            return []
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("heartbeat %r: unexpected fire failure", record.id)
            try:
                self._store.touch_fired(record.id, instance_id, error=str(exc))
            except Exception:
                pass
            return []

    async def _fire(
        self,
        record: HeartbeatRecord,
        run: HeartbeatRunRecord | None,
        instance_id: str = "default",
    ) -> list[AgentTurn]:
        state = run.state if run else {}
        self._store.touch_check(record.id, instance_id)

        try:
            events, new_state = await record.driver.check(state)
        except Exception as exc:
            err = str(exc)
            logger.error("driver.check failed for heartbeat %r: %s", record.id, err)
            self._store.touch_fired(record.id, instance_id, error=err)
            return []

        self._store.set_state(record.id, new_state, instance_id)

        if not events:
            return []

        self._store.touch_fired(record.id, instance_id)

        turns: list[AgentTurn] = []
        for event in events:
            try:
                turn = await self._invoke_agent(record, event)
                turns.append(turn)
            except Exception as exc:
                logger.error(
                    "agent invocation failed for heartbeat %r event %r: %s",
                    record.id,
                    event.name,
                    exc,
                )
        return turns

    async def _invoke_agent(self, record: HeartbeatRecord, event: HeartbeatEvent) -> AgentTurn:
        event_summary = _format_event(record, event)
        messages = [ChatMessage(role=Role.USER, content=event_summary)]

        if self.sessions is not None:
            session_id = f"heartbeat_{record.id}_{uuid.uuid4().hex[:8]}"
            self.sessions.get_or_create(
                session_id,
                title=f"[heartbeat] {record.name} — {event.name}",
            )

        turn = await self._run_fn(record.instructions, messages)

        if self.sessions is not None:
            # Persist the turn into the session for observability
            history = [
                messages[0],
                ChatMessage(role=Role.ASSISTANT, content=turn.reply),
            ]
            self.sessions.replace_history(session_id, history)
            self.sessions.bump_usage(
                session_id, turn.input_tokens, turn.output_tokens, turn.tool_calls
            )

        return turn


def _format_event(record: HeartbeatRecord, event: HeartbeatEvent) -> str:
    lines = [
        f"Heartbeat: {record.name}",
        f"Event: {event.name}",
        f"Time: {event.fired_at.isoformat()}",
    ]
    if event.payload:
        lines.append(f"Payload:\n{json.dumps(event.payload, indent=2)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def make_run_fn(agent: Any) -> RunFn:
    """Build a RunFn from an existing Agent, using its provider/tools but
    overriding the system prompt with the heartbeat's instructions.

    Forwards skill_registry so the heartbeat agent can use activate_skill.
    """
    from loom.loop import Agent, AgentConfig  # local import to avoid circularity

    async def _run(instructions: str, messages: list[ChatMessage]) -> AgentTurn:
        config = AgentConfig(
            system_preamble=instructions,
            model=agent._config.model,
            max_iterations=agent._config.max_iterations,
        )
        hb_agent = Agent(
            provider=agent._provider,
            provider_registry=agent._provider_registry,
            tool_registry=agent._tools,
            skill_registry=agent._skills,
            config=config,
        )
        return await hb_agent.run_turn(messages)

    return _run
