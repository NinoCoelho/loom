"""Broker-backed ``ask_user`` tool for web/SSE adopters."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from loom.hitl.broker import CURRENT_SESSION_ID, HitlBroker
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec

_VALID_KINDS = {"confirm", "choice", "text"}


class BrokerAskUserTool(ToolHandler):
    """Publishes a ``user_request`` to the broker and parks until the
    web layer calls ``HitlBroker.resolve``.

    Resolves the current session via :data:`CURRENT_SESSION_ID`; the
    server must ``.set()`` it before entering the agent loop.
    """

    def __init__(
        self,
        broker: HitlBroker,
        *,
        yolo_getter: Callable[[], bool] | None = None,
        default_timeout: int = 300,
    ) -> None:
        self._broker = broker
        self._yolo = yolo_getter or (lambda: False)
        self._default_timeout = default_timeout

    @property
    def tool(self) -> ToolSpec:
        return ToolSpec(
            name="ask_user",
            description=(
                "Pause the agent and ask the user a question. Returns the "
                "user's answer as a string, or '__timeout__' if they don't "
                "respond within the timeout — treat that as 'do not proceed'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": sorted(_VALID_KINDS),
                    },
                    "choices": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "default": {"type": "string"},
                    "timeout_seconds": {"type": "integer"},
                },
                "required": ["prompt"],
            },
        )

    async def invoke(self, args: dict[str, Any]) -> ToolResult:
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return _err("`prompt` must be a non-empty string")
        kind = args.get("kind", "confirm")
        if kind not in _VALID_KINDS:
            return _err(f"unsupported kind {kind!r}")
        choices = args.get("choices")
        if kind == "choice":
            if (
                not isinstance(choices, list)
                or not choices
                or not all(isinstance(c, str) and c for c in choices)
            ):
                return _err("kind='choice' requires a non-empty list of string choices")
        default = args.get("default")
        if default is not None and not isinstance(default, str):
            return _err("`default` must be a string if provided")
        timeout = args.get("timeout_seconds", self._default_timeout)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            return _err("`timeout_seconds` must be a positive number")

        session_id = CURRENT_SESSION_ID.get()
        if session_id is None:
            return _err(
                "ask_user is unavailable outside a live chat session — "
                "CURRENT_SESSION_ID context var is unset"
            )

        answer = await self._broker.ask(
            session_id,
            prompt,
            kind=kind,
            choices=choices if kind == "choice" else None,
            default=default,
            timeout_seconds=int(timeout),
            yolo=self._yolo(),
        )
        return ToolResult(text=answer)


def _err(message: str) -> ToolResult:
    payload = json.dumps({"ok": False, "error": message}, ensure_ascii=False)
    return ToolResult(text=payload, is_error=True)
