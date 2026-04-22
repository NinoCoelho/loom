<p align="center">
  <img src="docs/images/loom.png" alt="Loom" width="200" />
</p>

<h1 align="center">Loom</h1>

<p align="center">
  <strong>The composable Python framework for building agentic applications.</strong>
</p>

<p align="center">
  <a href="https://opensource.org/licenses/Apache-2.0"><img src="https://img.shields.io/badge/license-Apache--2.0-blue.svg" alt="License: Apache-2.0" /></a>
  <img src="https://img.shields.io/badge/python-3.12%2B-blue.svg" alt="Python 3.12+" />
  <img src="https://img.shields.io/badge/status-alpha-orange.svg" alt="Status: Alpha" />
</p>

<p align="center">
  Loom is a composable Python framework for building agentic applications — with a security model that actually holds. Credentials live in encrypted stores and are resolved into transport-ready material through an applier pipeline; the agent never touches secret bytes. Human-in-the-loop is a structural primitive, not a callback. Skills are loaded on demand to keep context clean. Recurring tasks run through stateless, event-driven drivers. No decorators, no base classes, no global state — just components you wire together.
</p>

---

## Build a chat agent in 7 steps

This guide takes you from a 10-line script to a fully-featured interactive chat agent — the same one in [`examples/tui`](examples/tui). Each step adds one layer.

### Install

```bash
pip install "git+https://github.com/NinoCoelho/loom.git"
```

For Anthropic support:
```bash
pip install "loom[anthropic] @ git+https://github.com/NinoCoelho/loom.git"
```

---

### Step 1 — Your first agent

This is the entire agentic loop. An `Agent` takes a provider, a tool registry, and a config, and runs conversations with `run_turn()`.

```python
import asyncio
from loom.loop import Agent, AgentConfig
from loom.llm.openai_compat import OpenAICompatibleProvider
from loom.tools.registry import ToolRegistry
from loom.types import ChatMessage, Role

async def main():
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1",  # or any OpenAI-compatible endpoint
        default_model="llama3",
    )

    agent = Agent(
        provider=provider,
        tool_registry=ToolRegistry(),
        config=AgentConfig(system_preamble="You are a helpful assistant."),
    )

    messages = [ChatMessage(role=Role.USER, content="Hello!")]
    turn = await agent.run_turn(messages)
    print(turn.reply)

asyncio.run(main())
```

`turn.reply` is the assistant's text. `turn.iterations`, `turn.tool_calls`, `turn.input_tokens`, and `turn.output_tokens` give you telemetry.

---

### Step 2 — Add a tool

Subclass `ToolHandler`, describe the tool with a `ToolSpec`, and implement `invoke()`. Register it with the `ToolRegistry`. The agent will call it automatically when the LLM decides to.

```python
from loom.tools.base import ToolHandler, ToolResult
from loom.tools.registry import ToolRegistry
from loom.types import ToolSpec

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
        return ToolResult(text=f"Weather in {city}: 72°F, sunny")

tools = ToolRegistry()
tools.register(WeatherTool())

agent = Agent(provider=provider, tool_registry=tools, config=config)
```

Loom handles the full tool call loop: it dispatches your handler, appends the result to the conversation, and keeps iterating until the LLM stops calling tools.

---

### Step 3 — Add memory

`MemoryToolHandler` gives your agent a persistent, searchable memory across sessions. Point it at a directory and register it as a tool — the agent will store and recall memories on its own.

```python
from pathlib import Path
from loom.tools.memory import MemoryToolHandler

memory_dir = Path.home() / ".myapp" / "memory"
tools.register(MemoryToolHandler(memory_dir))
```

Memory uses hybrid BM25 + salience + recency ranking. The agent decides when to save and recall — you just give it the tool.

---

### Step 4 — Add skills

Skills are Markdown files with YAML frontmatter. The agent can activate them by name to load reusable instructions mid-conversation — without burning them into the system prompt permanently.

```
~/.myapp/skills/
  summarize.md
  code-review.md
```

```markdown
---
name: summarize
description: How to summarize text clearly and concisely
---

1. Identify the key points — no more than five.
2. Write one sentence per point, plain language.
3. End with a single-sentence takeaway.
```

```python
from loom.skills.registry import SkillRegistry

skills_dir = Path.home() / ".myapp" / "skills"
skills_dir.mkdir(parents=True, exist_ok=True)

skill_registry = SkillRegistry(skills_dir)
skill_registry.scan()

agent = Agent(
    provider=provider,
    tool_registry=tools,
    skill_registry=skill_registry,
    config=config,
)
```

When the agent activates a skill, its body is injected into the conversation at that point. Skills can also be created, edited, and deleted by the agent itself via `SkillManager`.

---

### Step 5 — Add human-in-the-loop

`AskUserTool` lets the agent pause and ask the user a question. `TerminalTool` lets it run shell commands with approval. Both require you to provide the handler — you own the UI.

```python
from loom.tools.hitl import AskUserTool, TerminalTool

async def ask_user_handler(kind: str, message: str, choices: list[str] | None) -> str:
    print(f"\n? {message}")
    if kind == "confirm":
        return input("[y/n] > ").strip()
    elif kind == "choice" and choices:
        for i, c in enumerate(choices, 1):
            print(f"  {i}. {c}")
        idx = int(input("Choice > ").strip()) - 1
        return choices[idx]
    return input("> ").strip()

ask_user = AskUserTool(handler=ask_user_handler)
tools.register(ask_user)
tools.register(TerminalTool(ask_user))  # TerminalTool uses AskUserTool for approvals
```

---

### Step 6 — Add credentials

Store secrets, resolve them into transport-ready headers, and gate usage with a policy — all without the agent ever touching the secret bytes.

**Store a secret:**

```python
from pathlib import Path
from loom.store.secrets import SecretStore

store = SecretStore(path=Path.home() / ".myapp" / "secrets.db")
await store.put("my-api", {"type": "api_key", "value": "sk-..."})
```

The store writes an encrypted file at the path you choose (Fernet at-rest, key auto-generated at `secrets.db/../keys/secrets.key`). Override the key with `LOOM_SECRET_KEY` env var.

**Resolve headers automatically via `CredentialResolver` + `HttpCallTool`:**

```python
from loom.auth.appliers import ApiKeyHeaderApplier
from loom.auth.resolver import CredentialResolver
from loom.tools.http import HttpCallTool

resolver = CredentialResolver(store)
resolver.register(ApiKeyHeaderApplier(header_name="Authorization"), transport="http")

async def auth_hook(req: dict) -> dict:
    headers = await resolver.resolve_for("my-api", "http")
    return {**req, "headers": {**req["headers"], **headers}}

tools.register(HttpCallTool(pre_request_hook=auth_hook))
```

The hook runs before every HTTP request. The agent calls `http_call` with a URL and method; the hook injects the header. The agent never sees the key value.

**Add a policy (optional — AUTONOMOUS by default):**

```python
from loom.auth.enforcer import PolicyEnforcer
from loom.auth.policies import CredentialPolicy, PolicyMode
from loom.auth.policy_store import PolicyStore

policy_store = PolicyStore(path=Path.home() / ".myapp" / "policies.json")
await policy_store.put(CredentialPolicy(scope="my-api", mode=PolicyMode.NOTIFY_BEFORE))

enforcer = PolicyEnforcer(policy_store=policy_store, hitl=hitl_broker)
resolver = CredentialResolver(store, enforcer=enforcer)
```

`NOTIFY_BEFORE` blocks the request and fires a HITL prompt before releasing the secret. Other modes: `AUTONOMOUS` (no gate), `NOTIFY_AFTER` (fire-and-log), `TIME_BOXED` (allowed inside a datetime window), `ONE_SHOT` (single use, then auto-revoked).

---

### Step 7 — Put it all together

The previous steps each added one capability. Here's what a complete agent looks like when you wire everything into a single interactive chat loop — the same pattern behind the full TUI example.

```python
import asyncio
from pathlib import Path

from loom.loop import Agent, AgentConfig
from loom.llm.openai_compat import OpenAICompatibleProvider
from loom.skills.registry import SkillRegistry
from loom.tools.hitl import AskUserTool, TerminalTool
from loom.tools.memory import MemoryToolHandler
from loom.tools.registry import ToolRegistry
from loom.types import ChatMessage, Role

APP_DIR = Path.home() / ".myapp"

async def ask_user_handler(kind: str, message: str, choices: list[str] | None) -> str:
    print(f"\n? {message}")
    if kind == "confirm":
        return input("[y/n] > ").strip()
    elif kind == "choice" and choices:
        for i, c in enumerate(choices, 1):
            print(f"  {i}. {c}")
        return choices[int(input("Choice > ").strip()) - 1]
    return input("> ").strip()

async def main():
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:11434/v1",
        default_model="llama3",
    )

    tools = ToolRegistry()
    tools.register(MemoryToolHandler(APP_DIR / "memory"))
    ask_user = AskUserTool(handler=ask_user_handler)
    tools.register(ask_user)
    tools.register(TerminalTool(ask_user))

    skills_dir = APP_DIR / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_registry = SkillRegistry(skills_dir)
    skill_registry.scan()

    agent = Agent(
        provider=provider,
        tool_registry=tools,
        skill_registry=skill_registry,
        config=AgentConfig(system_preamble="You are a helpful assistant."),
    )

    history: list[ChatMessage] = []
    print("Type 'quit' to exit.\n")

    while True:
        user_input = input("You> ").strip()
        if not user_input or user_input.lower() in ("quit", "exit", "q"):
            break

        history.append(ChatMessage(role=Role.USER, content=user_input))
        turn = await agent.run_turn(history)
        history.append(ChatMessage(role=Role.ASSISTANT, content=turn.reply))

        print(f"\nAssistant: {turn.reply}\n")

asyncio.run(main())
```

That's a fully working persistent agent with memory, skills, human-in-the-loop, and credentials support — under 70 lines.

---

## What's next

The steps above cover the most common patterns. Loom has more:

| What | Where |
|---|---|
| Full TUI with rich formatting and history | [`examples/tui`](examples/tui) |
| Anthropic Claude provider | `loom.llm.anthropic` |
| Multi-agent runtime with delegation | `loom.runtime` |
| FastAPI server with SSE streaming | `loom.server` |
| Agent Communication Protocol (WebSocket) | `loom.acp` |
| MCP client (external tool servers) | `loom.mcp` |
| Multi-provider registry with model routing | `loom.llm.registry`, `loom.routing` |
| Agent home (identity files, vault, sessions) | `loom.home` |
| Credentials — typed secrets (8 types), 8 appliers (HTTP/SSH/AWS/JWT), resolver, 5 HITL policy modes, OS keychain backend | `loom.auth`, `loom.store.secrets`, `loom.store.keychain` |
| SSH tool — run commands on remote hosts; auth via credential pipeline | `loom.tools.ssh` (`loom[ssh]`) |
| Recurring tasks — cron/interval-scheduled drivers that detect events and trigger agent runs | `loom.heartbeat` |
| GraphRAG — knowledge-graph-augmented retrieval with vector search, entity extraction, and context injection | `loom.store.graphrag` (`loom[graphrag]`) |

See [ARCHITECTURE.md](ARCHITECTURE.md) for detailed design documentation and [docs/API.md](docs/API.md) for the complete API reference.

---

## Why Loom?

Most agent frameworks solve the "run a loop and call tools" problem. Loom does that too, but the design choices that matter are the ones that come up when you move past demos:

**Credentials that stay secret — by design.**
Secrets live in an encrypted store (or OS keychain). The agent never touches them. A resolver pipeline converts them into transport-ready headers, SSH connection args, or AWS signatures via typed appliers — and an optional policy layer (`NOTIFY_BEFORE`, `ONE_SHOT`, `TIME_BOXED`) gates each use through a HITL approval before the secret is released. This isn't bolted on. It's structural.

**Human-in-the-loop as a first-class primitive.**
Not a callback or a middleware. `AskUserTool` parks on an asyncio Future and emits SSE events; the same mechanism gates credential access in `PolicyEnforcer`. The agent can block mid-turn, wait for a human answer, and continue — in both terminal and web contexts.

**Recurring tasks with event-driven drivers.**
`loom.heartbeat` gives agents a cron/interval scheduler where driver code is pure: `check(state) -> (events, new_state)`. The runtime owns state persistence. When events fire, the agent runs with context. Agents can create their own heartbeats at runtime via tool call.

**Skills that don't bloat the context window.**
Skills are Markdown files. The agent sees only names and descriptions in the system prompt; full bodies are injected on demand when the agent activates one. At scale, this matters.

**Composition, not convention.**
There are no base classes to inherit, no decorators, no global registries. You construct components, configure them, and wire them together. The framework owns the loop; everything else is yours.

**Honest about what it isn't — yet.** Loom is early alpha. The primitives are solid; the ecosystem is not. If you're looking for 200 pre-built integrations, look elsewhere. If you want a framework whose security model you can actually audit and extend, this is the one.

---

## Get Involved

<p align="center"><strong>Loom is in early alpha. We need your help to make it great.</strong></p>

The core primitives are in place. What's missing is breadth — more providers, more tools, more real-world stress testing. Here's where you can help most:

- **Build something.** The best feedback comes from real use cases. Try it, break it, tell us what's missing.
- **Add a provider.** Gemini, Mistral, Cohere — implement `LLMProvider.chat()` and `chat_stream()`.
- **Add tools.** File operations, database queries, code execution sandboxes. Every `ToolHandler` is a PR.
- **Stress the runtime.** Spin up multi-agent delegation chains and find the limits.
- **Audit security.** Review `SkillGuard` patterns, secret redaction regexes, and path traversal prevention.

Read [CONTRIBUTING.md](CONTRIBUTING.md) to get started. Open an issue. Every bug report and "I tried this and it didn't work" story is valuable.

---

## Development

```bash
git clone https://github.com/NinoCoelho/loom.git
cd loom
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,anthropic,acp,mcp]"
```

```bash
pytest          # run tests
ruff check src/ # lint
ruff format src/
```

---

## License

Apache License 2.0
