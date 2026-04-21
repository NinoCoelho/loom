"""loom.store.keychain — KeychainStore: OS keychain-backed secret store.

Uses the ``keyring`` library (python-keyring) which abstracts over:

- macOS Keychain
- Linux Secret Service (libsecret / gnome-keyring)
- Windows Credential Manager
- Plaintext fallback (dev/test — not recommended for production)

Secrets are stored as JSON strings under a configurable service name
(default ``"loom"``).  Metadata is stored in a parallel namespace
``"<service>:metadata"``.

Install:  ``pip install "loom[keychain]"``

See docs/rfcs/0002-credentials-and-appliers.md for design rationale.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from loom.store.secrets import Secret, SecretMetadata

if TYPE_CHECKING:
    pass


class KeychainStore:
    """OS keychain-backed secret store (RFC 0002 Phase C).

    Args:
        service: Keyring service namespace (default ``"loom"``).  Each secret
            is stored under ``(service, scope)`` in the OS keychain.
            Metadata is stored under ``(service + ":metadata", scope)``.

    Raises:
        ImportError: At construction time if ``keyring`` is not installed.

    Usage::

        store = KeychainStore()
        await store.put("prod-api", {"type": "api_key", "value": "sk-..."})
        secret = await store.get("prod-api")
    """

    def __init__(self, service: str = "loom") -> None:
        try:
            import keyring as _keyring  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "keyring is required for KeychainStore. "
                "Install it with: pip install \"loom[keychain]\""
            ) from exc
        self._service = service
        self._meta_service = f"{service}:metadata"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _kr(self):  # type: ignore[return]
        import keyring
        return keyring

    def _get_raw(self, scope: str) -> dict | None:
        val = self._kr().get_password(self._service, scope)
        if val is None:
            return None
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return None

    def _set_raw(self, scope: str, data: dict) -> None:
        self._kr().set_password(self._service, scope, json.dumps(data))

    def _delete_raw(self, scope: str) -> None:
        try:
            self._kr().delete_password(self._service, scope)
        except Exception:
            pass  # already absent — idempotent

    def _get_meta_raw(self, scope: str) -> dict | None:
        val = self._kr().get_password(self._meta_service, scope)
        if val is None:
            return None
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return None

    def _set_meta_raw(self, scope: str, data: dict) -> None:
        self._kr().set_password(self._meta_service, scope, json.dumps(data))

    def _delete_meta_raw(self, scope: str) -> None:
        try:
            self._kr().delete_password(self._meta_service, scope)
        except Exception:
            pass

    def _list_scopes(self) -> list[str]:
        """Return all scope names stored in this service.

        keyring has no standard "list all credentials" API — the method varies
        by backend.  We maintain a separate ``"__index__"`` key under the
        metadata service that tracks the active scope set.
        """
        val = self._kr().get_password(self._meta_service, "__index__")
        if val is None:
            return []
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            return []

    def _add_to_index(self, scope: str) -> None:
        scopes = self._list_scopes()
        if scope not in scopes:
            scopes.append(scope)
            self._kr().set_password(
                self._meta_service, "__index__", json.dumps(scopes)
            )

    def _remove_from_index(self, scope: str) -> None:
        scopes = self._list_scopes()
        if scope in scopes:
            scopes.remove(scope)
            self._kr().set_password(
                self._meta_service, "__index__", json.dumps(scopes)
            )

    # ------------------------------------------------------------------
    # Public API (mirrors SecretStore)
    # ------------------------------------------------------------------

    async def put(self, scope: str, secret: Secret, *, metadata: dict | None = None) -> str:
        """Store *secret* under *scope* in the OS keychain.

        Returns a new UUID secret id.
        """
        secret_id = str(uuid.uuid4())
        entry = {
            "id": secret_id,
            "secret": secret,
        }
        self._set_raw(scope, entry)

        meta_entry = {
            "scope": scope,
            "secret_type": secret["type"],  # type: ignore[index]
            "created_at": datetime.now(UTC).isoformat(),
            "version": 1,
            "metadata": metadata or {},
        }
        # Preserve creation time and bump version if updating an existing entry.
        existing_meta = self._get_meta_raw(scope)
        if existing_meta is not None:
            meta_entry["created_at"] = existing_meta.get(
                "created_at", meta_entry["created_at"]
            )
            meta_entry["version"] = existing_meta.get("version", 0) + 1

        self._set_meta_raw(scope, meta_entry)
        self._add_to_index(scope)
        return secret_id

    async def get(self, scope: str) -> Secret | None:
        """Return the ``Secret`` for *scope*, or ``None`` if absent."""
        entry = self._get_raw(scope)
        if entry is None:
            return None
        return entry.get("secret")  # type: ignore[return-value]

    async def get_metadata(self, scope: str) -> dict | None:
        """Return the free-form metadata dict for *scope*, or ``None`` if absent."""
        meta = self._get_meta_raw(scope)
        if meta is None:
            return None
        return meta.get("metadata", {})

    async def list(self, scope_prefix: str | None = None) -> list[SecretMetadata]:
        """Return metadata for all stored secrets, optionally filtered by *scope_prefix*."""
        results: list[SecretMetadata] = []
        for scope in self._list_scopes():
            if scope_prefix is not None and not scope.startswith(scope_prefix):
                continue
            meta = self._get_meta_raw(scope)
            if meta is None:
                continue
            results.append(
                SecretMetadata(
                    scope=scope,
                    secret_type=meta.get("secret_type", ""),
                    created_at=meta.get("created_at", ""),
                    version=meta.get("version", 1),
                    metadata=meta.get("metadata", {}),
                )
            )
        return results

    async def revoke(self, scope: str) -> bool:
        """Remove the secret for *scope* from the OS keychain.

        Returns ``True`` if the scope existed, ``False`` otherwise.
        """
        entry = self._get_raw(scope)
        if entry is None:
            return False
        self._delete_raw(scope)
        self._delete_meta_raw(scope)
        self._remove_from_index(scope)
        return True

    async def rotate(self, scope: str, new_secret: Secret) -> str:
        """Replace the secret for *scope* with *new_secret*, bumping the version counter.

        Returns the new secret id.  Raises ``KeyError`` if *scope* does not exist.
        """
        existing_meta = self._get_meta_raw(scope)
        if existing_meta is None:
            raise KeyError(f"scope {scope!r} not found in KeychainStore")

        secret_id = str(uuid.uuid4())
        entry = {"id": secret_id, "secret": new_secret}
        self._set_raw(scope, entry)

        updated_meta = {
            **existing_meta,
            "secret_type": new_secret["type"],  # type: ignore[index]
            "version": existing_meta.get("version", 1) + 1,
            "metadata": existing_meta.get("metadata", {}),
        }
        self._set_meta_raw(scope, updated_meta)
        return secret_id
