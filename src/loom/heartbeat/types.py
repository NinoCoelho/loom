from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


@dataclass
class HeartbeatEvent:
    name: str
    payload: dict[str, Any] = field(default_factory=dict)
    fired_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class HeartbeatDriver(ABC):
    """Pure detection unit: given state-in, return (events, state-out).

    The runtime owns state persistence — drivers never read or write state
    themselves. An empty events list means nothing happened this cycle.
    """

    @abstractmethod
    async def check(
        self, state: dict[str, Any]
    ) -> tuple[list[HeartbeatEvent], dict[str, Any]]:
        ...


@dataclass
class HeartbeatRecord:
    id: str                  # directory name / primary key
    name: str
    description: str
    schedule: str            # raw schedule string (cron or natural language)
    enabled: bool
    instructions: str        # HEARTBEAT.md body — agent system prompt
    source_dir: Path
    driver: HeartbeatDriver


@dataclass
class HeartbeatRunRecord:
    heartbeat_id: str
    instance_id: str
    state: dict[str, Any]
    last_check: datetime | None
    last_fired: datetime | None
    last_error: str | None


def validate_heartbeat_id(id: str) -> None:
    if not _ID_RE.match(id):
        raise ValueError(f"invalid heartbeat id {id!r} — use [a-zA-Z0-9_-], max 64 chars")
