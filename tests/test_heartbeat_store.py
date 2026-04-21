import pytest

from loom.heartbeat.store import HeartbeatStore


@pytest.fixture
def store(tmp_dir):
    heartbeat_store = HeartbeatStore(tmp_dir / "heartbeats.sqlite")
    yield heartbeat_store
    heartbeat_store.close()


class TestHeartbeatStore:
    def test_get_run_missing(self, store):
        assert store.get_run("nonexistent") is None

    def test_get_state_missing_returns_empty(self, store):
        assert store.get_state("hb1") == {}

    def test_set_and_get_state(self, store):
        store.set_state("hb1", {"last_id": "abc123"})
        state = store.get_state("hb1")
        assert state == {"last_id": "abc123"}

    def test_overwrite_state(self, store):
        store.set_state("hb1", {"x": 1})
        store.set_state("hb1", {"x": 2, "y": 3})
        assert store.get_state("hb1") == {"x": 2, "y": 3}

    def test_touch_check(self, store):
        store.touch_check("hb1")
        run = store.get_run("hb1")
        assert run is not None
        assert run.last_check is not None
        assert run.last_fired is None

    def test_touch_fired_no_error(self, store):
        store.touch_fired("hb1")
        run = store.get_run("hb1")
        assert run.last_fired is not None
        assert run.last_error is None

    def test_touch_fired_with_error(self, store):
        store.touch_fired("hb1", error="connection refused")
        run = store.get_run("hb1")
        assert run.last_error == "connection refused"

    def test_list_runs_empty(self, store):
        assert store.list_runs() == []

    def test_list_runs_multiple(self, store):
        store.set_state("hb1", {"a": 1})
        store.set_state("hb2", {"b": 2})
        runs = store.list_runs()
        ids = {r.heartbeat_id for r in runs}
        assert ids == {"hb1", "hb2"}

    def test_instance_isolation(self, store):
        store.set_state("hb1", {"x": 1}, instance_id="inst-a")
        store.set_state("hb1", {"x": 99}, instance_id="inst-b")
        assert store.get_state("hb1", "inst-a") == {"x": 1}
        assert store.get_state("hb1", "inst-b") == {"x": 99}

    def test_delete(self, store):
        store.set_state("hb1", {"x": 1})
        store.delete("hb1")
        assert store.get_run("hb1") is None

    def test_delete_all(self, store):
        store.set_state("hb1", {}, instance_id="inst-a")
        store.set_state("hb1", {}, instance_id="inst-b")
        store.delete_all("hb1")
        assert store.list_runs() == []
