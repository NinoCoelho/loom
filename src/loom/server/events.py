from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SessionEvent:
    kind: str
    data: dict

    def to_sse(self) -> str:
        import json
        return f"event: {self.kind}\ndata: {json.dumps(self.data)}\n\n"
