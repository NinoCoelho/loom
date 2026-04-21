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
    """

    def __init__(
        self,
        registry: HeartbeatRegistry,
        store: HeartbeatStore,
        run_fn: RunFn,
        tick_interval: float = 60.0,
        sessions: Any = None,  # SessionStore | None — stored for callers, not used internally
    ) -> None:
        self._registry = registry
        self._store = store
        self._run_fn = run_fn
        self._tick_interval = tick_interval
        self.sessions = sessions
        self._task: asyncio.Task | None = None

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

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    # ------------------------------------------------------------------
    # manual trigger
    # ------------------------------------------------------------------

    async def trigger(
        self, heartbeat_id: str, instance_id: str = "default"
    ) -> list[AgentTurn]:
        record = self._registry.get(heartbeat_id)
        if record is None:
            raise KeyError(f"heartbeat {heartbeat_id!r} not found")
        run = self._store.get_run(heartbeat_id, instance_id)
        return await self._fire(record, run, instance_id)

    # ------------------------------------------------------------------
    # internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        logger.info("heartbeat scheduler started (tick=%.0fs)", self._tick_interval)
        while True:
            try:
                await asyncio.sleep(self._tick_interval)
                await self._tick()
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
                logger.warning("heartbeat %r has unparseable schedule %r", record.id, record.schedule)
                continue

            run = self._store.get_run(record.id)
            last_check = run.last_check if run else None

            if is_due(schedule, last_check, now):
                await self._fire(record, run)

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
                    record.id, event.name, exc,
                )
        return turns

    async def _invoke_agent(
        self, record: HeartbeatRecord, event: HeartbeatEvent
    ) -> AgentTurn:
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
    overriding the system prompt with the heartbeat's instructions."""
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
            config=config,
        )
        return await hb_agent.run_turn(messages)

    return _run
