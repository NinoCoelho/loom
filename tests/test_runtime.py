import pytest

from loom.loop import Agent, AgentConfig
from loom.runtime import AgentRuntime
from loom.permissions import AgentPermissions


@pytest.fixture
def runtime(tmp_dir):
    return AgentRuntime(tmp_dir / "loom")


def test_create_agent(runtime):
    agent = runtime.create_agent("test", AgentConfig(max_iterations=2))
    assert "test" in runtime.list_agents()
    assert runtime.get_agent("test") is agent


def test_create_multiple_agents(runtime):
    runtime.create_agent("a", AgentConfig())
    runtime.create_agent("b", AgentConfig())
    agents = runtime.list_agents()
    assert "a" in agents
    assert "b" in agents


def test_agent_home_initialized(runtime):
    runtime.create_agent("initialized", AgentConfig())
    home = runtime.get_agent_home("initialized")
    assert home is not None
    assert home.soul_path.exists()
    assert home.identity_path.exists()
    assert home.user_path.exists()
    assert home.skills_dir.exists()
    assert home.memory_dir.exists()


def test_session_store_per_agent(runtime):
    runtime.create_agent("s", AgentConfig())
    store = runtime.get_session_store("s")
    assert store is not None
    store.get_or_create("test-session", "Test")
    history = store.get_history("test-session")
    assert isinstance(history, list)


def test_memory_store_per_agent(runtime):
    runtime.create_agent("m", AgentConfig())
    store = runtime.get_memory_store("m")
    assert store is not None


def test_permissions_per_agent(runtime):
    perms = AgentPermissions(soul_writable=True, terminal_allowed=True)
    runtime.create_agent("p", AgentConfig(), permissions=perms)
    stored = runtime.get_agent_permissions("p")
    assert stored.soul_writable
    assert stored.terminal_allowed


def test_remove_agent(runtime):
    runtime.create_agent("to-remove", AgentConfig())
    assert "to-remove" in runtime.list_agents()
    assert runtime.remove_agent("to-remove")
    assert "to-remove" not in runtime.list_agents()
    assert runtime.get_session_store("to-remove") is None


def test_remove_nonexistent(runtime):
    assert not runtime.remove_agent("ghost")


def test_shared_skills_dir(runtime):
    assert runtime.shared_skills_dir.exists()


def test_get_nonexistent_agent(runtime):
    assert runtime.get_agent("ghost") is None


def test_get_nonexistent_home(runtime):
    assert runtime.get_agent_home("ghost") is None


def test_loom_home_property(runtime, tmp_dir):
    assert runtime.loom_home == tmp_dir / "loom"
