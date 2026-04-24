"""Transport-agnostic credential appliers.

An *applier* turns a typed ``Secret`` into ready-to-use material for a
specific transport (HTTP headers, raw string, etc.). Each applier handles
exactly one (secret_type, transport) pair.

The ``context`` dict may carry transport-specific hints.  Known keys:

- ``base_url`` (str) — target base URL.
- ``version`` (int) — secret version from ``SecretStore``.
"""

from loom.auth.appliers._aws import SigV4Applier
from loom.auth.appliers._http import (
    ApiKeyHeaderApplier,
    ApiKeyStringApplier,
    BasicHttpApplier,
    BearerHttpApplier,
    OAuth2CCHttpApplier,
)
from loom.auth.appliers._jwt import JwtBearerApplier
from loom.auth.appliers._protocol import Applier
from loom.auth.appliers._ssh import SshConnectArgs, SshKeyApplier, SshPasswordApplier

__all__ = [
    "Applier",
    # HTTP
    "BasicHttpApplier",
    "BearerHttpApplier",
    "OAuth2CCHttpApplier",
    "ApiKeyHeaderApplier",
    "ApiKeyStringApplier",
    # SSH
    "SshConnectArgs",
    "SshPasswordApplier",
    "SshKeyApplier",
    # AWS
    "SigV4Applier",
    # JWT
    "JwtBearerApplier",
]
