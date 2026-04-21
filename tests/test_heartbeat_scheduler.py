"""Scheduler tests using scripted drivers and a mock run_fn."""
from __future__ import annotations

import asyncio
import textwrap
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from loom.heartbeat.cron import parse_schedule
from loom.heartbeat.registry import HeartbeatRegistry
from loom.heartbeat.scheduler import HeartbeatScheduler
from loom.heartbeat.store import HeartbeatStore
from loom.heartbeat.types import HeartbeatDriver, HeartbeatEvent, HeartbeatRecord
from loom.loop import AgentTurn
from loom.types import ChatMessage, Role


DRIVER_CODE = textwrap.dedent("""\
    from loom.heartbeat.types import HeartbeatDriver, HeartbeatEvent

    class Driver(HeartbeatDriver):
        async def check(self, state):
            count = state.get("count", 0) + 1
            return [HeartbeatEvent(name="tick", payload={"count": count})], {"count": count}
""")

SILENT_DRIVER_CODE = textwrap.dedent("""\
    from loom.heartbeat.types import HeartbeatDriver

    class Driver(HeartbeatDriver):
        async def check(self, state):
            return [], state
""")


def _make_hb_dir(base: Path, name: str, driver_code: str = DRIVER_CODE) -> Path:
    hb_dir = base / name
    hb_dir.mkdir(parents=True)
    (hb_dir / "HEARTBEAT.md").write_text(
        f"---\nname: {name}\ndescription: Test\nschedule: \"every 1 second\"\nenabled: true\n---\nDo stuff.\n",
        encoding="utf-8",
    )
    (hb_dir / "driver.py").write_text(driver_code, encoding="utf-8")
    return hb_dir


def _fake_turn(reply: str = "done") -> AgentTurn:
    return AgentTurn(reply=reply, input_tokens=10, output_tokens=20)


@pytest.fixture
def hb_dir(tmp_dir):
    return tmp_dir / "heartbeats"


@pytest.fixture
def store(tmp_dir):
    return HeartbeatStore(tmp_dir / "hb.sqlite")


@pytest.fixture
def registry(hb_dir):
    reg = HeartbeatRegistry(hb_dir)
    return reg


@pytest.fixture
def run_fn():
    mock = AsyncMock(return_value=_fake_turn())
    return mock


@pytest.fixture
def scheduler(registry, store, run_fn):
    return HeartbeatScheduler(registry, store, run_fn, tick_interval=0.05)


class TestSchedulerTrigger:
    async def test_trigger_fires_driver(self, tmp_dir, store, run_fn):
        hb_dir = tmp_dir / "heartbeats"
        _make_hb_dir(hb_dir, "test-hb")
        reg = HeartbeatRegistry(hb_dir)
        reg.scan()
        sched = HeartbeatScheduler(reg, store, run_fn)

        turns = await sched.trigger("test-hb")
        assert len(turns) == 1
        run_fn.assert_awaited_once()
        # Instructions and a user message were passed
        instructions_arg, messages_arg = run_fn.call_args[0]
        assert "Do stuff" in instructions_arg
        assert messages_arg[0].role == Role.USER
        assert "tick" in messages_arg[0].content

    async def test_trigger_unknown_raises(self, tmp_dir, store, run_fn):
        reg = HeartbeatRegistry(tmp_dir / "empty")
        sched = HeartbeatScheduler(reg, store, run_fn)
        with pytest.raises(KeyError):
            await sched.trigger("nonexistent")

    async def test_trigger_updates_state(self, tmp_dir, store, run_fn):
        hb_dir = tmp_dir / "heartbeats"
        _make_hb_dir(hb_dir, "state-hb")
        reg = HeartbeatRegistry(hb_dir)
        reg.scan()
        sched = HeartbeatScheduler(reg, store, run_fn)

        await sched.trigger("state-hb")
        state = store.get_state("state-hb")
        assert state == {"count": 1}

        await sched.trigger("state-hb")
        state = store.get_state("state-hb")
        assert state == {"count": 2}

    async def test_silent_driver_no_agent_call(self, tmp_dir, store, run_fn):
        hb_dir = tmp_dir / "heartbeats"
        _make_hb_dir(hb_dir, "silent-hb", driver_code=SILENT_DRIVER_CODE)
        reg = HeartbeatRegistry(hb_dir)
        reg.scan()
        sched = HeartbeatScheduler(reg, store, run_fn)

        turns = await sched.trigger("silent-hb")
        assert turns == []
        run_fn.assert_not_awaited()

    async def test_driver_error_is_stored(self, tmp_dir, store, run_fn):
        error_driver = textwrap.dedent("""\
            from loom.heartbeat.types import HeartbeatDriver

            class Driver(HeartbeatDriver):
                async def check(self, state):
                    raise RuntimeError("boom")
        """)
        hb_dir = tmp_dir / "heartbeats"
        _make_hb_dir(hb_dir, "err-hb", driver_code=error_driver)
        reg = HeartbeatRegistry(hb_dir)
        reg.scan()
        sched = HeartbeatScheduler(reg, store, run_fn)

        turns = await sched.trigger("err-hb")
        assert turns == []
        run = store.get_run("err-hb")
        assert run is not None
        assert "boom" in (run.last_error or "")


class TestSchedulerLifecycle:
    async def test_start_stop(self, tmp_dir, store, run_fn):
        reg = HeartbeatRegistry(tmp_dir / "empty")
        sched = HeartbeatScheduler(reg, store, run_fn, tick_interval=0.05)
        sched.start()
        assert sched.running
        sched.stop()
        assert not sched.running

    async def test_start_idempotent(self, tmp_dir, store, run_fn):
        reg = HeartbeatRegistry(tmp_dir / "empty")
        sched = HeartbeatScheduler(reg, store, run_fn, tick_interval=60)
        t1 = sched.start()
        t2 = sched.start()
        assert t1 is t2
        sched.stop()

    async def test_disabled_heartbeat_skipped(self, tmp_dir, store, run_fn):
        hb_dir = tmp_dir / "heartbeats"
        _make_hb_dir(hb_dir, "off-hb")
        # Mark as disabled in HEARTBEAT.md
        md = hb_dir / "off-hb" / "HEARTBEAT.md"
        md.write_text(
            "---\nname: off-hb\ndescription: Test\nschedule: \"every 1 second\"\nenabled: false\n---\nDo stuff.\n"
        )
        reg = HeartbeatRegistry(hb_dir)
        reg.scan()
        sched = HeartbeatScheduler(reg, store, run_fn, tick_interval=0.05)
        sched.start()
        await asyncio.sleep(0.12)
        sched.stop()
        run_fn.assert_not_awaited()


class TestSchedulerWithSessions:
    async def test_session_created_on_fire(self, tmp_dir, store, run_fn):
        from loom.store.session import SessionStore
        sessions = SessionStore(tmp_dir / "sessions.sqlite")

        hb_dir = tmp_dir / "heartbeats"
        _make_hb_dir(hb_dir, "sess-hb")
        reg = HeartbeatRegistry(hb_dir)
        reg.scan()
        sched = HeartbeatScheduler(reg, store, run_fn, sessions=sessions)

        await sched.trigger("sess-hb")
        all_sessions = sessions.list_sessions()
        assert len(all_sessions) == 1
        assert "heartbeat" in all_sessions[0]["id"]
        assert "sess-hb" in (all_sessions[0]["title"] or "")
