from __future__ import annotations

from loom.tools.base import ToolHandler, ToolResult
from loom.types import ChatMessage, Role, ToolSpec


class DelegateTool(ToolHandler):
    def __init__(self, runtime: object) -> None:
        self._runtime = runtime

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="delegate",
            description=(
                "Delegate a task to another agent by name. "
                "The target agent will process the message and return a result."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "agent": {
                        "type": "string",
                        "description": "Name of the target agent to delegate to",
                    },
                    "message": {
                        "type": "string",
                        "description": "The task or message to send to the target agent",
                    },
                    "context": {
                        "type": "object",
                        "description": "Optional context to pass to the target agent",
                    },
                },
                "required": ["agent", "message"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        target_name = args.get("agent", "")
        message = args.get("message", "")
        context = args.get("context")

        if not target_name:
            return ToolResult(text="error: missing required field 'agent'")
        if not message:
            return ToolResult(text="error: missing required field 'message'")

        available = self._runtime.list_agents()
        if target_name not in available:
            return ToolResult(
                text=(
                    f"error: agent '{target_name}' not found. "
                    f"Available agents: {', '.join(available)}"
                )
            )

        agent = self._runtime.get_agent(target_name)
        if agent is None:
            return ToolResult(text=f"error: agent '{target_name}' could not be loaded")

        messages = [ChatMessage(role=Role.USER, content=message)]
        try:
            turn = await agent.run_turn(messages, context=context)
        except Exception as e:
            return ToolResult(text=f"error: agent '{target_name}' failed: {e}")

        metadata = {
            "agent": target_name,
            "iterations": turn.iterations,
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "tool_calls": turn.tool_calls,
        }
        return ToolResult(text=turn.reply, metadata=metadata)
