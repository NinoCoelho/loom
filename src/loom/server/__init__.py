"""HTTP server — ASGI app, SSE session events, and request/response schemas.

:func:`create_app` returns an ASGI application that serves the agent chat
endpoint, SSE session event stream, and admin routes (skills, config, etc.).

Key types:

* :class:`ChatRequest` / :class:`ChatReply` — JSON round-trip schemas.
* :class:`SessionInfo` — active session metadata.
* :class:`SessionEvent` — SSE event wrapper for the event-stream endpoint.
* :class:`SkillInfo` — skill summary exposed by the admin API.
"""

from loom.server.app import create_app as create_app
from loom.server.events import SessionEvent as SessionEvent
from loom.server.schemas import (
    ChatReply as ChatReply,
)
from loom.server.schemas import (
    ChatRequest as ChatRequest,
)
from loom.server.schemas import (
    RespondPayload as RespondPayload,
)
from loom.server.schemas import (
    SessionInfo as SessionInfo,
)
from loom.server.schemas import (
    SkillInfo as SkillInfo,
)

__all__ = [
    "create_app",
    "SessionEvent",
    "ChatReply",
    "ChatRequest",
    "RespondPayload",
    "SessionInfo",
    "SkillInfo",
]
