"""Protocol definition for secret stores.

Both :class:`~loom.store.secrets.SecretStore` (Fernet-encrypted) and
:class:`~loom.store.keychain.KeychainStore` (OS keychain) implement
this interface. The protocol enables type-safe swapping and easier testing.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from loom.store.secrets import Secret, SecretMetadata


@runtime_checkable
class SecretStoreProtocol(Protocol):
    """Common interface for all secret store backends."""

    async def put(
        self, scope: str, secret: Secret, *, metadata: dict | None = None
    ) -> str: ...

    async def get(self, scope: str) -> Secret | None: ...

    async def get_metadata(self, scope: str) -> dict | None: ...

    async def list(self, scope_prefix: str | None = None) -> list[SecretMetadata]: ...

    async def revoke(self, scope: str) -> bool: ...

    async def rotate(self, scope: str, new_secret: Secret) -> str: ...
