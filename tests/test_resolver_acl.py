"""Tests for the ACL hook on loom.auth.resolver.CredentialResolver (RFC 0002 Phase C).

Covers:
- ACL allows access when callable returns True.
- ACL denies access with ScopeAccessDenied when callable returns False.
- Missing principal raises MissingPrincipalError when ACL is installed.
- No ACL (None) allows all access regardless of context (backward compat).
- ACL callable receives correct (principal, scope, transport) arguments.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from loom.auth.appliers import ApiKeyStringApplier
from loom.auth.errors import ScopeAccessDenied
from loom.auth.resolver import CredentialResolver, MissingPrincipalError
from loom.store.secrets import ApiKeySecret, SecretStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(
        path=tmp_path / "secrets.db",
        key_path=tmp_path / "keys" / "secrets.key",
    )


@pytest.fixture
async def populated_store(store: SecretStore) -> SecretStore:
    secret: ApiKeySecret = {"type": "api_key", "value": "sk-test-123"}
    await store.put("my-scope", secret)
    return store


# ---------------------------------------------------------------------------
# No ACL (backward compat)
# ---------------------------------------------------------------------------


async def test_no_acl_allows_all(populated_store: SecretStore) -> None:
    resolver = CredentialResolver(
        store=populated_store,
        appliers={("api_key", "llm_api_key"): ApiKeyStringApplier()},
    )
    # No principal in context, no ACL — should succeed
    result = await resolver.resolve_for("my-scope", "llm_api_key")
    assert result == "sk-test-123"


# ---------------------------------------------------------------------------
# ACL allows
# ---------------------------------------------------------------------------


async def test_acl_allows_on_true(populated_store: SecretStore) -> None:
    def allow_all(principal: str, scope: str, transport: str) -> bool:
        return True

    resolver = CredentialResolver(
        store=populated_store,
        appliers={("api_key", "llm_api_key"): ApiKeyStringApplier()},
        scope_acl=allow_all,
    )
    result = await resolver.resolve_for(
        "my-scope", "llm_api_key", context={"principal": "alice"}
    )
    assert result == "sk-test-123"


# ---------------------------------------------------------------------------
# ACL denies
# ---------------------------------------------------------------------------


async def test_acl_denies_on_false(populated_store: SecretStore) -> None:
    def deny_all(principal: str, scope: str, transport: str) -> bool:
        return False

    resolver = CredentialResolver(
        store=populated_store,
        appliers={("api_key", "llm_api_key"): ApiKeyStringApplier()},
        scope_acl=deny_all,
    )
    with pytest.raises(ScopeAccessDenied) as exc_info:
        await resolver.resolve_for(
            "my-scope", "llm_api_key", context={"principal": "bob"}
        )
    err = exc_info.value
    assert err.principal == "bob"
    assert err.scope == "my-scope"


async def test_acl_denies_specific_scope(populated_store: SecretStore) -> None:
    """ACL can allow some scopes and deny others."""
    await populated_store.put("admin-scope", {"type": "api_key", "value": "admin-key"})

    def restricted(principal: str, scope: str, transport: str) -> bool:
        if scope == "admin-scope":
            return principal == "superuser"
        return True

    resolver = CredentialResolver(
        store=populated_store,
        appliers={("api_key", "llm_api_key"): ApiKeyStringApplier()},
        scope_acl=restricted,
    )

    # Regular user can access my-scope
    result = await resolver.resolve_for(
        "my-scope", "llm_api_key", context={"principal": "alice"}
    )
    assert result == "sk-test-123"

    # Regular user cannot access admin-scope
    with pytest.raises(ScopeAccessDenied):
        await resolver.resolve_for(
            "admin-scope", "llm_api_key", context={"principal": "alice"}
        )

    # Superuser can access admin-scope
    result = await resolver.resolve_for(
        "admin-scope", "llm_api_key", context={"principal": "superuser"}
    )
    assert result == "admin-key"


# ---------------------------------------------------------------------------
# Missing principal
# ---------------------------------------------------------------------------


async def test_acl_missing_principal_raises(populated_store: SecretStore) -> None:
    """When ACL is set but context has no 'principal', raise MissingPrincipalError."""

    def allow(principal: str, scope: str, transport: str) -> bool:
        return True

    resolver = CredentialResolver(
        store=populated_store,
        appliers={("api_key", "llm_api_key"): ApiKeyStringApplier()},
        scope_acl=allow,
    )
    with pytest.raises(MissingPrincipalError) as exc_info:
        await resolver.resolve_for("my-scope", "llm_api_key")
    assert "principal" in str(exc_info.value).lower()
    assert exc_info.value.scope == "my-scope"


async def test_acl_missing_principal_with_empty_context(populated_store: SecretStore) -> None:
    def allow(principal: str, scope: str, transport: str) -> bool:
        return True

    resolver = CredentialResolver(
        store=populated_store,
        appliers={("api_key", "llm_api_key"): ApiKeyStringApplier()},
        scope_acl=allow,
    )
    with pytest.raises(MissingPrincipalError):
        await resolver.resolve_for("my-scope", "llm_api_key", context={})


# ---------------------------------------------------------------------------
# ACL receives correct arguments
# ---------------------------------------------------------------------------


async def test_acl_receives_correct_arguments(populated_store: SecretStore) -> None:
    """The ACL callable is called with (principal, scope, transport)."""
    calls: list[tuple[str, str, str]] = []

    def recording_acl(principal: str, scope: str, transport: str) -> bool:
        calls.append((principal, scope, transport))
        return True

    resolver = CredentialResolver(
        store=populated_store,
        appliers={("api_key", "llm_api_key"): ApiKeyStringApplier()},
        scope_acl=recording_acl,
    )
    await resolver.resolve_for(
        "my-scope", "llm_api_key", context={"principal": "charlie"}
    )
    assert len(calls) == 1
    assert calls[0] == ("charlie", "my-scope", "llm_api_key")


# ---------------------------------------------------------------------------
# ScopeAccessDenied exception attributes
# ---------------------------------------------------------------------------


def test_scope_access_denied_attributes() -> None:
    err = ScopeAccessDenied("alice", "secret-scope")
    assert err.principal == "alice"
    assert err.scope == "secret-scope"
    assert "alice" in str(err)
    assert "secret-scope" in str(err)


# ---------------------------------------------------------------------------
# MissingPrincipalError attributes
# ---------------------------------------------------------------------------


def test_missing_principal_error_attributes() -> None:
    err = MissingPrincipalError("some-scope")
    assert err.scope == "some-scope"
    assert "some-scope" in str(err)
