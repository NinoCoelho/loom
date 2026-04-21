<p align="center">
  <img src="docs/images/loom.png" alt="Loom" width="200" />
</p>

<h1 align="center">Loom</h1>

<p align="center">
  <strong>The composable Python framework for building agentic applications.</strong>
</p>

<p align="center">
  <a href="https://opensource.org/licenses/MIT"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT" /></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Status: Alpha" />
</p>

<p align="center">
  Loom provides the core infrastructure every agent needs — an iterative LLM loop, tool calling, skill management, streaming, human-in-the-loop approvals, multi-provider support, agent identity, persistent memory, and multi-agent coordination. Wire the pieces you need. Ignore the rest.
</p>

---

## Why Loom?

Building a production agent means solving the same problems over and over: managing LLM conversations, dispatching tools, handling errors, streaming responses, letting humans approve actions, persisting memory across sessions, and coordinating multiple agents. Loom gives you battle-tested primitives for all of it without locking you into a specific domain model.

**Composition over inheritance.** You don't subclass `Agent`. You create one, wire in the tools/providers/skills you need, and call `run_turn()`. The framework stays out of your way.

**Provider-agnostic.** Works with OpenAI, Anthropic, Ollama, vLLM, Together, Groq — anything that speaks the OpenAI chat completions format, plus native Anthropic support. Route messages to the best model automatically.

**Production-minded.** Atomic file writes. Secret redaction with 30+ patterns. Security guard scanning on agent-authored content. Jittered retry with error classification. Crash safety by default.

---

## Features

- **Agentic Loop** — Iterative LLM call → tool dispatch → result → continue/stop with configurable iteration limits, hooks, and streaming
- **Tool System** — Pluggable `ToolHandler` abstraction with `ToolRegistry` dispatch. 7 built-in tools included
- **Skill System** — SKILL.md (YAML frontmatter + Markdown) with progressive disclosure, shared skill directories, and a security guard
- **Agent Home** — Structured directory per agent with identity files (SOUL.md, IDENTITY.md, USER.md), skills, memory, vault, sessions
- **Persistent Memory** — Hybrid retrieval: BM25 + salience (pinned/importance/access) + recency ranking, with pluggable embeddings
- **Multi-Agent** — `AgentRuntime` manages multiple agents with `DelegateTool` for inter-agent delegation
- **Multi-Provider** — OpenAI-compatible and Anthropic providers with `ProviderRegistry` and automatic model routing
- **Streaming** — Full SSE streaming with tool call assembly, 9 event types, and content deltas
- **Human-in-the-Loop** — Terminal prompts and web/SSE broker for confirm/choice/text questions and command approval
- **Agent Communication Protocol** — Call external agents over WebSocket with Ed25519 authentication
- **Session Persistence** — SQLite-backed per-agent session store with history, usage tracking, and search
- **Vault** — Filesystem + FTS5 full-text search knowledge base with YAML frontmatter
- **Secret Redaction** — 30+ patterns for API keys, tokens, connection strings
- **Error Classification** — Rich taxonomy with 15+ failover reasons mapping to recovery actions
- **Atomic Writes** — All file mutations use tempfile + rename for crash safety

---

## Quick Start

### Install

```bash
pip install loom
```

For Anthropic support:
```bash
pip install "loom[anthropic]"
```

For the ACP protocol:
```bash
pip install "loom[acp]"
```

For everything (development):
```bash
pip install -e ".[dev,anthropic,acp]"
```

### Minimal Agent

```python
import asyncio
from loom import Agent, AgentConfig, ChatMessage, Role, ToolRegistry
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

### Multi-Agent Setup

```python
from loom import AgentRuntime, AgentConfig, AgentPermissions

runtime = AgentRuntime()

supervisor = runtime.create_agent(
    "supervisor",
    AgentConfig(model="gpt-4o"),
    AgentPermissions(user_writable=True, delegate_allowed=True),
)

coder = runtime.create_agent(
    "coder",
    AgentConfig(model="gpt-4o"),
    AgentPermissions(skills_creatable=True, terminal_allowed=True),
)
```

### Agent Home Structure

```
~/.loom/
  shared-skills/           # Skills shared across all agents
  agents/
    supervisor/
      SOUL.md              # Purpose and values
      IDENTITY.md          # Name, role, tone
      USER.md              # Learned user preferences
      skills/              # Agent's own skills
      memory/              # Structured searchable memory
      vault/               # Knowledge base
      sessions.sqlite      # Per-agent session store
```

---

## Architecture

Loom is organized as 14 composable subsystems. Use what you need, ignore the rest.

| Subsystem | Module | Purpose |
|---|---|---|
| Agentic Loop | `loom.loop` | Iterative LLM call → tool dispatch → result → continue/stop |
| Tool System | `loom.tools` | Pluggable `ToolHandler` with registry dispatch |
| Skill System | `loom.skills` | Markdown-based skills with progressive disclosure and security guard |
| LLM Providers | `loom.llm` | OpenAI-compatible and Anthropic providers with registry |
| Streaming | `loom.types` | 9 event types for SSE streaming with tool call assembly |
| HITL | `loom.hitl` | Terminal prompts and web/SSE broker for human approvals |
| Memory | `loom.store.memory` | Hybrid BM25 + salience + recency retrieval with pluggable embeddings |
| Stores | `loom.store` | Session (SQLite), Vault (FTS5), Secrets (JSON) — all with atomic writes |
| Agent Home | `loom.home` | Structured per-agent directory with identity files |
| Multi-Agent | `loom.runtime` | AgentRuntime for multi-agent lifecycle and delegation |
| ACP | `loom.acp` | WebSocket inter-agent communication with Ed25519 auth |
| Server | `loom.server` | FastAPI factory with chat, streaming, session, and skill endpoints |
| Routing | `loom.routing` | Message classification and model selection |
| Config | `loom.config` | JSON config with CLI/env overlay resolution |

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed design documentation covering every subsystem, data flow diagrams, and design principles.

See [docs/API.md](docs/API.md) for the complete API reference with type signatures for every public class, method, and function.

---

## Documentation

| Document | Description |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Detailed design docs for all 14 subsystems with data flow |
| [docs/API.md](docs/API.md) | Complete API reference with type signatures |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, code style, and how to contribute |
| [CHANGELOG.md](CHANGELOG.md) | Version history in Keep-a-Changelog format |

---

## Development

```bash
git clone https://github.com/your-org/loom.git
cd loom
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,anthropic]"
```

**Run tests:**
```bash
pytest
```

**Lint:**
```bash
ruff check src/
ruff format src/
```

---

## Get Involved

<p align="center"><strong>Loom is in early alpha. We need your help to make it great.</strong></p>

This is the ground floor. Loom has a solid architectural foundation — 14 subsystems, ~9,000 lines of Python, 16 test files — but the most important work is still ahead. Here's where you can make a real impact:

### Test It Against Real Workflows

The framework works for the patterns we've built. Does it work for yours? We're looking for people to:

- **Build agents with Loom** and tell us what's missing, awkward, or broken. The best feedback comes from real use cases, not synthetic benchmarks.
- **Try different LLM providers** — we've tested OpenAI, Anthropic, and Ollama. Help us validate vLLM, Together, Groq, LM Studio, and others.
- **Stress the multi-agent runtime** — spin up 3, 5, 10 agents with delegation chains. Find the limits.
- **Test the memory system** — throw real data at `MemoryStore.recall()`. Does hybrid BM25 + salience ranking actually surface the right memories?

### Extend It

- **New tools** — File system operations, database queries, web scraping, code execution sandboxes. Every `ToolHandler` is a PR.
- **New providers** — Google Gemini, Mistral, Cohere. Implement `LLMProvider.chat()` and `chat_stream()`.
- **Embedding providers** — Wire in sentence-transformers, OpenAI embeddings, or your own model for better memory recall.
- **Server integrations** — WebSocket support, authentication middleware, rate limiting, production deployment guides.

### Harden It

- **CI/CD** — We don't have it yet. Help set up GitHub Actions for testing, linting, and publishing.
- **Error handling edge cases** — The error taxonomy covers 15+ scenarios. Real-world failures always find new ones.
- **Security review** — Audit the SkillGuard patterns, secret redaction regexes, and path traversal prevention.
- **Performance benchmarks** — Profile the agentic loop, memory recall, and streaming pipeline under load.

### Why Contribute?

- **You'll shape the API.** Early contributors have outsized influence on naming, patterns, and conventions.
- **Real systems engineering.** Loom touches async Python, SQLite, HTTP clients, WebSocket protocols, cryptography, LLM APIs, and streaming — it's a genuine systems project, not another wrapper.
- **Your agents get better.** If you're building agentic applications, contributing to the framework means your own tools improve.
- **MIT licensed, no CLA.** Fork it, use it, contribute back if you want.

### How to Start

1. Read [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup
2. Browse [ARCHITECTURE.md](ARCHITECTURE.md) to understand the subsystems
3. Pick an issue (or open one describing what you want to build)
4. Join the conversation — PRs, issues, and discussions are all welcome

**Every bug report, feature request, and "I tried this and it didn't work" story is valuable.** Open an issue. We're listening.

---

## License

MIT
