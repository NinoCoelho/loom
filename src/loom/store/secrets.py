"""loom.store.secrets — secret storage for the Loom framework.

Provides two classes:

- ``SecretsStore`` — the original plaintext-JSON store (kept for backward compatibility).
  **Deprecated**: prefer ``SecretStore`` for new code.
- ``SecretStore`` — typed, Fernet-encrypted at-rest storage. Implements RFC 0002 Phase A.

See docs/rfcs/0002-credentials-and-appliers.md for design rationale.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TypedDict

from loom.store.atomic import atomic_write

# ---------------------------------------------------------------------------
# Typed secret definitions (RFC 0002 Layer 1)
# ---------------------------------------------------------------------------


class PasswordSecret(TypedDict):
    type: Literal["password"]
    value: str


class ApiKeySecret(TypedDict):
    type: Literal["api_key"]
    value: str


class BasicAuthSecret(TypedDict):
    type: Literal["basic_auth"]
    username: str
    password: str


class BearerTokenSecret(TypedDict):
    type: Literal["bearer_token"]
    token: str
    expires_at: str | None  # ISO 8601


class OAuth2ClientCredentialsSecret(TypedDict):
    type: Literal["oauth2_client_credentials"]
    client_id: str
    client_secret: str
    token_url: str
    scopes: list[str] | None


class SshPrivateKeySecret(TypedDict):
    type: Literal["ssh_private_key"]
    key_pem: str  # PEM-encoded
    passphrase: str | None


class AwsSigV4Secret(TypedDict):
    """AWS credentials for SigV4 request signing (RFC 0002 Phase C)."""

    type: Literal["aws_sigv4"]
    access_key_id: str
    secret_access_key: str
    session_token: str | None  # optional STS session token
    region: str | None  # default region; may be overridden per-request via context


class JwtSigningKeySecret(TypedDict):
    """PEM private key (or HS256 shared secret) for JWT client-assertion (RFC 0002 Phase C)."""

    type: Literal["jwt_signing_key"]
    private_key_pem: str  # PEM private key for RS256/ES256; raw bytes/str for HS256
    algorithm: Literal["RS256", "ES256", "HS256"]
    key_id: str | None  # optional kid header claim
    issuer: str  # iss claim
    audience: str  # aud claim
    subject: str | None  # sub claim (optional)
    ttl_seconds: int  # token lifetime; default 300 when not provided


Secret = (
    PasswordSecret
    | ApiKeySecret
    | BasicAuthSecret
    | BearerTokenSecret
    | OAuth2ClientCredentialsSecret
    | SshPrivateKeySecret
    | AwsSigV4Secret
    | JwtSigningKeySecret
)


class SecretMetadata(TypedDict):
    scope: str
    secret_type: str
    created_at: str  # ISO 8601
    version: int
    metadata: dict


# ---------------------------------------------------------------------------
# SecretStore — Fernet-encrypted (RFC 0002 Phase A)
# ---------------------------------------------------------------------------


def _load_or_create_fernet_key(key_path: Path) -> bytes:
    """Load Fernet key from *key_path*, generating and persisting one if absent."""
    from cryptography.fernet import Fernet

    if key_path.exists():
        return key_path.read_bytes().strip()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Write atomically then lock down.
    tmp = key_path.with_suffix(".tmp")
    tmp.write_bytes(key)
    os.chmod(tmp, 0o600)
    tmp.rename(key_path)
    return key


def _fernet_from_env_or_file(key_path: Path):  # type: ignore[return]
    """Return a ``Fernet`` instance using ``LOOM_SECRET_KEY`` env var or *key_path*."""
    from cryptography.fernet import Fernet

    raw = os.environ.get("LOOM_SECRET_KEY")
    if raw:
        return Fernet(raw.encode() if isinstance(raw, str) else raw)
    return Fernet(_load_or_create_fernet_key(key_path))


class SecretStore:
    """Fernet-encrypted, scope-keyed secret store (RFC 0002 Phase A).

    Secrets are stored as a JSON document at *path* (conventionally
    ``$LOOM_HOME/secrets.db``), encrypted with a Fernet key loaded from
    ``$LOOM_HOME/keys/secrets.key`` (or ``LOOM_SECRET_KEY`` env override).

    The key file and storage file are created automatically on first use with
    mode 0600.

    No in-process cache — every ``get()`` call reads and decrypts from disk.
    This keeps the implementation simple; cache invalidation is a follow-up.
    """

    def __init__(self, path: Path, *, key_path: Path | None = None) -> None:
        self._path = path
        # Default key location mirrors LOOM_HOME convention; callers may override.
        self._key_path = key_path or (path.parent / "keys" / "secrets.key")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fernet(self):  # type: ignore[return]
        return _fernet_from_env_or_file(self._key_path)

    def _read_raw(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            encrypted = self._path.read_bytes()
            decrypted = self._fernet().decrypt(encrypted)
            return json.loads(decrypted.decode("utf-8"))
        except Exception:
            return {}

    def _write_raw(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        plaintext = json.dumps(data, indent=2).encode("utf-8")
        encrypted = self._fernet().encrypt(plaintext)
        # atomic_write works with str; use manual atomic bytes write here.
        tmp = self._path.with_suffix(".tmp")
        tmp.write_bytes(encrypted)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)
        # Ensure storage file has mode 0600 on first creation.
        current = stat.S_IMODE(os.stat(self._path).st_mode)
        if current != 0o600:
            os.chmod(self._path, 0o600)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def put(self, scope: str, secret: Secret, *, metadata: dict | None = None) -> str:
        """Store *secret* under *scope*.

        Returns an internal secret id (UUID). If *scope* already exists the
        entry is replaced (use ``rotate`` to track version increments).
        """
        data = self._read_raw()
        secret_id = str(uuid.uuid4())
        data[scope] = {
            "id": secret_id,
            "secret": secret,
            "created_at": datetime.now(UTC).isoformat(),
            "version": 1,
            "metadata": metadata or {},
        }
        self._write_raw(data)
        return secret_id

    async def get(self, scope: str) -> Secret | None:
        """Return the ``Secret`` stored under *scope*, or ``None`` if absent."""
        data = self._read_raw()
        entry = data.get(scope)
        if entry is None:
            return None
        return entry["secret"]  # type: ignore[return-value]

    async def get_metadata(self, scope: str) -> dict | None:
        """Return the free-form metadata dict for *scope*, or ``None`` if absent.

        This is the ``metadata`` kwarg passed to ``put()``.  Useful for
        transports (e.g. SSH) that need extra connection parameters stored
        alongside the secret (hostname, port, username).
        """
        data = self._read_raw()
        entry = data.get(scope)
        if entry is None:
            return None
        return entry.get("metadata", {})

    async def list(self, scope_prefix: str | None = None) -> list[SecretMetadata]:
        """Return metadata for all stored secrets, optionally filtered by *scope_prefix*."""
        data = self._read_raw()
        results: list[SecretMetadata] = []
        for scope, entry in data.items():
            if scope_prefix is not None and not scope.startswith(scope_prefix):
                continue
            results.append(
                SecretMetadata(
                    scope=scope,
                    secret_type=entry["secret"]["type"],
                    created_at=entry["created_at"],
                    version=entry["version"],
                    metadata=entry.get("metadata", {}),
                )
            )
        return results

    async def revoke(self, scope: str) -> bool:
        """Remove the secret for *scope*.

        Returns ``True`` if the scope existed, ``False`` otherwise (idempotent).
        """
        data = self._read_raw()
        if scope not in data:
            return False
        del data[scope]
        self._write_raw(data)
        return True

    async def rotate(self, scope: str, new_secret: Secret) -> str:
        """Replace the secret for *scope* with *new_secret*, bumping the version counter.

        Returns the new internal secret id. Raises ``KeyError`` if *scope* does
        not exist.
        """
        data = self._read_raw()
        if scope not in data:
            raise KeyError(f"scope {scope!r} not found in SecretStore")
        old_version = data[scope].get("version", 1)
        secret_id = str(uuid.uuid4())
        data[scope] = {
            "id": secret_id,
            "secret": new_secret,
            "created_at": data[scope]["created_at"],  # preserve original creation time
            "version": old_version + 1,
            "metadata": data[scope].get("metadata", {}),
        }
        self._write_raw(data)
        return secret_id


# ---------------------------------------------------------------------------
# SecretsStore — original plaintext store (kept for backward compatibility)
# ---------------------------------------------------------------------------


class SecretsStore:
    def __init__(self, secrets_path: Path) -> None:
        self._path = secrets_path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write({})
        self._path.chmod(0o600)

    def _read(self) -> dict[str, str]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, str]) -> None:
        atomic_write(self._path, json.dumps(data, indent=2))

    def get(self, key: str) -> str | None:
        return self._read().get(key)

    def set(self, key: str, value: str) -> None:
        data = self._read()
        data[key] = value
        self._write(data)

    def delete(self, key: str) -> bool:
        data = self._read()
        if key in data:
            del data[key]
            self._write(data)
            return True
        return False

    def list_keys(self) -> list[str]:
        return list(self._read().keys())
