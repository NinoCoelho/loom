"""Per-turn / per-task ContextVars shared across loom tools.

These ContextVars carry routing state (which session a turn belongs to,
how deep the sub-agent recursion is) without making the agent loop
plumb that state through every tool handler signature.

ContextVars copy automatically into asyncio tasks spawned via
``asyncio.create_task`` / ``asyncio.gather``, so a fan-out runner can
``set()`` a different value per child task and each child sees its own.

:data:`CURRENT_SESSION_ID` is re-exported from :mod:`loom.hitl.broker`
so adopters can import either path; both refer to the same ContextVar
object.
"""

from __future__ import annotations

from contextvars import ContextVar

# Re-export so ``from loom.context import CURRENT_SESSION_ID`` works.
from loom.hitl.broker import CURRENT_SESSION_ID

# Current sub-agent nesting depth. 0 in the parent (top-level) turn,
# incremented each time spawn_subagents recurses. Read by the
# spawn_subagents tool handler to decide whether further nesting is
# allowed (capped at MAX_SUBAGENT_DEPTH in loom.tools.subagent).
# ContextVars copy into asyncio tasks, so the depth propagates through
# asyncio.gather without explicit plumbing.
SUBAGENT_DEPTH: ContextVar[int] = ContextVar("loom_subagent_depth", default=0)


__all__ = ["CURRENT_SESSION_ID", "SUBAGENT_DEPTH"]
