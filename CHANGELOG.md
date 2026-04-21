# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added

- `loom.mcp` subpackage: MCP (Model Context Protocol) client integration. `McpServerConfig`, `McpClient` (async context manager for stdio/SSE transports), and `McpToolHandler` let agents register and call tools exposed by external MCP servers.
- New optional extra: `pip install "loom[mcp]"` (depends on the official `mcp` SDK).

## [0.2.0] - 2025-01-20

### Added

- Agent Home directory: structured layout for agent identity, skills, memory, vault, sessions
- Identity files: SOUL.md (purpose/values), IDENTITY.md (name/role/tone), USER.md (preferences)
- PromptBuilder with composable PromptSection system and priority ordering
- AgentPermissions: configurable write access to SOUL.md, IDENTITY.md, USER.md
- EditIdentityTool: agent can edit profile files within permission bounds
- MemoryStore: structured, searchable memory with categories, tags, FTS5 (with LIKE fallback)
- MemoryToolHandler rewritten with read/write/search/list/delete actions
- Multi-skill registry: agent skills + shared skill directories, agent takes precedence
- AgentRuntime: multi-agent lifecycle manager with shared resources
- DelegateTool: agent-as-tool pattern for inter-agent delegation
- Agent-scoped sessions (each agent has its own SQLite session store)
- Memory preview auto-injection into system prompt (top 5 recent, 1500 char budget)
- Graceful FTS5 fallback when SQLite compiled without FTS5 support

### Changed

- `Agent` constructor now accepts `agent_home`, `permissions`, `memory_store` parameters
- `AgentConfig` now has `extra_tools` field for additional tool registration
- `SkillRegistry` now accepts `additional_dirs` for shared skill directories
- `Agent._build_system_prompt()` uses `PromptBuilder` with identity sections + memory preview
- `system_preamble` still works as fallback when no AgentHome is configured

## [0.1.0] - 2025-01-20

### Added

- Core agentic loop with configurable iterations and streaming support
- Tool system with `ToolHandler` ABC and `ToolRegistry` dispatch
- Skill system with SKILL.md format, progressive disclosure, and security guard
- LLM provider layer: OpenAI-compatible (raw httpx) and Anthropic (SDK)
- Multi-provider support via `ProviderRegistry`
- Human-in-the-loop tools: `ask_user` (confirm/choice/text) and `terminal`
- Model routing with message classification (coding/reasoning/trivial/balanced)
- SQLite session store with history persistence and usage tracking
- Vault store with FTS5 full-text search
- Secret redaction with 30+ patterns
- Error classification with retry/abort/compress/fallback decisions
- Jittered exponential backoff retry logic
- Atomic file writes for crash safety
- FastAPI server factory with chat, streaming, and session endpoints
- Configuration management with env overlay resolution
- Pending question tracking across turns
- Test TUI application for framework validation
