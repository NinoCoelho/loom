"""``spawn_subagents`` — parallel fan-out to fresh, isolated child loops.

Runs N sub-agents concurrently with their own message histories and tool
registries; only their final answers come back to the parent. The parent's
context grows by N short answers instead of N full transcripts, which is
the whole point of using sub-agents over inline tool calls for deep
research and parallel investigation.

This module owns:

* :data:`SPAWN_SUBAGENTS_TOOL_SPEC` — the ``ToolSpec`` the LLM sees.
* :class:`SubagentRunner` — the async callable contract the host must
  satisfy. The runner owns child-session creation, agent loop wiring,
  result capture, and history persistence — those bits are inherently
  storage-coupled and live in the host (e.g. nexus's app.py).
* :class:`SpawnSubagentsTool` — a :class:`ToolHandler` that validates
  args, enforces fan-out and depth caps, and delegates to the runner.

Depth tracking flows through :data:`loom.context.SUBAGENT_DEPTH`; the
runner is responsible for ``.set(depth + 1)`` inside each child task so
recursive calls are correctly capped.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from loom.context import CURRENT_SESSION_ID, SUBAGENT_DEPTH
from loom.tools.base import ToolHandler, ToolResult
from loom.types import ToolSpec

# Hard cap on nesting. Keeps a runaway sub-agent from spinning up its own
# fan-out and exploding the wall-clock / token cost. Default is 1: parent
# can spawn sub-agents, sub-agents cannot spawn further.
MAX_SUBAGENT_DEPTH = 1

# Hard cap on parallel fan-out per call. Keeps the LLM from accidentally
# spawning a swarm; deep research with more than 8 angles probably wants
# a different decomposition strategy anyway.
MAX_TASKS_PER_CALL = 8


SPAWN_SUBAGENTS_TOOL_SPEC = ToolSpec(
    name="spawn_subagents",
    description=(
        "Run one or more sub-agents in parallel with fresh, isolated contexts. "
        "Each sub-agent gets the full tool registry except ask_user/terminal "
        "and cannot recursively spawn further sub-agents. Returns only each "
        "sub-agent's final answer — their tool calls and intermediate output "
        "do NOT pollute your context window. Use this for deep research, "
        "parallel investigation across angles, or any task where you want to "
        "delegate work and consume only the conclusion. Each task's `prompt` "
        "must be self-contained: the sub-agent has no memory of this "
        f"conversation. Maximum {MAX_TASKS_PER_CALL} tasks per call."
    ),
    parameters={
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "Short label for this sub-task (logged, shown "
                                "in the parent's tool result for synthesis)."
                            ),
                        },
                        "prompt": {
                            "type": "string",
                            "description": (
                                "Self-contained instruction for the sub-agent. "
                                "Include all relevant scope/context — the "
                                "sub-agent has no memory of this conversation."
                            ),
                        },
                        "model_id": {
                            "type": "string",
                            "description": (
                                "Optional model override for this sub-agent. "
                                "Defaults to the parent's configured model."
                            ),
                        },
                    },
                    "required": ["prompt"],
                },
                "minItems": 1,
                "maxItems": MAX_TASKS_PER_CALL,
            },
        },
        "required": ["tasks"],
    },
)


class SubagentRunner(Protocol):
    """Host-supplied async callable that actually executes the sub-agents.

    Implementations create a child session per task, run the agent loop
    with a tool registry that excludes recursive ``spawn_subagents`` and
    HITL primitives, and return one result dict per task in the same
    order. Errors per task should be returned in the dict, not raised —
    the tool boundary should remain stable for the parent LLM.
    """

    async def __call__(
        self,
        tasks: list[dict[str, Any]],
        *,
        parent_session_id: str,
        depth: int,
    ) -> list[dict[str, Any]]:  # [{session_id, result, error}, ...]
        ...


async def handle_spawn_subagents(
    args: dict[str, Any],
    *,
    runner: SubagentRunner | None,
    parent_session_id: str | None,
    depth: int,
    max_tasks: int = MAX_TASKS_PER_CALL,
    max_depth: int = MAX_SUBAGENT_DEPTH,
) -> str:
    """Tool handler. Returns a JSON string for the LLM tool result channel.

    ``runner`` is the host-supplied callable. It is None inside a
    sub-agent's restricted registry so recursive spawning is refused at
    the handler boundary (a defence in depth alongside ``max_depth``).
    """
    if runner is None:
        return json.dumps({
            "ok": False,
            "error": (
                "spawn_subagents unavailable: runner not wired (sub-agents "
                "cannot spawn further sub-agents in v1)"
            ),
        })
    if parent_session_id is None:
        return json.dumps({
            "ok": False,
            "error": "spawn_subagents requires an active session context",
        })
    if depth >= max_depth:
        return json.dumps({
            "ok": False,
            "error": f"spawn_subagents depth limit reached (max {max_depth})",
        })

    tasks = args.get("tasks") or []
    if not isinstance(tasks, list) or not tasks:
        return json.dumps({"ok": False, "error": "`tasks` must be a non-empty array"})
    if len(tasks) > max_tasks:
        return json.dumps({
            "ok": False,
            "error": f"too many tasks ({len(tasks)}); max {max_tasks} per call",
        })
    for i, t in enumerate(tasks):
        if (
            not isinstance(t, dict)
            or not isinstance(t.get("prompt"), str)
            or not t["prompt"].strip()
        ):
            return json.dumps({
                "ok": False,
                "error": f"task[{i}] missing or empty `prompt`",
            })

    try:
        results = await runner(tasks, parent_session_id=parent_session_id, depth=depth)
    except Exception as exc:  # pragma: no cover — defensive
        return json.dumps({"ok": False, "error": f"subagent runner crashed: {exc!r}"})

    out = []
    for t, r in zip(tasks, results):
        out.append({
            "name": t.get("name"),
            "session_id": r.get("session_id"),
            "result": r.get("result", ""),
            "error": r.get("error"),
        })
    return json.dumps({"ok": True, "results": out})


# Optional lazy-runner type: the host may want to resolve the runner
# at dispatch time (late-binding) rather than at construction. Accepting
# either a SubagentRunner or a () → SubagentRunner|None getter keeps the
# tool flexible for both wiring styles.
RunnerGetter = Callable[[], SubagentRunner | None]


class SpawnSubagentsTool(ToolHandler):
    """Loom-native :class:`ToolHandler` for ``spawn_subagents``.

    The host either passes a concrete ``runner`` (eager) or a ``runner_getter``
    (late-bound — useful when the runner is constructed after the tool
    registry, as in nexus where the SessionStore exists only at server
    startup). At dispatch time we read :data:`CURRENT_SESSION_ID` and
    :data:`SUBAGENT_DEPTH` from contextvars — the runner is responsible
    for advancing depth in each child task.
    """

    def __init__(
        self,
        runner: SubagentRunner | None = None,
        *,
        runner_getter: RunnerGetter | None = None,
        max_tasks: int = MAX_TASKS_PER_CALL,
        max_depth: int = MAX_SUBAGENT_DEPTH,
    ) -> None:
        if runner is None and runner_getter is None:
            raise ValueError("SpawnSubagentsTool requires runner or runner_getter")
        self._runner = runner
        self._runner_getter = runner_getter
        self._max_tasks = max_tasks
        self._max_depth = max_depth

    @property
    def tool(self) -> ToolSpec:
        return SPAWN_SUBAGENTS_TOOL_SPEC

    async def invoke(self, args: dict) -> ToolResult:
        runner = self._runner if self._runner is not None else self._runner_getter()
        text = await handle_spawn_subagents(
            args,
            runner=runner,
            parent_session_id=CURRENT_SESSION_ID.get(),
            depth=SUBAGENT_DEPTH.get(),
            max_tasks=self._max_tasks,
            max_depth=self._max_depth,
        )
        # ``text`` is the JSON envelope. The LLM sees an "ok": false body for
        # validation errors; we still return a successful ToolResult so the
        # tool-call shape stays consistent (the model parses ok/error itself).
        return ToolResult(text=text)


__all__ = [
    "MAX_SUBAGENT_DEPTH",
    "MAX_TASKS_PER_CALL",
    "SPAWN_SUBAGENTS_TOOL_SPEC",
    "SpawnSubagentsTool",
    "SubagentRunner",
    "handle_spawn_subagents",
]


# Awaitable-typed helper for typing reads — keeps the Protocol focused
# on the call signature without re-stating Awaitable in every callsite.
_Aw = Awaitable
