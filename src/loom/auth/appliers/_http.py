"""HTTP credential appliers — Authorization headers and API key strings."""

from __future__ import annotations

import base64
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from loom.auth.errors import AuthApplierError, SecretExpiredError
from loom.store.secrets import (
    ApiKeySecret,
    BasicAuthSecret,
    BearerTokenSecret,
    OAuth2ClientCredentialsSecret,
)


class BasicHttpApplier:
    """Applies a ``basic_auth`` secret as an HTTP ``Authorization`` header (RFC 7617)."""

    secret_type: str = "basic_auth"

    async def apply(self, secret: BasicAuthSecret, context: dict) -> dict[str, str]:  # type: ignore[override]
        credentials = f"{secret['username']}:{secret['password']}"
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}


class BearerHttpApplier:
    """Applies a ``bearer_token`` secret as an HTTP ``Authorization: Bearer`` header."""

    secret_type: str = "bearer_token"

    async def apply(self, secret: BearerTokenSecret, context: dict) -> dict[str, str]:  # type: ignore[override]
        expires_at = secret.get("expires_at")
        if expires_at:
            expiry = datetime.fromisoformat(expires_at)
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if datetime.now(UTC) >= expiry:
                raise SecretExpiredError()
        return {"Authorization": f"Bearer {secret['token']}"}


class OAuth2CCHttpApplier:
    """Applies an ``oauth2_client_credentials`` secret via OAuth2 client-credentials exchange."""

    secret_type: str = "oauth2_client_credentials"

    def __init__(self) -> None:
        self._cache: dict[tuple, tuple[str, float]] = {}

    def _cache_key(self, context: dict, secret: OAuth2ClientCredentialsSecret) -> tuple:
        scope_name = context.get("scope", "")
        version = context.get("version", 0)
        token_url = secret["token_url"]
        scopes_tuple = tuple(sorted(secret["scopes"] or []))
        return (scope_name, version, token_url, scopes_tuple)

    async def apply(self, secret: OAuth2ClientCredentialsSecret, context: dict) -> dict[str, str]:  # type: ignore[override]
        key = self._cache_key(context, secret)
        cached = self._cache.get(key)
        if cached is not None:
            token, expires_at = cached
            if time.monotonic() < expires_at - 30:
                return {"Authorization": f"Bearer {token}"}

        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": secret["client_id"],
            "client_secret": secret["client_secret"],
        }
        if secret.get("scopes"):
            data["scope"] = " ".join(secret["scopes"])  # type: ignore[arg-type]

        async with httpx.AsyncClient() as client:
            resp = await client.post(secret["token_url"], data=data)

        if resp.status_code != 200:
            raise AuthApplierError(
                f"OAuth2 token exchange failed: HTTP {resp.status_code} — {resp.text}"
            )

        payload = resp.json()
        access_token: str = payload["access_token"]
        expires_in: int = int(payload.get("expires_in", 3600))
        self._cache[key] = (access_token, time.monotonic() + expires_in)
        return {"Authorization": f"Bearer {access_token}"}


class ApiKeyHeaderApplier:
    """Applies an ``api_key`` secret as a custom HTTP request header."""

    secret_type: str = "api_key"

    def __init__(self, header_name: str = "X-API-Key") -> None:
        self.header_name = header_name

    async def apply(self, secret: ApiKeySecret, context: dict) -> dict[str, str]:  # type: ignore[override]
        return {self.header_name: secret["value"]}


class ApiKeyStringApplier:
    """Applies an ``api_key`` secret as a plain string (for LLM provider init)."""

    secret_type: str = "api_key"

    async def apply(self, secret: ApiKeySecret, context: dict) -> str:  # type: ignore[override]
        return secret["value"]
