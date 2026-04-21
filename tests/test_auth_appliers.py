"""Tests for loom.auth.appliers (RFC 0002 Phase A).

Covers: each applier produces correct output for a well-formed secret;
OAuth2CC caches token across calls and re-fetches on version bump;
expired bearer token raises SecretExpiredError.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from loom.auth.appliers import (
    ApiKeyHeaderApplier,
    ApiKeyStringApplier,
    BasicHttpApplier,
    BearerHttpApplier,
    OAuth2CCHttpApplier,
)
from loom.auth.errors import SecretExpiredError
from loom.store.secrets import (
    ApiKeySecret,
    BasicAuthSecret,
    BearerTokenSecret,
    OAuth2ClientCredentialsSecret,
)

# ---------------------------------------------------------------------------
# BasicHttpApplier
# ---------------------------------------------------------------------------


async def test_basic_http_applier_produces_correct_header() -> None:
    applier = BasicHttpApplier()
    secret: BasicAuthSecret = {"type": "basic_auth", "username": "alice", "password": "p4ss"}
    result = await applier.apply(secret, {})
    import base64

    expected = base64.b64encode(b"alice:p4ss").decode()
    assert result == {"Authorization": f"Basic {expected}"}


async def test_basic_http_applier_secret_type() -> None:
    assert BasicHttpApplier.secret_type == "basic_auth"


# ---------------------------------------------------------------------------
# BearerHttpApplier
# ---------------------------------------------------------------------------


async def test_bearer_http_applier_produces_correct_header() -> None:
    applier = BearerHttpApplier()
    secret: BearerTokenSecret = {
        "type": "bearer_token",
        "token": "my-token-abc",
        "expires_at": None,
    }
    result = await applier.apply(secret, {})
    assert result == {"Authorization": "Bearer my-token-abc"}


async def test_bearer_http_applier_no_expiry() -> None:
    """Token with no expiry should pass through."""
    applier = BearerHttpApplier()
    secret: BearerTokenSecret = {"type": "bearer_token", "token": "tok", "expires_at": None}
    result = await applier.apply(secret, {})
    assert "Authorization" in result


async def test_bearer_http_applier_future_expiry() -> None:
    """Token expiring in the future should pass through."""
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    applier = BearerHttpApplier()
    secret: BearerTokenSecret = {"type": "bearer_token", "token": "tok", "expires_at": future}
    result = await applier.apply(secret, {})
    assert "Authorization" in result


async def test_bearer_http_applier_expired_raises() -> None:
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    applier = BearerHttpApplier()
    secret: BearerTokenSecret = {"type": "bearer_token", "token": "tok", "expires_at": past}
    with pytest.raises(SecretExpiredError):
        await applier.apply(secret, {})


async def test_bearer_http_applier_secret_type() -> None:
    assert BearerHttpApplier.secret_type == "bearer_token"


# ---------------------------------------------------------------------------
# ApiKeyHeaderApplier
# ---------------------------------------------------------------------------


async def test_api_key_header_applier_default_header() -> None:
    applier = ApiKeyHeaderApplier()
    secret: ApiKeySecret = {"type": "api_key", "value": "key-123"}
    result = await applier.apply(secret, {})
    assert result == {"X-API-Key": "key-123"}


async def test_api_key_header_applier_custom_header() -> None:
    applier = ApiKeyHeaderApplier(header_name="Authorization")
    secret: ApiKeySecret = {"type": "api_key", "value": "key-abc"}
    result = await applier.apply(secret, {})
    assert result == {"Authorization": "key-abc"}


async def test_api_key_header_applier_secret_type() -> None:
    assert ApiKeyHeaderApplier.secret_type == "api_key"


# ---------------------------------------------------------------------------
# ApiKeyStringApplier
# ---------------------------------------------------------------------------


async def test_api_key_string_applier_returns_raw_value() -> None:
    applier = ApiKeyStringApplier()
    secret: ApiKeySecret = {"type": "api_key", "value": "sk-raw-value"}
    result = await applier.apply(secret, {})
    assert result == "sk-raw-value"


async def test_api_key_string_applier_secret_type() -> None:
    assert ApiKeyStringApplier.secret_type == "api_key"


# ---------------------------------------------------------------------------
# OAuth2CCHttpApplier — uses httpx.MockTransport
# ---------------------------------------------------------------------------


def _make_token_handler(token: str = "access-token-1", expires_in: int = 3600):
    """Returns a mock handler for an OAuth2 token endpoint."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": expires_in,
            },
        )

    return handler


@pytest.fixture
def patch_httpx_client(monkeypatch: pytest.MonkeyPatch):
    """Returns a helper that installs a MockTransport into httpx.AsyncClient."""

    def _install(handler):
        original = httpx.AsyncClient

        def _factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _factory)

    return _install


async def test_oauth2_cc_produces_bearer_header(patch_httpx_client) -> None:
    patch_httpx_client(_make_token_handler("tok-xyz"))
    applier = OAuth2CCHttpApplier()
    secret: OAuth2ClientCredentialsSecret = {
        "type": "oauth2_client_credentials",
        "client_id": "cid",
        "client_secret": "csec",
        "token_url": "https://auth.example.com/token",
        "scopes": None,
    }
    result = await applier.apply(secret, {"scope": "my-scope", "version": 1})
    assert result == {"Authorization": "Bearer tok-xyz"}


async def test_oauth2_cc_caches_token_across_calls(patch_httpx_client) -> None:
    """Token endpoint should be called only once for repeated apply() calls."""
    call_count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200, json={"access_token": "cached-tok", "token_type": "Bearer", "expires_in": 3600}
        )

    patch_httpx_client(counting_handler)
    applier = OAuth2CCHttpApplier()
    secret: OAuth2ClientCredentialsSecret = {
        "type": "oauth2_client_credentials",
        "client_id": "cid",
        "client_secret": "csec",
        "token_url": "https://auth.example.com/token",
        "scopes": ["read"],
    }
    ctx = {"scope": "my-scope", "version": 1}
    await applier.apply(secret, ctx)
    await applier.apply(secret, ctx)
    await applier.apply(secret, ctx)
    assert call_count == 1  # only one actual token request


async def test_oauth2_cc_refetches_on_version_bump(patch_httpx_client) -> None:
    """Bumping version in context should bypass the cache."""
    call_count = 0

    def counting_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json={
                "access_token": f"tok-v{call_count}",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    patch_httpx_client(counting_handler)
    applier = OAuth2CCHttpApplier()
    secret: OAuth2ClientCredentialsSecret = {
        "type": "oauth2_client_credentials",
        "client_id": "cid",
        "client_secret": "csec",
        "token_url": "https://auth.example.com/token",
        "scopes": None,
    }
    r1 = await applier.apply(secret, {"scope": "s", "version": 1})
    r2 = await applier.apply(secret, {"scope": "s", "version": 2})  # bumped version
    assert call_count == 2
    assert r1 != r2


async def test_oauth2_cc_secret_type() -> None:
    assert OAuth2CCHttpApplier.secret_type == "oauth2_client_credentials"
