"""loom.auth.policies — credential usage policies for RFC 0002 Phase B.

Defines the policy data types that govern when an agent is allowed to use
a credential.  The ``PolicyMode`` enum has five modes (AUTONOMOUS through
ONE_SHOT); ``CredentialPolicy`` is a frozen dataclass that carries the
per-scope configuration.

See docs/rfcs/0002-credentials-and-appliers.md for full design rationale.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class PolicyMode(StrEnum):
    """Controls how the enforcer gates credential access."""

    AUTONOMOUS = "autonomous"
    """No gate — the agent may use the credential freely."""

    NOTIFY_BEFORE = "notify_before"
    """Human must approve each use via the HITL broker before the secret is released."""

    NOTIFY_AFTER = "notify_after"
    """Fire-and-log — the secret is released immediately; a HitlEvent is emitted for audit."""

    TIME_BOXED = "time_boxed"
    """Autonomous inside [window_start, window_end); denied outside the window."""

    ONE_SHOT = "one_shot"
    """Single-use: allowed once, then uses_remaining is decremented to 0 and the secret
    is auto-revoked via SecretStore.revoke()."""


@dataclass(frozen=True)
class CredentialPolicy:
    """Immutable policy configuration for a single scope.

    Args:
        scope: The loom scope this policy applies to.
        mode: Which enforcement mode to apply.
        window_start: Start of the allowed window (TIME_BOXED only).
        window_end: End of the allowed window (TIME_BOXED only).
        uses_remaining: Countdown counter (ONE_SHOT starts at 1; may be >1
            for counted policies).  ``None`` means unlimited (used by
            non-counted modes).
        prompt_message: Custom prompt shown to the human for NOTIFY_BEFORE.
            Defaults to ``"Allow agent to use credential {scope}?"`` if omitted.
    """

    scope: str
    mode: PolicyMode
    window_start: datetime | None = None
    window_end: datetime | None = None
    uses_remaining: int | None = None
    prompt_message: str | None = None
