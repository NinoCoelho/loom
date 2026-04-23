"""JWT Bearer credential applier — produces signed JWTs for client-assertion."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from loom.store.secrets import JwtSigningKeySecret


class JwtBearerApplier:
    """Produces a signed JWT and applies it as ``Authorization: Bearer <jwt>``."""

    secret_type: str = "jwt_signing_key"

    def __init__(self) -> None:
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
