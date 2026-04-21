"""loom.auth.resolver — CredentialResolver: wires SecretStore + Appliers together.

The resolver is the single entry-point for consuming credentials::

    resolver = CredentialResolver(store=store, appliers={
        ("basic_auth", "http"): BasicHttpApplier(),
        ("bearer_token", "http"): BearerHttpApplier(),
        ("api_key", "llm_api_key"): ApiKeyStringApplier(),
    })

    headers = await resolver.resolve_for("prod-oic", transport="http")

See docs/rfcs/0002-credentials-and-appliers.md for design rationale.
"""

from __future__ import annotations

from typing import Any

from loom.auth.appliers import Applier
from loom.auth.errors import NoApplierError, ScopeNotFoundError
from loom.store.secrets import SecretStore


class CredentialResolver:
    """Looks up a secret and dispatches to the correct applier.

    Args:
        store: A ``SecretStore`` instance.
        appliers: Initial applier mapping ``(secret_type, transport) -> Applier``.
            Additional appliers can be added via ``register()``.

    Usage::

        resolver = CredentialResolver(store, {
            ("basic_auth", "http"): BasicHttpApplier(),
        })
        headers = await resolver.resolve_for("my-scope", "http")
    """

    def __init__(
        self,
        store: SecretStore,
        appliers: dict[tuple[str, str], Applier] | None = None,
    ) -> None:
        self._store = store
        self._appliers: dict[tuple[str, str], Applier] = dict(appliers or {})

    def register(self, applier: Applier, *, transport: str) -> None:
        """Register *applier* for its ``secret_type`` on *transport*.

        If an applier is already registered for the same ``(secret_type, transport)``
        pair it is replaced silently.
        """
        self._appliers[(applier.secret_type, transport)] = applier

    async def resolve_for(
        self,
        scope: str,
        transport: str,
        context: dict | None = None,
    ) -> Any:
        """Resolve credentials for *scope* on *transport*.

        1. Fetches the secret from the store.
        2. Selects the applier registered for ``(secret.type, transport)``.
        3. Calls ``applier.apply(secret, context)`` and returns the result.

        Args:
            scope: The loom scope name (e.g. ``"prod-oic"``).
            transport: The target transport (e.g. ``"http"``, ``"llm_api_key"``).
            context: Optional extra hints forwarded to the applier unchanged.
                Merged with ``{"scope": scope}`` before dispatch.

        Raises:
            ScopeNotFoundError: If *scope* is absent from the store.
            NoApplierError: If no applier is registered for the resolved
                ``(secret_type, transport)`` pair.
        """
        secret = await self._store.get(scope)
        if secret is None:
            raise ScopeNotFoundError(scope)

        secret_type: str = secret["type"]  # type: ignore[index]
        applier = self._appliers.get((secret_type, transport))
        if applier is None:
            raise NoApplierError(scope, secret_type, transport)

        merged_context: dict = {"scope": scope, **(context or {})}
        return await applier.apply(secret, merged_context)  # type: ignore[arg-type]
