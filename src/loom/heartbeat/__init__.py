"""Heartbeat system — scheduled background tasks for agents.

A **heartbeat** is a periodic function (:attr:`HeartbeatDriver.run`)
registered with a :class:`HeartbeatRecord` (name, schedule, tool spec).
The :class:`HeartbeatScheduler` drives the cron loop; the
:class:`HeartbeatManager` maintains the active registry and deduplicates
overlapping runs.

Schedule syntax follows crontab conventions (see :func:`parse_schedule`);
the ``is_due`` helper checks whether a given schedule fires at a
particular UTC timestamp. Persisted state lives in
:class:`HeartbeatStore` (SQLite).
"""

from loom.heartbeat.cron import Schedule, is_due, parse_schedule
from loom.heartbeat.loader import load_heartbeat
from loom.heartbeat.manager import HeartbeatManager
from loom.heartbeat.registry import HeartbeatRegistry
from loom.heartbeat.scheduler import HeartbeatScheduler, RunFn, make_run_fn
from loom.heartbeat.store import HeartbeatStore
from loom.heartbeat.tool import HeartbeatToolHandler
from loom.heartbeat.types import (
    HeartbeatDriver,
    HeartbeatEvent,
    HeartbeatRecord,
    HeartbeatRunRecord,
    validate_heartbeat_id,
)

__all__ = [
    "HeartbeatDriver",
    "HeartbeatEvent",
    "HeartbeatRecord",
    "HeartbeatRunRecord",
    "HeartbeatStore",
    "HeartbeatRegistry",
    "HeartbeatManager",
    "HeartbeatScheduler",
    "HeartbeatToolHandler",
    "RunFn",
    "Schedule",
    "is_due",
    "load_heartbeat",
    "make_run_fn",
    "parse_schedule",
    "validate_heartbeat_id",
]
