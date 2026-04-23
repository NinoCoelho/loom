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
import uuid
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from loom.auth.errors import AuthApplierError, SecretExpiredError
from loom.store.secrets import (
    ApiKeySecret,
    AwsSigV4Secret,
    BasicAuthSecret,
    BearerTokenSecret,
    JwtSigningKeySecret,
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


# ---------------------------------------------------------------------------
# SigV4Applier (RFC 0002 Phase C — AWS)
# ---------------------------------------------------------------------------


class SigV4Applier:
    """Signs HTTP requests with AWS Signature Version 4.

    Uses ``botocore`` (from ``loom[aws]``) which implements the SigV4 algorithm.
    Imported lazily so the base loom install does not require botocore.

    Context keys (all optional — override defaults from the secret):
    - ``method`` (str): HTTP method, default ``"GET"``.
    - ``url`` (str): Full request URL (required for signing).
    - ``headers`` (dict[str, str]): Existing request headers (enriched in place).
    - ``body`` (bytes | str | None): Request body bytes.
    - ``service`` (str): AWS service name, e.g. ``"execute-api"``, ``"s3"``.
    - ``region`` (str): AWS region override (falls back to secret's ``region`` or ``"us-east-1"``).

    Output: ``dict[str, str]`` — the *full* headers dict including SigV4
    ``Authorization``, ``x-amz-date``, ``x-amz-security-token`` (if applicable).

    Security: ``secret_access_key`` is never included in the output headers or
    log messages.
    """

    secret_type: str = "aws_sigv4"

    async def apply(self, secret: AwsSigV4Secret, context: dict) -> dict[str, str]:  # type: ignore[override]
        try:
            import botocore.auth
            import botocore.awsrequest
            import botocore.credentials
        except ImportError as exc:
            raise ImportError(
                "botocore is required for SigV4 request signing. "
                'Install it with: pip install "loom[aws]"'
            ) from exc

        method: str = context.get("method", "GET").upper()
        url: str = context.get("url", "")
        body = context.get("body") or b""
        if isinstance(body, str):
            body = body.encode("utf-8")

        service: str = context.get("service", "execute-api")
        region: str = context.get("region") or secret.get("region") or "us-east-1"

        # Build botocore credentials object — never exposes secret_access_key in headers
        creds = botocore.credentials.Credentials(
            access_key=secret["access_key_id"],
            secret_key=secret["secret_access_key"],
            token=secret.get("session_token"),
        )

        # Build AWSRequest
        incoming_headers: dict[str, str] = dict(context.get("headers") or {})
        request = botocore.awsrequest.AWSRequest(
            method=method,
            url=url,
            data=body,
            headers=incoming_headers,
        )

        # Sign
        signer = botocore.auth.SigV4Auth(creds, service, region)
        signer.add_auth(request)

        # Return the final headers dict (includes Authorization, x-amz-date, etc.)
        return dict(request.headers)


# ---------------------------------------------------------------------------
# JwtBearerApplier (RFC 0002 Phase C — client-assertion JWT)
# ---------------------------------------------------------------------------


class JwtBearerApplier:
    """Produces a signed JWT and applies it as ``Authorization: Bearer <jwt>``.

    Uses PyJWT with the ``cryptography`` backend (already a core loom dep).

    Claims built at ``apply()`` time:
    - ``iss`` — ``secret["issuer"]``
    - ``sub`` — ``secret["subject"]`` (omitted if ``None``)
    - ``aud`` — ``secret["audience"]``
    - ``iat`` — current UTC unix timestamp
    - ``exp`` — ``iat + ttl_seconds`` (default 300 s)
    - ``jti`` — a fresh UUID per token

    Cache: tokens are cached in-process keyed by ``(scope, version)``.  The
    cache entry is valid as long as the token has not expired (with a 30-second
    buffer).  Version comes from ``context["version"]``; bumping it (via
    ``SecretStore.rotate``) forces a new JWT.

    Algorithm support:
    - ``RS256`` / ``ES256`` — ``secret["private_key_pem"]`` is a PEM private key.
    - ``HS256`` — ``secret["private_key_pem"]`` is treated as the HMAC shared secret.

    Context keys:
    - ``scope`` (str): loom scope name; used as cache key dimension.
    - ``version`` (int): secret version; cache-busting on rotate.

    Output: ``{"Authorization": "Bearer <signed-jwt>"}``
    """

    secret_type: str = "jwt_signing_key"

    def __init__(self) -> None:
        # Cache: (scope, version) -> (token_str, exp_epoch)
        self._cache: dict[tuple[str, int], tuple[str, float]] = {}

    def _cache_key(self, context: dict) -> tuple[str, int]:
        return (context.get("scope", ""), context.get("version", 0))

    async def apply(self, secret: JwtSigningKeySecret, context: dict) -> dict[str, str]:  # type: ignore[override]
        try:
            import jwt as pyjwt
        except ImportError as exc:
            raise ImportError(
                'PyJWT is required for JwtBearerApplier. Install it with: pip install "loom[jwt]"'
            ) from exc

        key = self._cache_key(context)
        cached = self._cache.get(key)
        now_ts = datetime.now(UTC).timestamp()
        if cached is not None:
            token_str, exp_epoch = cached
            if now_ts < exp_epoch - 30:
                return {"Authorization": f"Bearer {token_str}"}

        # Build claims
        ttl = int(secret.get("ttl_seconds") or 300)
        iat = int(now_ts)
        exp = iat + ttl

        claims: dict[str, Any] = {
            "iss": secret["issuer"],
            "aud": secret["audience"],
            "iat": iat,
            "exp": exp,
            "jti": str(uuid.uuid4()),
        }
        sub = secret.get("subject")
        if sub is not None:
            claims["sub"] = sub

        algorithm: str = secret.get("algorithm", "RS256")
        private_key_pem: str = secret["private_key_pem"]

        # For HS256 the "PEM" field holds the raw shared secret bytes/str
        if algorithm == "HS256":
            signing_key: Any = (
                private_key_pem.encode("utf-8")
                if isinstance(private_key_pem, str)
                else private_key_pem
            )
        else:
            signing_key = private_key_pem

        headers: dict[str, Any] = {}
        kid = secret.get("key_id")
        if kid:
            headers["kid"] = kid

        token_str = pyjwt.encode(
            claims,
            signing_key,
            algorithm=algorithm,
            headers=headers or None,
        )

        self._cache[key] = (token_str, float(exp))
        return {"Authorization": f"Bearer {token_str}"}
