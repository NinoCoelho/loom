"""loom.auth.enforcer — PolicyEnforcer: gates credential access via policies + HITL.

``PolicyEnforcer.gate(scope, context)`` is called by ``CredentialResolver``
*before* the secret is retrieved from the store.  It checks the policy for
the scope and either allows, denies, or — for NOTIFY_BEFORE — blocks until a
human answers via the HITL broker.

Default behaviour when no policy is registered for a scope: **AUTONOMOUS
(allow)** with an INFO-level log.  This ensures backward compatibility with
pre-existing scopes that were set up before policies were introduced.

If the caller's ``context`` dict contains a ``"now"`` key (a
timezone-aware ``datetime``), the enforcer uses that value for time
comparisons.  Otherwise ``datetime.now(timezone.utc)`` is used.  This makes
tests deterministic without monkey-patching.

See docs/rfcs/0002-credentials-and-appliers.md for full design rationale.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loom.auth.policies import PolicyMode
from loom.hitl.broker import TIMEOUT_SENTINEL, HitlBroker, HitlEvent

if TYPE_CHECKING:
    from loom.auth.policies import CredentialPolicy
    from loom.auth.policy_store import PolicyStore
    from loom.store.secrets import SecretStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GateDecision + CredentialDenied
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    """Result of a ``PolicyEnforcer.gate()`` call.

    Attributes:
        allowed: Whether the credential may be used.
        policy: The policy that produced this decision (``None`` if no policy
            was configured for the scope — implicit AUTONOMOUS).
        prompt_resolution: The human's answer string when NOTIFY_BEFORE was
            triggered and the request was approved.  ``None`` otherwise.
        reason: Human-readable denial reason when ``allowed=False``.
    """

    allowed: bool
    policy: CredentialPolicy | None = None
    prompt_resolution: str | None = None
    reason: str | None = None


class CredentialDenied(Exception):
    """Raised by ``PolicyEnforcer.gate()`` or ``CredentialResolver.resolve_for()``
    when a policy forbids credential use."""

    def __init__(self, scope: str, reason: str) -> None:
        super().__init__(f"Credential denied for scope {scope!r}: {reason}")
        self.scope = scope
        self.reason = reason


# ---------------------------------------------------------------------------
# PolicyEnforcer
# ---------------------------------------------------------------------------

_HITL_SESSION_ID = "__policy_enforcer__"
"""Synthetic session id used for HITL requests that originate from the
enforcer (not from a running agent session).  In practice callers that embed
an enforcer inside a real agent session should pass a real session id via the
context dict ``{"hitl_session_id": sid}``."""


class PolicyEnforcer:
    """Evaluates a :class:`~loom.auth.policies.CredentialPolicy` for a scope and
    decides whether access is granted.

    Args:
        policy_store: Where to look up policies.
        hitl: Optional HITL broker.  Required for NOTIFY_BEFORE/NOTIFY_AFTER
            modes; if absent and the policy needs it, the call is denied.
        secret_store: Optional secret store used to auto-revoke ONE_SHOT
            credentials after the last use.
    """

    def __init__(
        self,
        policy_store: PolicyStore,
        hitl: HitlBroker | None = None,
        secret_store: SecretStore | None = None,
    ) -> None:
        self._policy_store = policy_store
        self._hitl = hitl
        self._secret_store = secret_store

    async def gate(self, scope: str, context: dict | None = None) -> GateDecision:
        """Evaluate the policy for *scope* and return a :class:`GateDecision`.

        Raises:
            CredentialDenied: if the policy denies access.

        The decision object is also returned (with ``allowed=True``) on
        success so callers can inspect ``prompt_resolution`` or attach the
        decision to the applier context.
        """
        ctx = context or {}
        now: datetime = ctx.get("now") or datetime.now(UTC)
        session_id: str = ctx.get("hitl_session_id", _HITL_SESSION_ID)

        policy = await self._policy_store.get(scope)

        if policy is None:
            logger.info(
                "No policy configured for scope %r — defaulting to AUTONOMOUS", scope
            )
            return GateDecision(allowed=True, policy=None)

        mode = policy.mode

        # ── AUTONOMOUS ──────────────────────────────────────────────
        if mode == PolicyMode.AUTONOMOUS:
            return GateDecision(allowed=True, policy=policy)

        # ── NOTIFY_BEFORE ────────────────────────────────────────────
        if mode == PolicyMode.NOTIFY_BEFORE:
            if self._hitl is None:
                raise CredentialDenied(scope, "no HITL broker configured")
            prompt = (
                policy.prompt_message
                or f"Allow agent to use credential {scope!r}?"
            )
            answer = await self._hitl.ask(
                session_id,
                prompt,
                kind="confirm",
                choices=["yes", "no"],
                default="no",
            )
            if answer == TIMEOUT_SENTINEL:
                raise CredentialDenied(scope, "HITL request timed out")
            if answer not in ("yes", "y"):
                raise CredentialDenied(scope, f"HITL request rejected (answer={answer!r})")
            return GateDecision(allowed=True, policy=policy, prompt_resolution=answer)

        # ── NOTIFY_AFTER ─────────────────────────────────────────────
        if mode == PolicyMode.NOTIFY_AFTER:
            if self._hitl is not None:
                # Fire-and-forget — do not block
                event = HitlEvent(
                    kind="credential_used",
                    data={
                        "scope": scope,
                        "mode": mode.value,
                        "message": f"Credential {scope!r} was used (notify_after).",
                    },
                )
                # Schedule as a non-blocking background emit
                asyncio.get_event_loop().call_soon(
                    self._hitl.publish, session_id, event
                )
            return GateDecision(allowed=True, policy=policy)

        # ── TIME_BOXED ───────────────────────────────────────────────
        if mode == PolicyMode.TIME_BOXED:
            start = policy.window_start
            end = policy.window_end
            if start is not None and now < start:
                raise CredentialDenied(
                    scope,
                    f"outside allowed window: now={now.isoformat()}"
                    f" < window_start={start.isoformat()}",
                )
            if end is not None and now >= end:
                raise CredentialDenied(
                    scope,
                    f"outside allowed window: now={now.isoformat()}"
                    f" >= window_end={end.isoformat()}",
                )
            return GateDecision(allowed=True, policy=policy)

        # ── ONE_SHOT ─────────────────────────────────────────────────
        if mode == PolicyMode.ONE_SHOT:
            remaining = policy.uses_remaining
            if remaining is None or remaining <= 0:
                raise CredentialDenied(scope, "one_shot credential has no uses remaining")
            # Decrement now (atomic via PolicyStore)
            new_remaining = await self._policy_store.decrement_uses(scope)
            if new_remaining == 0:
                # Revoke the secret if we have a store
                if self._secret_store is not None:
                    await self._secret_store.revoke(scope)
                else:
                    logger.warning(
                        "ONE_SHOT credential %r exhausted but no SecretStore configured"
                        " — cannot auto-revoke",
                        scope,
                    )
            return GateDecision(allowed=True, policy=policy)

        # Unreachable if PolicyMode is exhaustive, but be defensive
        raise CredentialDenied(scope, f"unknown policy mode: {mode!r}")
