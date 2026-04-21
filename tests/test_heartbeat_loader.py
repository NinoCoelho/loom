import textwrap

import pytest

from loom.heartbeat.loader import load_heartbeat
from loom.heartbeat.types import HeartbeatDriver

DRIVER_CODE = textwrap.dedent("""\
    from loom.heartbeat.types import HeartbeatDriver, HeartbeatEvent

    class Driver(HeartbeatDriver):
        async def check(self, state):
            count = state.get("count", 0) + 1
            events = [HeartbeatEvent(name="tick", payload={"count": count})]
            return events, {"count": count}
""")

DRIVER_NO_EVENTS = textwrap.dedent("""\
    from loom.heartbeat.types import HeartbeatDriver

    class Driver(HeartbeatDriver):
        async def check(self, state):
            return [], state
""")


def _write_heartbeat(
    base_dir,
    name,
    schedule="every 1 minute",
    driver_code=DRIVER_CODE,
    instructions="Do something.",
):
    hb_dir = base_dir / name
    hb_dir.mkdir(parents=True)
    (hb_dir / "HEARTBEAT.md").write_text(
        (
            f'---\nname: {name}\ndescription: Test heartbeat\n'
            f'schedule: "{schedule}"\nenabled: true\n---\n{instructions}\n'
        ),
        encoding="utf-8",
    )
    (hb_dir / "driver.py").write_text(driver_code, encoding="utf-8")
    return hb_dir


class TestLoadHeartbeat:
    def test_basic_load(self, tmp_dir):
        hb_dir = _write_heartbeat(tmp_dir, "my-hb")
        record = load_heartbeat(hb_dir)
        assert record.id == "my-hb"
        assert record.name == "my-hb"
        assert record.description == "Test heartbeat"
        assert record.schedule == "every 1 minute"
        assert record.enabled is True
        assert record.instructions.strip() == "Do something."
        assert isinstance(record.driver, HeartbeatDriver)

    async def test_driver_executes(self, tmp_dir):
        hb_dir = _write_heartbeat(tmp_dir, "exec-hb")
        record = load_heartbeat(hb_dir)
        events, new_state = await record.driver.check({})
        assert len(events) == 1
        assert events[0].name == "tick"
        assert new_state == {"count": 1}

    async def test_state_accumulates(self, tmp_dir):
        hb_dir = _write_heartbeat(tmp_dir, "accum-hb")
        record = load_heartbeat(hb_dir)
        _, s1 = await record.driver.check({})
        _, s2 = await record.driver.check(s1)
        assert s2 == {"count": 2}

    async def test_no_events_driver(self, tmp_dir):
        hb_dir = _write_heartbeat(tmp_dir, "quiet-hb", driver_code=DRIVER_NO_EVENTS)
        record = load_heartbeat(hb_dir)
        events, state = await record.driver.check({})
        assert events == []

    def test_missing_heartbeat_md(self, tmp_dir):
        hb_dir = tmp_dir / "no-md"
        hb_dir.mkdir()
        (hb_dir / "driver.py").write_text(DRIVER_CODE)
        with pytest.raises(FileNotFoundError, match="HEARTBEAT.md"):
            load_heartbeat(hb_dir)

    def test_missing_driver_py(self, tmp_dir):
        hb_dir = tmp_dir / "no-driver"
        hb_dir.mkdir()
        (hb_dir / "HEARTBEAT.md").write_text(
            "---\nname: no-driver\ndescription: x\nschedule: '@daily'\nenabled: true\n---\n"
        )
        with pytest.raises(FileNotFoundError, match="driver.py"):
            load_heartbeat(hb_dir)

    def test_name_mismatch_raises(self, tmp_dir):
        hb_dir = tmp_dir / "dir-name"
        hb_dir.mkdir()
        (hb_dir / "HEARTBEAT.md").write_text(
            "---\nname: wrong-name\ndescription: x\nschedule: '@daily'\nenabled: true\n---\n"
        )
        (hb_dir / "driver.py").write_text(DRIVER_CODE)
        with pytest.raises(ValueError, match="does not match directory"):
            load_heartbeat(hb_dir)

    def test_missing_schedule_raises(self, tmp_dir):
        hb_dir = tmp_dir / "no-sched"
        hb_dir.mkdir()
        (hb_dir / "HEARTBEAT.md").write_text(
            "---\nname: no-sched\ndescription: x\nenabled: true\n---\n"
        )
        (hb_dir / "driver.py").write_text(DRIVER_CODE)
        with pytest.raises(ValueError, match="schedule"):
            load_heartbeat(hb_dir)

    def test_driver_without_driver_class_raises(self, tmp_dir):
        bad_driver = "class NotDriver:\n    pass\n"
        hb_dir = _write_heartbeat(tmp_dir, "bad-driver", driver_code=bad_driver)
        with pytest.raises(AttributeError, match="Driver"):
            load_heartbeat(hb_dir)
