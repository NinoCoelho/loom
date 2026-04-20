from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

from loom.config.base import ConfigStore, LoomConfig, ProviderConfig
from loom.config.resolver import resolve_config
from loom.llm.openai_compat import OpenAICompatibleProvider
from loom.llm.registry import ProviderRegistry
from loom.loop import Agent, AgentConfig
from loom.skills.guard import SkillGuard
from loom.skills.registry import SkillRegistry
from loom.tools.base import ToolHandler, ToolResult
from loom.tools.hitl import AskUserTool, TerminalTool
from loom.tools.http import HttpCallTool
from loom.tools.memory import MemoryToolHandler
from loom.tools.registry import ToolRegistry
from loom.types import ChatMessage, Role, ToolSpec

app = typer.Typer(help="Loom TUI - Test application for the Loom agentic framework")
console = Console()

LOOM_DIR = Path.home() / ".loom-tui"


def _build_agent(config: LoomConfig) -> Agent:
    base_url, api_key, model = resolve_config(config=config)
    if not base_url and not model:
        console.print("[red]No LLM configured. Run 'loom-tui setup' or set LOOM_LLM_BASE_URL, LOOM_LLM_API_KEY, LOOM_LLM_MODEL env vars.[/red]")
        raise typer.Exit(1)

    provider = OpenAICompatibleProvider(
        base_url=base_url or "http://localhost:11434/v1",
        api_key=api_key or None,
        default_model=model or "gpt-4o",
    )

    registry = ProviderRegistry()
    model_id = model or "default"
    registry.register(model_id, provider, provider.default_model)

    tool_registry = ToolRegistry()
    tool_registry.register(HttpCallTool())

    memory_dir = LOOM_DIR / "memory"
    tool_registry.register(MemoryToolHandler(memory_dir))

    ask_user = AskUserTool(
        handler=_cli_ask_user,
    )
    tool_registry.register(ask_user)
    tool_registry.register(TerminalTool(ask_user))

    skills_dir = LOOM_DIR / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_registry = SkillRegistry(skills_dir)
    if any(skills_dir.iterdir()):
        skill_registry.scan()

    agent_config = AgentConfig(
        max_iterations=config.max_iterations,
        model=model_id,
        system_preamble=(
            "You are a helpful AI assistant powered by the Loom framework. "
            "You have access to tools for HTTP requests, terminal commands, memory, and skills. "
            "Be concise and direct. Use tools when they help accomplish the task."
        ),
    )

    return Agent(
        provider_registry=registry,
        tool_registry=tool_registry,
        skill_registry=skill_registry,
        config=agent_config,
    )


async def _cli_ask_user_handler(kind: str, message: str, choices: list[str] | None) -> str:
    if kind == "confirm":
        console.print(f"\n[bold yellow]? {message}[/bold yellow]")
        result = input("[y/n] > ").strip().lower()
        return result
    elif kind == "choice" and choices:
        console.print(f"\n[bold yellow]? {message}[/bold yellow]")
        for i, c in enumerate(choices, 1):
            console.print(f"  {i}. {c}")
        result = input("Choice > ").strip()
        try:
            idx = int(result) - 1
            if 0 <= idx < len(choices):
                return choices[idx]
        except ValueError:
            pass
        return result
    else:
        console.print(f"\n[bold yellow]? {message}[/bold yellow]")
        return input("> ").strip()


_cli_ask_user = _cli_ask_user_handler


@app.command()
def chat():
    """Start an interactive chat session."""
    config_store = ConfigStore(LOOM_DIR / "config.json")
    config = config_store.load()

    try:
        agent = _build_agent(config)
    except SystemExit:
        return

    console.print(Panel("Loom TUI - Agentic Chat", style="bold blue"))
    console.print("Type [bold]exit[/bold] or [bold]quit[/bold] to leave.\n")

    session_id = "tui-session"
    history: list[ChatMessage] = []
    prompt_session: PromptSession = PromptSession(history=FileHistory(str(LOOM_DIR / "history")))

    while True:
        try:
            user_input = prompt_session.prompt("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            break

        history.append(ChatMessage(role=Role.USER, content=user_input))

        with console.status("[bold green]Thinking...", spinner="dots"):
            try:
                turn = asyncio.run(agent.run_turn(history))
            except KeyboardInterrupt:
                console.print("[yellow]Interrupted.[/yellow]")
                history.pop()
                continue
            except Exception as e:
                console.print(f"[red]Error: {e}[/red]")
                history.pop()
                continue

        history.append(ChatMessage(role=Role.ASSISTANT, content=turn.reply))
        console.print()
        console.print(Panel(Markdown(turn.reply), title="Assistant", border_style="blue"))

        if turn.iterations > 1:
            console.print(
                f"[dim]Iterations: {turn.iterations} | "
                f"Tools: {turn.tool_calls} | "
                f"Tokens: {turn.input_tokens}+{turn.output_tokens}[/dim]"
            )
        if turn.skills_touched:
            console.print(f"[dim]Skills: {', '.join(turn.skills_touched)}[/dim]")
        console.print()

    console.print("[dim]Goodbye![/dim]")


@app.command()
def setup(
    base_url: str = typer.Option("", help="LLM API base URL"),
    api_key: str = typer.Option("", help="API key"),
    model: str = typer.Option("", help="Default model name"),
):
    """Configure the LLM provider."""
    config_store = ConfigStore(LOOM_DIR / "config.json")

    if not base_url:
        base_url = input("Base URL [http://localhost:11434/v1]: ").strip() or "http://localhost:11434/v1"
    if not model:
        model = input("Model name [gpt-4o]: ").strip() or "gpt-4o"
    if not api_key:
        api_key = input("API key (leave empty for local): ").strip()

    config = LoomConfig(
        default_model=model,
        providers={
            "default": ProviderConfig(
                base_url=base_url,
                api_key_inline=api_key,
                provider_type="openai_compat",
                default_model=model,
            )
        },
    )
    config_store.save(config)
    console.print("[green]Configuration saved![/green]")


@app.command()
def send(message: str):
    """Send a single message and print the response."""
    config_store = ConfigStore(LOOM_DIR / "config.json")
    config = config_store.load()

    try:
        agent = _build_agent(config)
    except SystemExit:
        return

    history = [ChatMessage(role=Role.USER, content=message)]
    turn = asyncio.run(agent.run_turn(history))
    console.print(Markdown(turn.reply))


if __name__ == "__main__":
    app()
