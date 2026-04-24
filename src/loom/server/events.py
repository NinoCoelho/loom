"""SSE event wrapper and serialisation utilities.

:class:`SessionEvent` wraps a (kind, data) pair for transmission over
the server-sent events stream.

:func:`serialize_event` converts a Pydantic ``StreamEvent`` into a
JSON dict suitable for SSE, optionally injecting ``session_id``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class SessionEvent:
    kind: str
    data: dict

    def to_sse(self) -> str:
        import json

        return f"event: {self.kind}\ndata: {json.dumps(self.data)}\n\n"


def serialize_event(
    event: Any,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Convert a StreamEvent to a JSON-safe dict for SSE.

    Args:
        event: A Pydantic StreamEvent instance (or any object with a
            ``type`` attribute — the ``serialize_event`` hook in
            ``AgentConfig`` may have already transformed it).
        session_id: Injected into every event dict when not ``None``.

    Returns:
        A dict ready for ``json.dumps`` in an SSE ``data:`` line.
    """
    # If a custom serializer already produced a dict, enrich and return.
    if isinstance(event, dict):
        if session_id is not None:
            event.setdefault("session_id", session_id)
        return event

    # Pydantic model → dict via model_dump, then enrich.
    if hasattr(event, "model_dump"):
        data = event.model_dump(mode="json")
    elif hasattr(event, "__dict__"):
        data = dict(vars(event))
    else:
        data = {"type": getattr(event, "type", "unknown")}

    if session_id is not None:
        data["session_id"] = session_id

    return data
