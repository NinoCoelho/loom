# Contributing to Loom

Thank you for your interest in contributing to Loom! This document provides guidelines and instructions.

## Development Setup

```bash
git clone https://github.com/your-org/loom.git
cd loom
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,anthropic]"
```

## Project Structure

```
loom/
  src/loom/          -- Framework source
    acp/             -- Agent Communication Protocol (WebSocket + Ed25519)
    config/          -- Configuration management
    hitl/            -- Human-in-the-Loop broker (web/SSE)
    llm/             -- LLM provider layer
    tools/           -- Tool handler abstractions
    skills/          -- Skill system
    search/          -- Web search providers (DDGS, Brave, Tavily, Google, composite)
    scrape/          -- Web scrape providers (Scrapling cascade)
    store/           -- Persistence (sessions, vault, secrets, memory)
    server/          -- FastAPI server factory
    routing/         -- Model routing and classification
    loop.py          -- Core agentic loop
    types.py         -- Shared types
    errors.py        -- Error classification
    retry.py         -- Retry logic
    home.py          -- Agent Home directory layout
    permissions.py   -- Agent permissions model
    prompt.py        -- PromptBuilder system
    runtime.py       -- Multi-agent lifecycle manager
  examples/tui/      -- Test TUI application
  docs/              -- Additional documentation
```

## Code Style

- Python 3.12+ with type annotations
- Pydantic v2 for data models
- No comments in code (self-documenting code only)
- `ruff` for linting and formatting
- Line length: 99 characters

## Running Tests

```bash
pip install -e ".[dev]"
pytest
```

## Running Linting

```bash
ruff check src/
ruff format src/
```

## Adding a New Tool

1. Create a new file in `src/loom/tools/` (or in your app)
2. Subclass `ToolHandler`
3. Implement the `tool` property (returns `ToolSpec`)
4. Implement `invoke(self, args: dict) -> ToolResult`
5. Register with `ToolRegistry.register(handler)`

```python
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec

class MyTool(ToolHandler):
    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="my_tool",
            description="Does something useful",
            parameters={
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        return ToolResult(text=f"Processed: {args['input']}")
```

## Adding a New LLM Provider

1. Create a new file in `src/loom/llm/`
2. Subclass `LLMProvider`
3. Implement `chat()` (required) and `chat_stream()` (optional)
4. Register via `ProviderRegistry.register(model_id, provider, upstream_model)`

## Commit Messages

- Use present tense: "add feature" not "added feature"
- Be concise and descriptive
- Reference issues when applicable

## Pull Request Process

1. Create a feature branch
2. Make your changes
3. Run linting and tests
4. Submit PR with a clear description

## License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.
