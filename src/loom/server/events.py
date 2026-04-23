"""SSE session event wrapper.

:class:`SessionEvent` wraps a (kind, data) pair for transmission over the
server-sent events stream. Emitted by the session event bus in the FastAPI
app.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionEvent:
    kind: str
    data: dict

    def to_sse(self) -> str:
        import json

        return f"event: {self.kind}\ndata: {json.dumps(self.data)}\n\n"
