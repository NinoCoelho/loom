from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Natural-language interval patterns
_INTERVAL_RE = re.compile(
    r"every\s+(?:(\d+)\s+)?(second|minute|hour|day)s?",
    re.IGNORECASE,
)

_SHORTHANDS: dict[str, str] = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}

_UNIT_SECONDS: dict[str, float] = {
    "second": 1.0,
    "minute": 60.0,
    "hour": 3600.0,
    "day": 86400.0,
}


@dataclass(frozen=True)
class Schedule:
    # Set exactly one of interval_seconds or cron fields.
    interval_seconds: float | None = None
    # cron fields: each is a frozenset of matching int values
    minutes: frozenset[int] | None = None
    hours: frozenset[int] | None = None
    days: frozenset[int] | None = None
    months: frozenset[int] | None = None
    weekdays: frozenset[int] | None = None

    @property
    def is_interval(self) -> bool:
        return self.interval_seconds is not None


def parse_schedule(expr: str) -> Schedule:
    """Parse a schedule expression into a Schedule.

    Accepts:
    - Natural language: "every 5 minutes", "every hour", "every 2 days"
    - Cron shorthands: "@daily", "@hourly", etc.
    - Standard 5-field cron: "*/5 * * * *", "0 9 * * 1-5"
    """
    expr = expr.strip()

    # Natural language interval
    m = _INTERVAL_RE.match(expr)
    if m:
        n = int(m.group(1)) if m.group(1) else 1
        unit = m.group(2).lower()
        return Schedule(interval_seconds=n * _UNIT_SECONDS[unit])

    # Shorthand aliases
    canonical = _SHORTHANDS.get(expr.lower())
    if canonical:
        expr = canonical

    # Standard cron expression
    parts = expr.split()
    if len(parts) == 5:
        return Schedule(
            minutes=_parse_field(parts[0], 0, 59),
            hours=_parse_field(parts[1], 0, 23),
            days=_parse_field(parts[2], 1, 31),
            months=_parse_field(parts[3], 1, 12),
            weekdays=_parse_field(parts[4], 0, 6),
        )

    raise ValueError(
        f"unrecognised schedule {expr!r} — use cron (5 fields), @shorthand, "
        "or 'every N seconds/minutes/hours/days'"
    )


def is_due(schedule: Schedule, last_check: datetime | None, now: datetime) -> bool:
    """Return True if this heartbeat should fire right now."""
    if schedule.is_interval:
        if last_check is None:
            return True
        return (now - last_check).total_seconds() >= schedule.interval_seconds  # type: ignore[operator]

    # Cron: match current time, but don't fire more than once per minute
    if last_check is not None and (now - last_check).total_seconds() < 60:
        return False
    return _cron_matches(schedule, now)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _parse_field(field: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(lo, hi + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            values.update(range(lo, hi + 1, step))
        elif "-" in part:
            raw_range, *rest = part.split("/")
            a, b = raw_range.split("-")
            step = int(rest[0]) if rest else 1
            values.update(range(int(a), int(b) + 1, step))
        else:
            values.add(int(part))
    return frozenset(values)


def _cron_matches(schedule: Schedule, dt: datetime) -> bool:
    assert schedule.minutes is not None
    assert schedule.hours is not None
    assert schedule.days is not None
    assert schedule.months is not None
    assert schedule.weekdays is not None
    # weekday: Python isoweekday 1=Mon…7=Sun → cron 0=Sun…6=Sat
    wd = dt.isoweekday() % 7  # Sun=0, Mon=1, …, Sat=6
    return (
        dt.minute in schedule.minutes
        and dt.hour in schedule.hours
        and dt.day in schedule.days
        and dt.month in schedule.months
        and wd in schedule.weekdays
    )
