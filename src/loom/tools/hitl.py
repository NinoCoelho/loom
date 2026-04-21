from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec


class AskUserTool(ToolHandler):
    def __init__(
        self,
        handler: Callable[[str, str, list[str] | None], Coroutine[Any, Any, str]],
    ) -> None:
        self._handler = handler

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="ask_user",
            description="Ask the user a question and wait for their response.",
            parameters={
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["confirm", "choice", "text"],
                        "description": "Type of question",
                    },
                    "message": {
                        "type": "string",
                        "description": "Question to ask",
                    },
                    "choices": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Options for 'choice' kind",
                    },
                },
                "required": ["kind", "message"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        kind = args.get("kind", "")
        message = args.get("message", "")
        choices = args.get("choices")

        valid_kinds = {"confirm", "choice", "text"}
        if kind not in valid_kinds:
            return ToolResult(text=f"Invalid kind: {kind}. Must be one of {valid_kinds}")

        response = await self._handler(kind, message, choices)
        return ToolResult(text=response)


class TerminalTool(ToolHandler):
    def __init__(
        self,
        ask_handler: AskUserTool,
        default_timeout: int = 60,
        max_timeout: int = 600,
        max_output: int = 4000,
    ) -> None:
        self._ask = ask_handler
        self._default_timeout = default_timeout
        self._max_timeout = max_timeout
        self._max_output = max_output

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="terminal",
            description="Run a terminal command with optional approval.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                    },
                    "require_approval": {
                        "type": "boolean",
                        "default": True,
                        "description": "Whether to ask for user approval",
                    },
                },
                "required": ["command"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        command = args.get("command", "")
        timeout = min(args.get("timeout", self._default_timeout), self._max_timeout)
        require_approval = args.get("require_approval", True)

        if require_approval:
            approval = await self._ask.invoke(
                {"kind": "confirm", "message": f"Run command: {command}"}
            )
            if approval.text.lower().strip() not in ("y", "yes", "ok"):
                return ToolResult(text="Command rejected by user.")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            output = stdout.decode(errors="replace") if stdout else ""
            if len(output) > self._max_output:
                output = output[: self._max_output] + "\n... [truncated]"
            return ToolResult(text=output, metadata={"exit_code": proc.returncode})
        except TimeoutError:
            return ToolResult(text=f"Command timed out after {timeout}s")
        except Exception as e:
            return ToolResult(text=f"Command error: {e}")
