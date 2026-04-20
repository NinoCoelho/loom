# Loom

A reusable Python framework for building agentic applications with LLMs. Loom provides the core infrastructure for agentic loops, tool calling, skill management, streaming, human-in-the-loop (HITL) approvals, and multi-provider support.

## Features

- **Agentic Loop** -- Iterative LLM call -> tool dispatch -> result -> continue/stop cycle with configurable iteration limits
- **Tool System** -- Pluggable `ToolHandler` abstraction with a `ToolRegistry` for dispatch by name
- **Skill System** -- SKILL.md (YAML frontmatter + Markdown) with progressive disclosure, agent authoring, and security guard
- **Multi-Provider** -- OpenAI-compatible (works with Ollama, vLLM, Together, Groq, etc.) and Anthropic providers with a `ProviderRegistry`
- **Streaming** -- Full SSE streaming support with tool call assembly and content deltas
- **HITL** -- `ask_user` (confirm/choice/text) and `terminal` (approval-gated shell commands) tools
- **Model Routing** -- Message classification (coding/reasoning/trivial/balanced) with model selection
- **Session Persistence** -- SQLite-backed session store with history, usage tracking, and search
- **Vault** -- Filesystem + FTS5 full-text search knowledge base
- **Secret Redaction** -- 30+ patterns for API keys, tokens, connection strings
- **Error Classification** -- Rich error taxonomy with retry/abort/compress/fallback decisions
- **Atomic Writes** -- All file mutations use tempfile + rename for crash safety

## Quick Start

```bash
pip install loom
```

### Minimal Agent

```python
import asyncio
from loom import Agent, AgentConfig, ToolRegistry
from loom.llm.openai_compat import OpenAICompatibleProvider

async def main():
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1",
        default_model="llama3",
    )
    tools = ToolRegistry()
    config = AgentConfig(system_preamble="You are a helpful assistant.")

    agent = Agent(provider=provider, tool_registry=tools, config=config)
    messages = [ChatMessage(role=Role.USER, content="Hello!")]
    turn = await agent.run_turn(messages)
    print(turn.reply)

asyncio.run(main())
```

### With Tools

```python
from loom import ToolHandler, ToolResult, ToolSpec

class WeatherTool(ToolHandler):
    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="get_weather",
            description="Get current weather for a city",
            parameters={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        city = args["city"]
        return ToolResult(text=f"Weather in {city}: 72F, sunny")

tools = ToolRegistry()
tools.register(WeatherTool())
```

### With Skills

```python
from loom import SkillRegistry, SkillManager, SkillGuard
from pathlib import Path

skills = SkillRegistry(Path("~/.myapp/skills"))
skills.scan()

guard = SkillGuard()
manager = SkillManager(skills, guard)

result = manager.invoke({
    "action": "create",
    "name": "greet",
    "description": "How to greet users",
    "body": "1. Say hello warmly\n2. Ask how you can help",
})
```

### Multi-Provider with Routing

```python
from loom.llm.openai_compat import OpenAICompatibleProvider
from loom.llm.registry import ProviderRegistry
from loom.routing.router import choose_model, ModelStrengths

registry = ProviderRegistry()
registry.register("fast", OpenAICompatibleProvider("http://localhost:11434/v1", default_model="llama3"), "llama3")
registry.register("smart", OpenAICompatibleProvider("https://api.openai.com/v1", api_key="...", default_model="gpt-4o"), "gpt-4o")

strengths = {
    "fast": ModelStrengths(speed=10, cost=10, reasoning=3, coding=5),
    "smart": ModelStrengths(speed=3, cost=2, reasoning=10, coding=10),
}

best = choose_model("Explain why this code fails: def foo(): pass", registry, strengths)
```

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed design documentation.

## License

MIT
