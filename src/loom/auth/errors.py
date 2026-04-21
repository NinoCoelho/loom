"""loom.auth.errors — error types for the credential/applier subsystem.

See docs/rfcs/0002-credentials-and-appliers.md for design rationale.
"""

from __future__ import annotations


class AuthApplierError(Exception):
    """Base class for all auth applier errors."""


class SecretExpiredError(AuthApplierError):
    """Raised when a secret (e.g. a bearer token) has passed its expiry time."""

    def __init__(self, scope: str | None = None) -> None:
        msg = "Secret expired" + (f" for scope {scope!r}" if scope else "")
        super().__init__(msg)
        self.scope = scope


class NoApplierError(AuthApplierError):
    """Raised when no applier is registered for (secret_type, transport)."""

    def __init__(self, scope: str, secret_type: str, transport: str) -> None:
        super().__init__(
            f"No applier registered for secret_type={secret_type!r}, transport={transport!r}"
            f" (scope={scope!r})"
        )
        self.scope = scope
        self.secret_type = secret_type
        self.transport = transport


class ScopeNotFoundError(AuthApplierError):
    """Raised when the requested scope is absent from the SecretStore."""

    def __init__(self, scope: str) -> None:
        super().__init__(f"Scope {scope!r} not found in SecretStore")
        self.scope = scope


class ScopeAccessDenied(AuthApplierError):
    """Raised by ``CredentialResolver`` when the ACL hook denies access.

    The ACL hook (``scope_acl`` kwarg on ``CredentialResolver``) is called
    before the policy enforcer.  Denial here means the *principal* is not
    allowed to use this scope at all — distinct from policy denial (HITL,
    time-box, etc.) which is ``CredentialDenied``.
    """

    def __init__(self, principal: str, scope: str) -> None:
        super().__init__(
            f"Principal {principal!r} is not allowed to access scope {scope!r}"
        )
        self.principal = principal
        self.scope = scope
