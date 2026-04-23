"""ACP tool handler — exposes ``call_agent`` as a Loom tool callable."""

from __future__ import annotations

from loom.acp.client import AcpConfig, call_agent
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec


class AcpCallTool(ToolHandler):
    """Call an external agent over an ACP gateway.

    Usage
    -----
    >>> from loom.acp import AcpCallTool, AcpConfig
    >>> tool = AcpCallTool(AcpConfig.from_env())
    >>> registry.register(tool)

    When ``AcpConfig.gateway_url`` is empty the tool returns a friendly
    "not configured" message instead of failing — the agent degrades
    gracefully without a gateway.
    """

    def __init__(self, config: AcpConfig) -> None:
        self._config = config

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="acp_call",
            description="Call an external agent over the ACP gateway.",
            parameters={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Target agent ID."},
                    "message": {"type": "string", "description": "Message to send."},
                },
                "required": ["agent_id", "message"],
            },
        )

    async def invoke(self, args: dict) -> ToolResult:
        agent_id = args.get("agent_id", "")
        message = args.get("message", "")
        text = await call_agent(agent_id, message, self._config)
        return ToolResult(text=text)
