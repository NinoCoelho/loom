"""Tests for CredentialResolver + PolicyEnforcer integration (RFC 0002 Phase B).

Covers: enforcer allows → applier called; enforcer denies → CredentialDenied raised
and store.get not called; enforcer=None retains Phase A behaviour; _gate_decision
injected into applier context.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from loom.auth.enforcer import CredentialDenied, GateDecision, PolicyEnforcer
from loom.auth.policies import CredentialPolicy, PolicyMode
from loom.auth.policy_store import PolicyStore
from loom.auth.resolver import CredentialResolver
from loom.store.secrets import ApiKeySecret, SecretStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _secret_store(tmp_path: Path) -> SecretStore:
    return SecretStore(
        path=tmp_path / "secrets.db",
        key_path=tmp_path / "keys" / "secrets.key",
    )


def _policy_store(tmp_path: Path) -> PolicyStore:
    return PolicyStore(tmp_path / "policies.json")


# ---------------------------------------------------------------------------
# Capturing applier
# ---------------------------------------------------------------------------


class CapturingApplier:
    secret_type = "api_key"
    last_context: dict = {}

    async def apply(self, secret: Any, context: dict) -> str:
        CapturingApplier.last_context = dict(context)
        return secret["value"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_enforcer_allows_resolver_returns_value(tmp_path: Path) -> None:
    ss = _secret_store(tmp_path)
    ps = _policy_store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.AUTONOMOUS))
    secret: ApiKeySecret = {"type": "api_key", "value": "my-key"}
    await ss.put("svc", secret)

    enforcer = PolicyEnforcer(policy_store=ps)
    from loom.auth.appliers import ApiKeyStringApplier
    resolver = CredentialResolver(
        store=ss,
        appliers={("api_key", "llm_api_key"): ApiKeyStringApplier()},
        enforcer=enforcer,
    )
    result = await resolver.resolve_for("svc", "llm_api_key")
    assert result == "my-key"


async def test_enforcer_denies_raises_credential_denied(tmp_path: Path) -> None:
    ss = _secret_store(tmp_path)
    ps = _policy_store(tmp_path)
    # ONE_SHOT with 0 uses → always denied
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.ONE_SHOT, uses_remaining=0))
    secret: ApiKeySecret = {"type": "api_key", "value": "my-key"}
    await ss.put("svc", secret)

    # Spy on store.get to assert it is NOT called
    original_get = ss.get
    get_calls: list[str] = []

    async def spy_get(scope: str) -> Any:
        get_calls.append(scope)
        return await original_get(scope)

    ss.get = spy_get  # type: ignore[method-assign]

    enforcer = PolicyEnforcer(policy_store=ps)
    from loom.auth.appliers import ApiKeyStringApplier
    resolver = CredentialResolver(
        store=ss,
        appliers={("api_key", "llm_api_key"): ApiKeyStringApplier()},
        enforcer=enforcer,
    )

    with pytest.raises(CredentialDenied):
        await resolver.resolve_for("svc", "llm_api_key")

    assert get_calls == [], "store.get must not be called when enforcer denies"


async def test_no_enforcer_phase_a_compat(tmp_path: Path) -> None:
    """Resolver with no enforcer must work exactly as Phase A."""
    ss = _secret_store(tmp_path)
    secret: ApiKeySecret = {"type": "api_key", "value": "phase-a-key"}
    await ss.put("svc", secret)

    from loom.auth.appliers import ApiKeyStringApplier
    resolver = CredentialResolver(
        store=ss,
        appliers={("api_key", "llm_api_key"): ApiKeyStringApplier()},
        # no enforcer
    )
    result = await resolver.resolve_for("svc", "llm_api_key")
    assert result == "phase-a-key"


async def test_gate_decision_in_applier_context(tmp_path: Path) -> None:
    """_gate_decision must appear in the context dict forwarded to the applier."""
    ss = _secret_store(tmp_path)
    ps = _policy_store(tmp_path)
    await ps.put(CredentialPolicy(scope="svc", mode=PolicyMode.AUTONOMOUS))
    secret: ApiKeySecret = {"type": "api_key", "value": "v"}
    await ss.put("svc", secret)

    enforcer = PolicyEnforcer(policy_store=ps)
    applier = CapturingApplier()
    resolver = CredentialResolver(
        store=ss,
        appliers={("api_key", "capture"): applier},  # type: ignore[dict-item]
        enforcer=enforcer,
    )
    await resolver.resolve_for("svc", "capture")
    assert "_gate_decision" in CapturingApplier.last_context
    gate_decision = CapturingApplier.last_context["_gate_decision"]
    assert isinstance(gate_decision, GateDecision)
    assert gate_decision.allowed is True
