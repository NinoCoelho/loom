"""loom.auth.resolver — CredentialResolver: wires SecretStore + Appliers together.

The resolver is the single entry-point for consuming credentials::

    resolver = CredentialResolver(store=store, appliers={
        ("basic_auth", "http"): BasicHttpApplier(),
        ("bearer_token", "http"): BearerHttpApplier(),
        ("api_key", "llm_api_key"): ApiKeyStringApplier(),
    })

    headers = await resolver.resolve_for("prod-oic", transport="http")

Phase B adds an optional ``enforcer`` parameter.  When provided, the enforcer
gates credential access *before* the secret is fetched.  Callers that omit
``enforcer`` retain identical Phase A behaviour — fully backward compatible.

Phase C adds an optional ``scope_acl`` callable.  When provided it is called
*before* the policy enforcer with ``(principal, scope, transport)`` and must
return ``True`` to allow or ``False`` to deny.  ``principal`` is taken from
``context["principal"]``; if not supplied and an ACL is installed, a
``MissingPrincipalError`` is raised.  Default ``None`` = allow-all.

See docs/rfcs/0002-credentials-and-appliers.md for design rationale.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from loom.auth.appliers import Applier
from loom.auth.errors import NoApplierError, ScopeAccessDenied, ScopeNotFoundError
from loom.store.secrets import SecretStore

if TYPE_CHECKING:
    from loom.auth.enforcer import PolicyEnforcer

# Callable type: (principal: str, scope: str, transport: str) -> bool
ScopeAcl = Callable[[str, str, str], bool]


class MissingPrincipalError(Exception):
    """Raised when a scope ACL is configured but ``context["principal"]`` is absent."""

    def __init__(self, scope: str) -> None:
        super().__init__(
            f"ACL is installed but no principal was provided in context for scope {scope!r}. "
            "Pass context={'principal': '<name>'} to resolve_for()."
        )
        self.scope = scope


class CredentialResolver:
    """Looks up a secret and dispatches to the correct applier.

    Args:
        store: A ``SecretStore`` instance.
        appliers: Initial applier mapping ``(secret_type, transport) -> Applier``.
            Additional appliers can be added via ``register()``.
        enforcer: Optional :class:`~loom.auth.enforcer.PolicyEnforcer`.  When
            supplied, ``gate(scope, context)`` is called before the secret is
            fetched.  A denied gate raises
            :class:`~loom.auth.enforcer.CredentialDenied`.  If ``None``
            (default), gating is skipped entirely — Phase A behaviour is
            preserved.
        scope_acl: Optional ACL callable ``(principal, scope, transport) -> bool``.
            Called before the policy enforcer.  If it returns ``False``,
            :class:`~loom.auth.errors.ScopeAccessDenied` is raised.  If
            ``None`` (default), all principals are allowed (backward-compat).

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
        *,
        enforcer: PolicyEnforcer | None = None,
        scope_acl: ScopeAcl | None = None,
    ) -> None:
        self._store = store
        self._appliers: dict[tuple[str, str], Applier] = dict(appliers or {})
        self._enforcer = enforcer
        self._scope_acl = scope_acl

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

        1. Gates via ``enforcer.gate(scope, context)`` if an enforcer is set.
        2. Fetches the secret from the store.
        3. Selects the applier registered for ``(secret.type, transport)``.
        4. Calls ``applier.apply(secret, context)`` and returns the result.

        The :class:`~loom.auth.enforcer.GateDecision` from step 1 is merged
        into the applier context as ``"_gate_decision"`` so appliers can
        inspect ``prompt_resolution`` when needed.

        Args:
            scope: The loom scope name (e.g. ``"prod-oic"``).
            transport: The target transport (e.g. ``"http"``, ``"llm_api_key"``).
            context: Optional extra hints forwarded to the applier unchanged.
                Merged with ``{"scope": scope}`` before dispatch.

        Raises:
            CredentialDenied: If the enforcer denies access.
            ScopeNotFoundError: If *scope* is absent from the store.
            NoApplierError: If no applier is registered for the resolved
                ``(secret_type, transport)`` pair.
        """
        merged_context: dict = {"scope": scope, **(context or {})}

        # Phase C ACL — runs before enforcer; skipped when no ACL is configured
        if self._scope_acl is not None:
            principal = merged_context.get("principal")
            if principal is None:
                raise MissingPrincipalError(scope)
            if not self._scope_acl(principal, scope, transport):
                raise ScopeAccessDenied(principal, scope)

        # Phase B gate — skipped when no enforcer is configured (Phase A compat)
        if self._enforcer is not None:
            gate_decision = await self._enforcer.gate(scope, merged_context)
            merged_context["_gate_decision"] = gate_decision

        secret = await self._store.get(scope)
        if secret is None:
            raise ScopeNotFoundError(scope)

        # Inject scope metadata so appliers (e.g. SSH) can read hostname/port/username
        # without callers having to fetch it separately.  Only set if not already provided.
        if "metadata" not in merged_context and hasattr(self._store, "get_metadata"):
            scope_meta = await self._store.get_metadata(scope)  # type: ignore[union-attr]
            if scope_meta is not None:
                merged_context["metadata"] = scope_meta

        secret_type: str = secret["type"]  # type: ignore[index]
        applier = self._appliers.get((secret_type, transport))
        if applier is None:
            raise NoApplierError(scope, secret_type, transport)

        return await applier.apply(secret, merged_context)  # type: ignore[arg-type]
