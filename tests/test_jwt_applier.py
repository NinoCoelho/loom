"""Tests for loom.auth.appliers.JwtBearerApplier (RFC 0002 Phase C).

Covers:
- RS256 / ES256 / HS256 signing: sign and re-verify the JWT with PyJWT.
- Claims: iss, sub, aud, iat, exp, jti all present and correct.
- Cache: second call hits cache (no new jti); version bump invalidates.
- TTL: default 300 s applied when ttl_seconds absent.
- kid header: only set when key_id provided.
- Missing PyJWT raises ImportError.
"""

from __future__ import annotations

import time

import pytest

# ---------------------------------------------------------------------------
# PyJWT availability guard
# ---------------------------------------------------------------------------

pyjwt = pytest.importorskip("jwt", reason="PyJWT not installed (loom[jwt] required)")

# ---------------------------------------------------------------------------
# Key generation helpers (use cryptography which is a core dep)
# ---------------------------------------------------------------------------


def _generate_rsa_key_pem() -> tuple[str, str]:
    """Return (private_pem, public_pem) as strings."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


def _generate_ec_key_pem() -> tuple[str, str]:
    """Return (private_pem, public_pem) as strings for P-256."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key

    private_key = generate_private_key(SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_secret(
    private_key_pem: str,
    algorithm: str = "RS256",
    issuer: str = "test-issuer",
    audience: str = "test-audience",
    subject: str | None = "test-subject",
    ttl_seconds: int = 300,
    key_id: str | None = None,
):
    return {
        "type": "jwt_signing_key",
        "private_key_pem": private_key_pem,
        "algorithm": algorithm,
        "issuer": issuer,
        "audience": audience,
        "subject": subject,
        "ttl_seconds": ttl_seconds,
        "key_id": key_id,
    }


# ---------------------------------------------------------------------------
# RS256 tests
# ---------------------------------------------------------------------------


async def test_jwt_rs256_produces_bearer_header() -> None:
    from loom.auth.appliers import JwtBearerApplier

    priv_pem, _ = _generate_rsa_key_pem()
    secret = _make_secret(priv_pem, algorithm="RS256")
    applier = JwtBearerApplier()
    result = await applier.apply(secret, {})
    assert "Authorization" in result
    assert result["Authorization"].startswith("Bearer ")


async def test_jwt_rs256_verifiable_with_public_key() -> None:
    import jwt as pyjwt

    from loom.auth.appliers import JwtBearerApplier

    priv_pem, pub_pem = _generate_rsa_key_pem()
    secret = _make_secret(priv_pem, algorithm="RS256", issuer="my-iss", audience="my-aud")
    applier = JwtBearerApplier()
    result = await applier.apply(secret, {})
    token = result["Authorization"].split(" ", 1)[1]

    claims = pyjwt.decode(token, pub_pem, algorithms=["RS256"], audience="my-aud")
    assert claims["iss"] == "my-iss"
    assert claims["aud"] == "my-aud"
    assert claims["sub"] == "test-subject"
    assert "jti" in claims
    assert "iat" in claims
    assert "exp" in claims


async def test_jwt_claims_exp_iat_relationship() -> None:
    import jwt as pyjwt

    from loom.auth.appliers import JwtBearerApplier

    priv_pem, pub_pem = _generate_rsa_key_pem()
    secret = _make_secret(priv_pem, algorithm="RS256", ttl_seconds=600)
    applier = JwtBearerApplier()
    before = int(time.time())
    result = await applier.apply(secret, {})
    after = int(time.time())
    token = result["Authorization"].split(" ", 1)[1]

    claims = pyjwt.decode(
        token, pub_pem, algorithms=["RS256"], audience="test-audience"
    )
    assert claims["exp"] - claims["iat"] == 600
    assert before <= claims["iat"] <= after + 1


async def test_jwt_subject_omitted_when_none() -> None:
    import jwt as pyjwt

    from loom.auth.appliers import JwtBearerApplier

    priv_pem, pub_pem = _generate_rsa_key_pem()
    secret = _make_secret(priv_pem, algorithm="RS256", subject=None)
    applier = JwtBearerApplier()
    result = await applier.apply(secret, {})
    token = result["Authorization"].split(" ", 1)[1]

    claims = pyjwt.decode(
        token, pub_pem, algorithms=["RS256"], audience="test-audience"
    )
    assert "sub" not in claims


async def test_jwt_kid_header_included_when_set() -> None:
    import jwt as pyjwt

    from loom.auth.appliers import JwtBearerApplier

    priv_pem, pub_pem = _generate_rsa_key_pem()
    secret = _make_secret(priv_pem, algorithm="RS256", key_id="my-key-1")
    applier = JwtBearerApplier()
    result = await applier.apply(secret, {})
    token = result["Authorization"].split(" ", 1)[1]

    header = pyjwt.get_unverified_header(token)
    assert header["kid"] == "my-key-1"


async def test_jwt_no_kid_when_key_id_none() -> None:
    import jwt as pyjwt

    from loom.auth.appliers import JwtBearerApplier

    priv_pem, pub_pem = _generate_rsa_key_pem()
    secret = _make_secret(priv_pem, algorithm="RS256", key_id=None)
    applier = JwtBearerApplier()
    result = await applier.apply(secret, {})
    token = result["Authorization"].split(" ", 1)[1]

    header = pyjwt.get_unverified_header(token)
    assert "kid" not in header


# ---------------------------------------------------------------------------
# ES256 tests
# ---------------------------------------------------------------------------


async def test_jwt_es256_verifiable() -> None:
    import jwt as pyjwt

    from loom.auth.appliers import JwtBearerApplier

    priv_pem, pub_pem = _generate_ec_key_pem()
    secret = _make_secret(priv_pem, algorithm="ES256", issuer="ec-iss", audience="ec-aud")
    applier = JwtBearerApplier()
    result = await applier.apply(secret, {})
    token = result["Authorization"].split(" ", 1)[1]

    claims = pyjwt.decode(token, pub_pem, algorithms=["ES256"], audience="ec-aud")
    assert claims["iss"] == "ec-iss"


# ---------------------------------------------------------------------------
# HS256 tests
# ---------------------------------------------------------------------------


async def test_jwt_hs256_verifiable() -> None:
    import jwt as pyjwt

    from loom.auth.appliers import JwtBearerApplier

    shared_secret = "super-secret-hmac-key"
    secret = {
        "type": "jwt_signing_key",
        "private_key_pem": shared_secret,
        "algorithm": "HS256",
        "issuer": "hs-iss",
        "audience": "hs-aud",
        "subject": None,
        "ttl_seconds": 60,
        "key_id": None,
    }
    applier = JwtBearerApplier()
    result = await applier.apply(secret, {})
    token = result["Authorization"].split(" ", 1)[1]

    claims = pyjwt.decode(
        token,
        shared_secret.encode("utf-8"),
        algorithms=["HS256"],
        audience="hs-aud",
    )
    assert claims["iss"] == "hs-iss"


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------


async def test_jwt_cache_hit_same_scope_version() -> None:
    """Two apply() calls with the same scope+version must return the same jti."""

    from loom.auth.appliers import JwtBearerApplier

    priv_pem, pub_pem = _generate_rsa_key_pem()
    secret = _make_secret(priv_pem, algorithm="RS256")
    applier = JwtBearerApplier()
    ctx = {"scope": "s1", "version": 1}

    r1 = await applier.apply(secret, ctx)
    r2 = await applier.apply(secret, ctx)
    # Same token returned from cache
    assert r1["Authorization"] == r2["Authorization"]


async def test_jwt_cache_miss_on_version_bump() -> None:
    """Version bump must produce a fresh JWT (new jti)."""
    import jwt as pyjwt

    from loom.auth.appliers import JwtBearerApplier

    priv_pem, pub_pem = _generate_rsa_key_pem()
    secret = _make_secret(priv_pem, algorithm="RS256")
    applier = JwtBearerApplier()

    r1 = await applier.apply(secret, {"scope": "s2", "version": 1})
    r2 = await applier.apply(secret, {"scope": "s2", "version": 2})

    t1 = r1["Authorization"].split(" ", 1)[1]
    t2 = r2["Authorization"].split(" ", 1)[1]
    c1 = pyjwt.decode(t1, pub_pem, algorithms=["RS256"], audience="test-audience")
    c2 = pyjwt.decode(t2, pub_pem, algorithms=["RS256"], audience="test-audience")
    assert c1["jti"] != c2["jti"]


async def test_jwt_default_ttl_300_seconds() -> None:
    """When ttl_seconds is missing, default 300 seconds is used."""
    import jwt as pyjwt

    from loom.auth.appliers import JwtBearerApplier

    priv_pem, pub_pem = _generate_rsa_key_pem()
    secret = {
        "type": "jwt_signing_key",
        "private_key_pem": priv_pem,
        "algorithm": "RS256",
        "issuer": "i",
        "audience": "a",
        "subject": None,
        # ttl_seconds intentionally absent
        "key_id": None,
    }
    applier = JwtBearerApplier()
    result = await applier.apply(secret, {})
    token = result["Authorization"].split(" ", 1)[1]

    claims = pyjwt.decode(token, pub_pem, algorithms=["RS256"], audience="a")
    assert claims["exp"] - claims["iat"] == 300


# ---------------------------------------------------------------------------
# Secret type attribute
# ---------------------------------------------------------------------------


async def test_jwt_secret_type_attribute() -> None:
    from loom.auth.appliers import JwtBearerApplier
    assert JwtBearerApplier.secret_type == "jwt_signing_key"


# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------


async def test_jwt_missing_pyjwt_raises_import_error(monkeypatch) -> None:
    import sys

    # Inject None sentinel so the lazy `import jwt` inside apply() fails.
    # We do NOT reload the appliers module — that would corrupt global state for
    # tests that run afterward (e.g. SSH appliers tests).
    monkeypatch.setitem(sys.modules, "jwt", None)  # type: ignore[arg-type]

    from loom.auth.appliers import JwtBearerApplier
    applier = JwtBearerApplier()

    priv_pem, _ = _generate_rsa_key_pem()
    secret = {
        "type": "jwt_signing_key",
        "private_key_pem": priv_pem,
        "algorithm": "RS256",
        "issuer": "i",
        "audience": "a",
        "subject": None,
        "ttl_seconds": 300,
        "key_id": None,
    }
    with pytest.raises(ImportError, match="PyJWT"):
        await applier.apply(secret, {})
