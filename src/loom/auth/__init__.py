"""loom.auth — credential appliers, resolver, and policies (RFC 0002).

Public surface::

    from loom.auth import (
        # Phase A — appliers + resolver
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
        # Phase B — policies + HITL enforcer
        PolicyMode,
        CredentialPolicy,
        PolicyStore,
        GateDecision,
        CredentialDenied,
        PolicyEnforcer,
    )

Layer 2 of RFC 0002: appliers turn typed ``Secret`` objects into
transport-ready material (HTTP headers, plain strings, etc.) without the
agent ever seeing raw credential bytes.

Layer 3 of RFC 0002: policies gate credential access via HITL before the
secret is retrieved from the store.

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
from loom.auth.enforcer import CredentialDenied, GateDecision, PolicyEnforcer
from loom.auth.errors import (
    AuthApplierError,
    NoApplierError,
    ScopeNotFoundError,
    SecretExpiredError,
)
from loom.auth.policies import CredentialPolicy, PolicyMode
from loom.auth.policy_store import PolicyStore
from loom.auth.resolver import CredentialResolver

__all__ = [
    # Phase A
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
    # Phase B
    "PolicyMode",
    "CredentialPolicy",
    "PolicyStore",
    "GateDecision",
    "CredentialDenied",
    "PolicyEnforcer",
]
