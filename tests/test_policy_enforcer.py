"""Tests for loom.auth.enforcer.PolicyEnforcer (RFC 0002 Phase B).

Uses a FakeHitlBroker test double that records requests and lets the test
inject scripted answers.  FakeSecretStore is a minimal spy to verify
revoke() calls.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loom.auth.enforcer import CredentialDenied, PolicyEnforcer
from loom.auth.policies import CredentialPolicy, PolicyMode
from loom.auth.policy_store import PolicyStore
from loom.hitl.broker import TIMEOUT_SENTINEL, HitlEvent

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeHitlBroker:
    """Records ask() calls and returns scripted answers."""

    def __init__(self, answer: str = "yes") -> None:
        self.answer = answer
        self.calls: list[dict] = []
        self.published_events: list[tuple[str, HitlEvent]] = []

    async def ask(
        self,
        session_id: str,
        prompt: str,
        *,
        kind: str = "confirm",
        choices: list[str] | None = None,
        default: str | None = None,
        timeout_seconds: int = 300,
        yolo: bool = False,
    ) -> str:
        self.calls.append(
            {
                "session_id": session_id,
                "prompt": prompt,
                "kind": kind,
                "choices": choices,
                "default": default,
            }
        )
        return self.answer

    def publish(self, session_id: str, event: HitlEvent) -> None:
        self.published_events.append((session_id, event))


class FakeSecretStore:
    """Minimal spy: records revoke() calls."""

    def __init__(self) -> None:
        self.revoked: list[str] = []

    async def revoke(self, scope: str) -> bool:
        self.revoked.append(scope)
        return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _store(tmp_path: Path) -> PolicyStore:
    return PolicyStore(tmp_path / "policies.json")


def _now() -> datetime:
    return datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# AUTONOMOUS
# ---------------------------------------------------------------------------


async def test_autonomous_allows(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.AUTONOMOUS))
    enforcer = PolicyEnforcer(policy_store=ps)
    decision = await enforcer.gate("svc")
    assert decision.allowed is True
    assert decision.policy is not None
    assert decision.policy.mode == PolicyMode.AUTONOMOUS


# ---------------------------------------------------------------------------
# No policy → implicit AUTONOMOUS
# ---------------------------------------------------------------------------


async def test_no_policy_defaults_to_autonomous(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ps = _store(tmp_path)
    enforcer = PolicyEnforcer(policy_store=ps)
    with caplog.at_level(logging.INFO, logger="loom.auth.enforcer"):
        decision = await enforcer.gate("unknown-scope")
    assert decision.allowed is True
    assert decision.policy is None
    assert any("AUTONOMOUS" in r.message or "defaulting" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# NOTIFY_BEFORE
# ---------------------------------------------------------------------------


async def test_notify_before_no_hitl_raises(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.NOTIFY_BEFORE))
    enforcer = PolicyEnforcer(policy_store=ps, hitl=None)
    with pytest.raises(CredentialDenied) as exc_info:
        await enforcer.gate("svc")
    assert "no HITL broker configured" in str(exc_info.value)


async def test_notify_before_hitl_approved(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.NOTIFY_BEFORE))
    broker = FakeHitlBroker(answer="yes")
    enforcer = PolicyEnforcer(policy_store=ps, hitl=broker)  # type: ignore[arg-type]
    decision = await enforcer.gate("svc")
    assert decision.allowed is True
    assert decision.prompt_resolution == "yes"
    assert len(broker.calls) == 1


async def test_notify_before_hitl_rejected(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.NOTIFY_BEFORE))
    broker = FakeHitlBroker(answer="no")
    enforcer = PolicyEnforcer(policy_store=ps, hitl=broker)  # type: ignore[arg-type]
    with pytest.raises(CredentialDenied) as exc_info:
        await enforcer.gate("svc")
    assert "rejected" in str(exc_info.value)


async def test_notify_before_hitl_timeout(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.NOTIFY_BEFORE))
    broker = FakeHitlBroker(answer=TIMEOUT_SENTINEL)
    enforcer = PolicyEnforcer(policy_store=ps, hitl=broker)  # type: ignore[arg-type]
    with pytest.raises(CredentialDenied) as exc_info:
        await enforcer.gate("svc")
    assert "timed out" in str(exc_info.value)


async def test_notify_before_custom_prompt_message(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(
        CredentialPolicy(
            scope="svc",
            mode=PolicyMode.NOTIFY_BEFORE,
            prompt_message="Custom: approve svc?",
        )
    )
    broker = FakeHitlBroker(answer="yes")
    enforcer = PolicyEnforcer(policy_store=ps, hitl=broker)  # type: ignore[arg-type]
    await enforcer.gate("svc")
    assert broker.calls[0]["prompt"] == "Custom: approve svc?"


# ---------------------------------------------------------------------------
# NOTIFY_AFTER
# ---------------------------------------------------------------------------


async def test_notify_after_allows_immediately(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.NOTIFY_AFTER))
    broker = FakeHitlBroker()
    enforcer = PolicyEnforcer(policy_store=ps, hitl=broker)  # type: ignore[arg-type]
    decision = await enforcer.gate("svc")
    assert decision.allowed is True
    # ask() must NOT have been called (fire-and-forget)
    assert broker.calls == []


async def test_notify_after_emits_event(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.NOTIFY_AFTER))
    broker = FakeHitlBroker()
    enforcer = PolicyEnforcer(policy_store=ps, hitl=broker)  # type: ignore[arg-type]
    await enforcer.gate("svc")
    # Give the event loop a tick to process the call_soon callback
    await asyncio.sleep(0)
    assert len(broker.published_events) == 1
    _, event = broker.published_events[0]
    assert event.kind == "credential_used"


# ---------------------------------------------------------------------------
# TIME_BOXED
# ---------------------------------------------------------------------------


async def test_time_boxed_inside_window_allowed(tmp_path: Path) -> None:
    start = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
    end = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
    ps = _store(tmp_path)
    await ps.put(
        CredentialPolicy(
            scope="svc", mode=PolicyMode.TIME_BOXED, window_start=start, window_end=end
        )
    )
    enforcer = PolicyEnforcer(policy_store=ps)
    # now = 12:00, inside [10:00, 14:00)
    decision = await enforcer.gate("svc", context={"now": _now()})
    assert decision.allowed is True


async def test_time_boxed_before_window_denied(tmp_path: Path) -> None:
    start = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
    end = datetime(2024, 6, 15, 18, 0, 0, tzinfo=UTC)
    ps = _store(tmp_path)
    await ps.put(
        CredentialPolicy(
            scope="svc", mode=PolicyMode.TIME_BOXED, window_start=start, window_end=end
        )
    )
    enforcer = PolicyEnforcer(policy_store=ps)
    with pytest.raises(CredentialDenied) as exc_info:
        await enforcer.gate("svc", context={"now": _now()})
    assert "window_start" in str(exc_info.value)


async def test_time_boxed_after_window_denied(tmp_path: Path) -> None:
    start = datetime(2024, 6, 15, 8, 0, 0, tzinfo=UTC)
    end = datetime(2024, 6, 15, 11, 0, 0, tzinfo=UTC)
    ps = _store(tmp_path)
    await ps.put(
        CredentialPolicy(
            scope="svc", mode=PolicyMode.TIME_BOXED, window_start=start, window_end=end
        )
    )
    enforcer = PolicyEnforcer(policy_store=ps)
    with pytest.raises(CredentialDenied) as exc_info:
        # now = 12:00, past end 11:00
        await enforcer.gate("svc", context={"now": _now()})
    assert "window_end" in str(exc_info.value)


async def test_time_boxed_injected_now(tmp_path: Path) -> None:
    start = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
    end = datetime(2024, 6, 15, 14, 0, 0, tzinfo=UTC)
    ps = _store(tmp_path)
    await ps.put(
        CredentialPolicy(
            scope="svc", mode=PolicyMode.TIME_BOXED, window_start=start, window_end=end
        )
    )
    enforcer = PolicyEnforcer(policy_store=ps)
    # Inject a time outside the window to ensure context["now"] is used
    outside = datetime(2024, 6, 15, 20, 0, 0, tzinfo=UTC)
    with pytest.raises(CredentialDenied):
        await enforcer.gate("svc", context={"now": outside})


# ---------------------------------------------------------------------------
# ONE_SHOT
# ---------------------------------------------------------------------------


async def test_one_shot_uses_remaining_1_allowed_then_revoked(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.ONE_SHOT, uses_remaining=1))
    secret_store = FakeSecretStore()
    enforcer = PolicyEnforcer(policy_store=ps, secret_store=secret_store)  # type: ignore[arg-type]
    decision = await enforcer.gate("svc")
    assert decision.allowed is True
    # Decremented to 0 → revoke called
    updated = await ps.get("svc")
    assert updated is not None
    assert updated.uses_remaining == 0
    assert "svc" in secret_store.revoked


async def test_one_shot_uses_remaining_0_denied(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.ONE_SHOT, uses_remaining=0))
    enforcer = PolicyEnforcer(policy_store=ps)
    with pytest.raises(CredentialDenied) as exc_info:
        await enforcer.gate("svc")
    assert "no uses remaining" in str(exc_info.value)


async def test_one_shot_no_secret_store_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.ONE_SHOT, uses_remaining=1))
    enforcer = PolicyEnforcer(policy_store=ps, secret_store=None)
    with caplog.at_level(logging.WARNING, logger="loom.auth.enforcer"):
        decision = await enforcer.gate("svc")
    assert decision.allowed is True
    assert any(
        "cannot auto-revoke" in r.message or "no SecretStore" in r.message
        for r in caplog.records
    )


async def test_one_shot_uses_remaining_3_decrements_no_revoke(tmp_path: Path) -> None:
    ps = _store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.ONE_SHOT, uses_remaining=3))
    secret_store = FakeSecretStore()
    enforcer = PolicyEnforcer(policy_store=ps, secret_store=secret_store)  # type: ignore[arg-type]
    decision = await enforcer.gate("svc")
    assert decision.allowed is True
    updated = await ps.get("svc")
    assert updated is not None
    assert updated.uses_remaining == 2
    # Not yet at 0 → no revoke
    assert secret_store.revoked == []
