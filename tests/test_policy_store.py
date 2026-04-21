"""Tests for loom.auth.policy_store.PolicyStore (RFC 0002 Phase B).

Covers: put/get/delete/list roundtrip; prefix filtering; decrement_uses;
file permissions; idempotent delete; missing-scope handling.
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path

import pytest

from loom.auth.policies import CredentialPolicy, PolicyMode
from loom.auth.policy_store import PolicyStore


@pytest.fixture
def store(tmp_path: Path) -> PolicyStore:
    return PolicyStore(tmp_path / "policies.json")


def _policy(scope: str, mode: PolicyMode = PolicyMode.AUTONOMOUS) -> CredentialPolicy:
    return CredentialPolicy(scope=scope, mode=mode)


# ---------------------------------------------------------------------------
# Basic CRUD
# ---------------------------------------------------------------------------


async def test_put_and_get_roundtrip(store: PolicyStore) -> None:
    p = CredentialPolicy(scope="prod-oic", mode=PolicyMode.AUTONOMOUS)
    await store.put(p)
    result = await store.get("prod-oic")
    assert result == p


async def test_get_unknown_scope_returns_none(store: PolicyStore) -> None:
    result = await store.get("no-such-scope")
    assert result is None


async def test_delete_existing_returns_true(store: PolicyStore) -> None:
    await store.put(_policy("to-delete"))
    assert await store.delete("to-delete") is True
    assert await store.get("to-delete") is None


async def test_delete_idempotent_returns_false(store: PolicyStore) -> None:
    # Deleting a scope that never existed should return False, not raise
    assert await store.delete("ghost") is False
    # Deleting a second time after first delete also returns False
    await store.put(_policy("double-delete"))
    await store.delete("double-delete")
    assert await store.delete("double-delete") is False


async def test_list_all(store: PolicyStore) -> None:
    scopes = ["alpha", "beta", "gamma"]
    for s in scopes:
        await store.put(_policy(s))
    results = await store.list()
    result_scopes = {p.scope for p in results}
    assert result_scopes == set(scopes)


async def test_list_prefix_filter(store: PolicyStore) -> None:
    await store.put(_policy("prod/oic"))
    await store.put(_policy("prod/nexus"))
    await store.put(_policy("staging/oic"))

    prod = await store.list(scope_prefix="prod/")
    assert {p.scope for p in prod} == {"prod/oic", "prod/nexus"}

    staging = await store.list(scope_prefix="staging/")
    assert {p.scope for p in staging} == {"staging/oic"}


# ---------------------------------------------------------------------------
# decrement_uses
# ---------------------------------------------------------------------------


async def test_decrement_uses_existing(store: PolicyStore) -> None:
    p = CredentialPolicy(scope="one-shot", mode=PolicyMode.ONE_SHOT, uses_remaining=3)
    await store.put(p)
    new_val = await store.decrement_uses("one-shot")
    assert new_val == 2
    # Verify persistence
    updated = await store.get("one-shot")
    assert updated is not None
    assert updated.uses_remaining == 2


async def test_decrement_uses_absent_scope_returns_minus_one(store: PolicyStore) -> None:
    result = await store.decrement_uses("no-policy-here")
    assert result == -1


async def test_decrement_uses_unlimited_policy_returns_minus_one(store: PolicyStore) -> None:
    """A policy without uses_remaining (e.g. AUTONOMOUS) should return -1."""
    p = CredentialPolicy(scope="auto", mode=PolicyMode.AUTONOMOUS, uses_remaining=None)
    await store.put(p)
    result = await store.decrement_uses("auto")
    assert result == -1


# ---------------------------------------------------------------------------
# File mode
# ---------------------------------------------------------------------------


async def test_file_mode_is_0600(store: PolicyStore) -> None:
    await store.put(_policy("mode-test"))
    path = store._path
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


# ---------------------------------------------------------------------------
# Datetime roundtrip (TIME_BOXED)
# ---------------------------------------------------------------------------


async def test_time_boxed_datetime_roundtrip(store: PolicyStore) -> None:
    start = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    end = datetime(2024, 12, 31, 23, 59, 59, tzinfo=UTC)
    p = CredentialPolicy(
        scope="window-cred",
        mode=PolicyMode.TIME_BOXED,
        window_start=start,
        window_end=end,
    )
    await store.put(p)
    result = await store.get("window-cred")
    assert result is not None
    assert result.window_start == start
    assert result.window_end == end
