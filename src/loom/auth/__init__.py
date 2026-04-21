"""loom.auth — credential appliers and resolver (RFC 0002 Phase A).

Public surface::

    from loom.auth import (
        Applier,
        BasicHttpApplier,
        BearerHttpApplier,
        OAuth2CCHttpApplier,
        ApiKeyHeaderApplier,
        ApiKeyStringApplier,
        CredentialResolver,
        AuthApplierError,
        SecretExpiredError,
        NoApplierError,
        ScopeNotFoundError,
    )

Layer 2 of RFC 0002: appliers turn typed ``Secret`` objects into
transport-ready material (HTTP headers, plain strings, etc.) without the
agent ever seeing raw credential bytes.

See docs/rfcs/0002-credentials-and-appliers.md for full design rationale.
"""

from __future__ import annotations

from loom.auth.appliers import (
    ApiKeyHeaderApplier,
    ApiKeyStringApplier,
    Applier,
    BasicHttpApplier,
    BearerHttpApplier,
    OAuth2CCHttpApplier,
)
from loom.auth.errors import (
    AuthApplierError,
    NoApplierError,
    ScopeNotFoundError,
    SecretExpiredError,
)
from loom.auth.resolver import CredentialResolver

__all__ = [
    "Applier",
    "BasicHttpApplier",
    "BearerHttpApplier",
    "OAuth2CCHttpApplier",
    "ApiKeyHeaderApplier",
    "ApiKeyStringApplier",
    "CredentialResolver",
    "AuthApplierError",
    "SecretExpiredError",
    "NoApplierError",
    "ScopeNotFoundError",
]
