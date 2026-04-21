"""Tests for loom.auth.resolver.CredentialResolver (RFC 0002 Phase A).

Covers: dispatches to right applier; raises NoApplierError for unregistered
pairs; raises ScopeNotFoundError for missing scope; register() adds appliers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from loom.auth.appliers import ApiKeyStringApplier, BasicHttpApplier
from loom.auth.errors import NoApplierError, ScopeNotFoundError
from loom.auth.resolver import CredentialResolver
from loom.store.secrets import (
    ApiKeySecret,
    BasicAuthSecret,
    SecretStore,
)


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(
        path=tmp_path / "secrets.db",
        key_path=tmp_path / "keys" / "secrets.key",
    )


@pytest.fixture
def resolver(store: SecretStore) -> CredentialResolver:
    return CredentialResolver(
        store=store,
        appliers={
            ("basic_auth", "http"): BasicHttpApplier(),
            ("api_key", "llm_api_key"): ApiKeyStringApplier(),
        },
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_resolver_dispatches_basic_auth(
    resolver: CredentialResolver, store: SecretStore
) -> None:
    secret: BasicAuthSecret = {"type": "basic_auth", "username": "user", "password": "pw"}
    await store.put("my-service", secret)

    result = await resolver.resolve_for("my-service", "http")
    assert "Authorization" in result
    assert result["Authorization"].startswith("Basic ")


async def test_resolver_dispatches_api_key_string(
    resolver: CredentialResolver, store: SecretStore
) -> None:
    secret: ApiKeySecret = {"type": "api_key", "value": "sk-test-key"}
    await store.put("openai-prod", secret)

    result = await resolver.resolve_for("openai-prod", "llm_api_key")
    assert result == "sk-test-key"


async def test_resolver_passes_context_to_applier(
    resolver: CredentialResolver, store: SecretStore
) -> None:
    """Context should be forwarded; scope key is always injected."""
    received_context: dict = {}

    class CapturingApplier:
        secret_type = "api_key"

        async def apply(self, secret: Any, context: dict) -> str:
            received_context.update(context)
            return secret["value"]

    resolver.register(CapturingApplier(), transport="capture")  # type: ignore[arg-type]

    secret: ApiKeySecret = {"type": "api_key", "value": "val"}
    await store.put("cap-scope", secret)
    await resolver.resolve_for("cap-scope", "capture", context={"extra": "hint"})

    assert received_context["scope"] == "cap-scope"
    assert received_context["extra"] == "hint"


# ---------------------------------------------------------------------------
# register() at runtime
# ---------------------------------------------------------------------------


async def test_resolver_register_adds_applier(store: SecretStore) -> None:
    resolver = CredentialResolver(store=store)
    resolver.register(ApiKeyStringApplier(), transport="llm_api_key")

    secret: ApiKeySecret = {"type": "api_key", "value": "registered-key"}
    await store.put("registered-scope", secret)

    result = await resolver.resolve_for("registered-scope", "llm_api_key")
    assert result == "registered-key"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


async def test_resolver_raises_scope_not_found(resolver: CredentialResolver) -> None:
    with pytest.raises(ScopeNotFoundError) as exc_info:
        await resolver.resolve_for("does-not-exist", "http")
    assert "does-not-exist" in str(exc_info.value)


async def test_resolver_raises_no_applier_for_transport(
    resolver: CredentialResolver, store: SecretStore
) -> None:
    secret: ApiKeySecret = {"type": "api_key", "value": "k"}
    await store.put("scope-no-transport", secret)

    with pytest.raises(NoApplierError) as exc_info:
        await resolver.resolve_for("scope-no-transport", "ftp")  # no applier for ftp
    err = exc_info.value
    assert err.transport == "ftp"
    assert err.secret_type == "api_key"
    assert err.scope == "scope-no-transport"


async def test_resolver_raises_no_applier_for_secret_type(
    resolver: CredentialResolver, store: SecretStore
) -> None:
    from loom.store.secrets import PasswordSecret

    secret: PasswordSecret = {"type": "password", "value": "pw"}
    await store.put("scope-pw", secret)

    with pytest.raises(NoApplierError) as exc_info:
        # No applier registered for (password, http)
        await resolver.resolve_for("scope-pw", "http")
    err = exc_info.value
    assert err.secret_type == "password"
