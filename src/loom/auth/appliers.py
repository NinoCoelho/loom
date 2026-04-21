"""loom.auth.appliers — transport-agnostic credential appliers.

An *applier* turns a typed ``Secret`` into ready-to-use material for a
specific transport (HTTP headers, raw string, etc.). Each applier handles
exactly one (secret_type, transport) pair.

Applier protocol::

    class Applier(Protocol):
        secret_type: str
        async def apply(self, secret: Secret, context: dict) -> Any: ...

The ``context`` dict may carry transport-specific hints.  Known keys:

- ``base_url`` (str) — target base URL (informational; not used by most appliers).
- ``version`` (int) — secret version from ``SecretStore``; used by
  ``OAuth2CCHttpApplier`` to detect rotations and invalidate its token cache.

See docs/rfcs/0002-credentials-and-appliers.md for design rationale.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from loom.auth.errors import AuthApplierError, SecretExpiredError
from loom.store.secrets import (
    ApiKeySecret,
    BasicAuthSecret,
    BearerTokenSecret,
    OAuth2ClientCredentialsSecret,
    PasswordSecret,
    Secret,
    SshPrivateKeySecret,
)

# ---------------------------------------------------------------------------
# Applier protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Applier(Protocol):
    """Transport-agnostic credential applier protocol.

    Each concrete applier handles one secret type and one transport.
    """

    secret_type: str

    def apply(self, secret: Secret, context: dict) -> Awaitable[Any]:
        """Turn *secret* into transport-ready material.

        Args:
            secret: The typed secret from ``SecretStore``.
            context: Transport-specific hints (``base_url``, ``version``, …).

        Returns:
            Transport-ready output (e.g. ``dict[str, str]`` headers or ``str``).
        """
        ...


# ---------------------------------------------------------------------------
# HTTP appliers
# ---------------------------------------------------------------------------


class BasicHttpApplier:
    """Applies a ``basic_auth`` secret as an HTTP ``Authorization`` header (RFC 7617).

    Context keys: none required.

    Output: ``{"Authorization": "Basic <base64(user:pass)>"}``
    """

    secret_type: str = "basic_auth"

    async def apply(self, secret: BasicAuthSecret, context: dict) -> dict[str, str]:  # type: ignore[override]
        credentials = f"{secret['username']}:{secret['password']}"
        encoded = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}


class BearerHttpApplier:
    """Applies a ``bearer_token`` secret as an HTTP ``Authorization: Bearer`` header.

    If the token carries an ``expires_at`` timestamp (ISO 8601) and it is in
    the past, raises ``SecretExpiredError`` before returning.

    Context keys: none required.

    Output: ``{"Authorization": "Bearer <token>"}``
    """

    secret_type: str = "bearer_token"

    async def apply(self, secret: BearerTokenSecret, context: dict) -> dict[str, str]:  # type: ignore[override]
        expires_at = secret.get("expires_at")
        if expires_at:
            expiry = datetime.fromisoformat(expires_at)
            # Make timezone-aware if naive (assume UTC).
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if datetime.now(UTC) >= expiry:
                raise SecretExpiredError()
        return {"Authorization": f"Bearer {secret['token']}"}


class OAuth2CCHttpApplier:
    """Applies an ``oauth2_client_credentials`` secret as an HTTP ``Authorization: Bearer`` header.

    Performs the OAuth2 client-credentials token exchange via httpx and caches
    the resulting access token in-process.  Cache key:
    ``(scope, version, token_url, scopes_tuple)``.

    Cache invalidation: the caller should pass ``version`` in *context* (sourced
    from ``SecretMetadata.version``).  When the version bumps (e.g. after
    ``SecretStore.rotate``), the cached token is discarded and a new exchange is
    performed.

    Context keys:
    - ``version`` (int, optional) — secret version; bumping this invalidates cache.
    - ``scope`` (str, optional) — the loom scope name (used as part of cache key).

    Output: ``{"Authorization": "Bearer <access_token>"}``
    """

    secret_type: str = "oauth2_client_credentials"

    def __init__(self) -> None:
        # Cache: (scope_name, version, token_url, scopes_tuple) -> (token, expires_at_epoch)
        self._cache: dict[tuple, tuple[str, float]] = {}

    def _cache_key(
        self,
        context: dict,
        secret: OAuth2ClientCredentialsSecret,
    ) -> tuple:
        scope_name = context.get("scope", "")
        version = context.get("version", 0)
        token_url = secret["token_url"]
        scopes_tuple = tuple(sorted(secret["scopes"] or []))
        return (scope_name, version, token_url, scopes_tuple)

    async def apply(  # type: ignore[override]
        self,
        secret: OAuth2ClientCredentialsSecret,
        context: dict,
    ) -> dict[str, str]:
        key = self._cache_key(context, secret)
        cached = self._cache.get(key)
        if cached is not None:
            token, expires_at = cached
            # Leave a 30-second buffer before expiry.
            if time.monotonic() < expires_at - 30:
                return {"Authorization": f"Bearer {token}"}

        # Exchange credentials for a token.
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
    """Applies an ``api_key`` secret as a custom HTTP request header.

    Args:
        header_name: The header to inject (default ``"X-API-Key"``).

    Context keys: none required.

    Output: ``{header_name: key_value}``
    """

    secret_type: str = "api_key"

    def __init__(self, header_name: str = "X-API-Key") -> None:
        self.header_name = header_name

    async def apply(self, secret: ApiKeySecret, context: dict) -> dict[str, str]:  # type: ignore[override]
        return {self.header_name: secret["value"]}


class ApiKeyStringApplier:
    """Applies an ``api_key`` secret as a plain string (for LLM provider init).

    Context keys: none required.

    Output: the raw key string.
    """

    secret_type: str = "api_key"

    async def apply(self, secret: ApiKeySecret, context: dict) -> str:  # type: ignore[override]
        return secret["value"]


# ---------------------------------------------------------------------------
# SSH appliers (RFC 0003)
# ---------------------------------------------------------------------------



class SshConnectArgs(dict):
    """Normalized connection args fed to ``asyncssh.connect()``.

    Keys:
    - ``host`` (str): target hostname (from SecretMetadata).
    - ``port`` (int): SSH port (default 22).
    - ``username`` (str): remote username.
    - ``password`` (str, optional): plaintext password (PasswordSecret path).
    - ``client_keys`` (list[asyncssh.SSHKey], optional): loaded private keys
      (SshPrivateKeySecret path).
    - ``known_hosts`` (str | None | False): path to known_hosts file, None for default,
      or False to disable checking (dev only — emits a warning).
    """


class SshPasswordApplier:
    """Applies a ``password`` secret as SSH password authentication.

    Reads ``hostname``, ``port``, and ``username`` from ``SecretMetadata``
    (via the ``context["metadata"]`` key populated by the tool).

    Context keys:
    - ``metadata`` (dict): SecretMetadata.metadata for the scope (hostname, port, username).

    Output: ``SshConnectArgs`` with host/port/username/password.
    """

    secret_type: str = "password"

    async def apply(self, secret: PasswordSecret, context: dict) -> SshConnectArgs:  # type: ignore[override]
        meta = context.get("metadata") or {}
        host = meta.get("hostname") or meta.get("host") or ""
        port = int(meta.get("port", 22))
        username = meta.get("username") or meta.get("user") or ""
        args = SshConnectArgs(
            host=host,
            port=port,
            username=username,
            password=secret["value"],
        )
        return args


class SshKeyApplier:
    """Applies an ``ssh_private_key`` secret as SSH public-key authentication.

    Reads ``hostname``, ``port``, and ``username`` from ``SecretMetadata``
    (via the ``context["metadata"]`` key populated by the tool).

    Context keys:
    - ``metadata`` (dict): SecretMetadata.metadata for the scope (hostname, port, username).

    Output: ``SshConnectArgs`` with host/port/username/client_keys (asyncssh key object).

    Note: asyncssh is imported lazily; if ``loom[ssh]`` is not installed an
    ``ImportError`` is raised at apply-time with an actionable message.
    """

    secret_type: str = "ssh_private_key"

    async def apply(self, secret: SshPrivateKeySecret, context: dict) -> SshConnectArgs:  # type: ignore[override]
        try:
            import asyncssh  # lazy — only required when loom[ssh] is installed
        except ImportError as exc:
            raise ImportError(
                "asyncssh is required for SSH key auth. Install it with: pip install 'loom[ssh]'"
            ) from exc

        passphrase = secret.get("passphrase")
        key = asyncssh.import_private_key(
            secret["key_pem"],
            passphrase=passphrase.encode() if passphrase else None,
        )
        meta = context.get("metadata") or {}
        host = meta.get("hostname") or meta.get("host") or ""
        port = int(meta.get("port", 22))
        username = meta.get("username") or meta.get("user") or ""
        args = SshConnectArgs(
            host=host,
            port=port,
            username=username,
            client_keys=[key],
        )
        return args
