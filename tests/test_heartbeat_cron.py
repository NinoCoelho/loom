from datetime import UTC, datetime

import pytest

from loom.heartbeat.cron import Schedule, is_due, parse_schedule


class TestParseSchedule:
    def test_interval_minutes(self):
        s = parse_schedule("every 5 minutes")
        assert s.is_interval
        assert s.interval_seconds == 300.0

    def test_interval_singular(self):
        assert parse_schedule("every minute").interval_seconds == 60.0
        assert parse_schedule("every hour").interval_seconds == 3600.0
        assert parse_schedule("every day").interval_seconds == 86400.0

    def test_interval_hours(self):
        s = parse_schedule("every 2 hours")
        assert s.interval_seconds == 7200.0

    def test_interval_seconds(self):
        s = parse_schedule("every 30 seconds")
        assert s.interval_seconds == 30.0

    def test_interval_case_insensitive(self):
        s = parse_schedule("Every 10 Minutes")
        assert s.interval_seconds == 600.0

    def test_cron_every_5_min(self):
        s = parse_schedule("*/5 * * * *")
        assert not s.is_interval
        assert 0 in s.minutes
        assert 5 in s.minutes
        assert 55 in s.minutes
        assert 3 not in s.minutes

    def test_cron_specific_time(self):
        s = parse_schedule("0 9 * * 1-5")
        assert s.minutes == frozenset({0})
        assert s.hours == frozenset({9})
        assert 1 in s.weekdays and 5 in s.weekdays
        assert 0 not in s.weekdays and 6 not in s.weekdays

    def test_cron_comma_list(self):
        s = parse_schedule("0 8,12,18 * * *")
        assert s.hours == frozenset({8, 12, 18})

    def test_shorthand_daily(self):
        s = parse_schedule("@daily")
        assert not s.is_interval
        assert s.minutes == frozenset({0})
        assert s.hours == frozenset({0})

    def test_shorthand_hourly(self):
        s = parse_schedule("@hourly")
        assert s.minutes == frozenset({0})

    def test_shorthand_weekly(self):
        s = parse_schedule("@weekly")
        assert s.weekdays == frozenset({0})  # Sunday

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_schedule("not a schedule")


class TestIsDue:
    def test_interval_never_checked(self):
        s = parse_schedule("every 5 minutes")
        assert is_due(s, None, datetime.now(UTC))

    def test_interval_not_elapsed(self):
        s = parse_schedule("every 5 minutes")
        last = datetime(2024, 1, 1, 12, 0, 0)
        now = datetime(2024, 1, 1, 12, 4, 0)  # 4 minutes later
        assert not is_due(s, last, now)

    def test_interval_elapsed(self):
        s = parse_schedule("every 5 minutes")
        last = datetime(2024, 1, 1, 12, 0, 0)
        now = datetime(2024, 1, 1, 12, 5, 0)  # exactly 5 minutes later
        assert is_due(s, last, now)

    def test_cron_never_checked_matches(self):
        # 0 9 * * * — fires at 09:00
        s = parse_schedule("0 9 * * *")
        now = datetime(2024, 1, 1, 9, 0, 0)
        assert is_due(s, None, now)

    def test_cron_never_checked_no_match(self):
        s = parse_schedule("0 9 * * *")
        now = datetime(2024, 1, 1, 10, 0, 0)
        assert not is_due(s, None, now)

    def test_cron_already_checked_this_minute(self):
        s = parse_schedule("0 9 * * *")
        now = datetime(2024, 1, 1, 9, 0, 30)
        last = datetime(2024, 1, 1, 9, 0, 5)  # checked 25s ago
        assert not is_due(s, last, now)

    def test_cron_checked_previous_minute(self):
        s = parse_schedule("*/5 * * * *")
        now = datetime(2024, 1, 1, 9, 5, 0)
        last = datetime(2024, 1, 1, 9, 3, 0)  # 2 minutes ago
        assert is_due(s, last, now)
