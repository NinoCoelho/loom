import pytest

from loom.loop import Agent, AgentConfig, AgentTurn
from loom.tools.registry import ToolRegistry


def test_agent_turn():
    turn = AgentTurn(reply="hello", iterations=1, model="test")
    assert turn.reply == "hello"
    assert turn.iterations == 1
    assert turn.skills_touched == []
    assert turn.model == "test"


def test_agent_config_defaults():
    config = AgentConfig()
    assert config.max_iterations == 32
    assert config.model is None
    assert config.system_preamble == ""
    assert config.extra_tools == []


def test_agent_config_custom():
    config = AgentConfig(max_iterations=10, model="gpt-4o", system_preamble="Be helpful")
    assert config.max_iterations == 10
    assert config.model == "gpt-4o"


def test_agent_no_provider_raises():
    agent = Agent(tool_registry=ToolRegistry(), config=AgentConfig())
    with pytest.raises(RuntimeError, match="No LLM provider"):
        agent._resolve_provider()


def test_agent_build_prompt_without_home():
    agent = Agent(
        tool_registry=ToolRegistry(),
        config=AgentConfig(system_preamble="I am a test assistant."),
    )
    prompt = agent._build_system_prompt()
    assert "test assistant" in prompt


def test_agent_build_prompt_with_home(agent_home, perms_full):
    from loom.store.memory import MemoryStore

    mem = MemoryStore(agent_home.memory_dir, agent_home.memory_index_db)
    try:
        agent = Agent(
            tool_registry=ToolRegistry(),
            agent_home=agent_home,
            permissions=perms_full,
            memory_store=mem,
            config=AgentConfig(max_iterations=1),
        )
        prompt = agent._build_system_prompt()
        assert "Soul" in prompt
        assert "Identity" in prompt
    finally:
        mem.close()


def test_agent_build_prompt_with_context(agent_home, perms_full):
    agent = Agent(
        tool_registry=ToolRegistry(),
        agent_home=agent_home,
        permissions=perms_full,
        config=AgentConfig(max_iterations=1),
    )
    prompt = agent._build_system_prompt(context={"target": "production"})
    assert "production" in prompt


def test_agent_extract_pending_question():
    agent = Agent(tool_registry=ToolRegistry(), config=AgentConfig())
    q = agent._extract_pending_question("Sure! Should I proceed with the deployment?")
    assert q is not None
    assert "proceed" in q


def test_agent_extract_no_question():
    agent = Agent(tool_registry=ToolRegistry(), config=AgentConfig())
    q = agent._extract_pending_question("Here is your result.")
    assert q is None


def test_agent_annotate_short_reply():
    agent = Agent(tool_registry=ToolRegistry(), config=AgentConfig())
    agent._pending_question = "Should I continue?"

    annotated = agent._annotate_short_reply("yes")
    assert annotated is not None
    assert "affirmative" in annotated

    annotated = agent._annotate_short_reply("no")
    assert annotated is not None
    assert "negative" in annotated

    annotated = agent._annotate_short_reply("I think we should use Python")
    assert annotated is None


def test_agent_home_property(agent_home):
    agent = Agent(
        tool_registry=ToolRegistry(),
        agent_home=agent_home,
        config=AgentConfig(),
    )
    assert agent.home is agent_home


def test_agent_permissions_property(perms_full):
    agent = Agent(
        tool_registry=ToolRegistry(),
        permissions=perms_full,
        config=AgentConfig(),
    )
    assert agent.permissions is perms_full
