"""Session-scoped Human-in-the-Loop broker.

The existing :class:`loom.tools.hitl.AskUserTool` runs on a simple async
callback — great for CLI / TUI drivers where the process owns the
terminal. Web UIs need a different shape: the tool pauses on one side of
an HTTP boundary while the browser POSTs the answer on the other.

This subpackage ports the Future + pub/sub pattern so Loom can back a
web SSE integration without every adopter re-implementing it:

* :class:`HitlBroker` — per-session pending-request registry plus a
  pub/sub event bus. Tools call :meth:`HitlBroker.ask`; the web layer
  calls :meth:`HitlBroker.resolve` when the user answers.
* :class:`BrokerAskUserTool` — an ``ask_user`` ToolHandler bound to a
  broker, resolving the current session via a :class:`contextvars.ContextVar`.
* :data:`CURRENT_SESSION_ID` — set this in the server's chat handler so
  the tool knows which session to publish/park on.
"""

from loom.hitl.broker import (
    CURRENT_SESSION_ID,
    HitlBroker,
    HitlEvent,
    HitlRequest,
    TIMEOUT_SENTINEL,
)
from loom.hitl.tool import BrokerAskUserTool

__all__ = [
    "BrokerAskUserTool",
    "CURRENT_SESSION_ID",
    "HitlBroker",
    "HitlEvent",
    "HitlRequest",
    "TIMEOUT_SENTINEL",
]
