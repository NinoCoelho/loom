from __future__ import annotations

from loom.tools.base import ToolResult


class _ToolError(Exception):
    def __init__(self, result: ToolResult) -> None:
        self.result = result


def _err(msg: str, error_class: str) -> ToolResult:
    return ToolResult(
        text=f"SSH error: {msg}",
        metadata={"exit_code": None, "error_class": error_class},
        is_error=True,
    )


def _valid_session_id(s: str) -> bool:
    if not s:
        return False
    return all(c.isalnum() or c in ("_", "-") for c in s)
