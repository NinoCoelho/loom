"""Tests for loom.auth.appliers.SigV4Applier (RFC 0002 Phase C).

Uses a frozen datetime and known-good SigV4 test vectors where applicable.
Primary goals:
1. Authorization header is present and has correct SigV4 shape.
2. No plaintext secret_access_key appears in the resulting headers.
3. Session token (x-amz-security-token) is included when present.
4. region / service overrides work correctly.
5. Missing botocore raises ImportError with helpful message.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_secret(
    access_key_id: str = "AKIAIOSFODNN7EXAMPLE",
    secret_access_key: str = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
    session_token: str | None = None,
    region: str | None = "us-east-1",
):
    return {
        "type": "aws_sigv4",
        "access_key_id": access_key_id,
        "secret_access_key": secret_access_key,
        "session_token": session_token,
        "region": region,
    }


# ---------------------------------------------------------------------------
# botocore availability guard
# ---------------------------------------------------------------------------


botocore = pytest.importorskip("botocore", reason="botocore not installed (loom[aws] required)")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_sigv4_produces_authorization_header() -> None:
    from loom.auth.appliers import SigV4Applier

    applier = SigV4Applier()
    secret = _make_secret()
    context = {
        "method": "GET",
        "url": "https://execute-api.us-east-1.amazonaws.com/prod/resource",
        "service": "execute-api",
        "region": "us-east-1",
    }
    headers = await applier.apply(secret, context)

    assert "Authorization" in headers
    auth = headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256")
    assert "Credential=" in auth
    assert "SignedHeaders=" in auth
    assert "Signature=" in auth


async def test_sigv4_no_plaintext_secret_in_headers() -> None:
    from loom.auth.appliers import SigV4Applier

    secret_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    applier = SigV4Applier()
    secret = _make_secret(secret_access_key=secret_key)
    context = {
        "method": "POST",
        "url": "https://s3.amazonaws.com/my-bucket/my-key",
        "service": "s3",
        "region": "us-east-1",
        "body": b"hello world",
    }
    headers = await applier.apply(secret, context)

    headers_str = str(headers)
    assert secret_key not in headers_str


async def test_sigv4_includes_x_amz_date() -> None:
    from loom.auth.appliers import SigV4Applier

    applier = SigV4Applier()
    secret = _make_secret()
    context = {
        "method": "GET",
        "url": "https://sts.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        "service": "sts",
        "region": "us-east-1",
    }
    headers = await applier.apply(secret, context)
    # botocore injects x-amz-date
    assert any(k.lower() == "x-amz-date" for k in headers)


async def test_sigv4_session_token_included() -> None:
    from loom.auth.appliers import SigV4Applier

    applier = SigV4Applier()
    secret = _make_secret(session_token="FAKE_SESSION_TOKEN_XYZ")
    context = {
        "method": "GET",
        "url": "https://s3.amazonaws.com/my-bucket/",
        "service": "s3",
        "region": "us-east-1",
    }
    headers = await applier.apply(secret, context)
    # STS session token should appear as x-amz-security-token
    assert any(k.lower() == "x-amz-security-token" for k in headers)
    security_token_header = next(
        v for k, v in headers.items() if k.lower() == "x-amz-security-token"
    )
    assert security_token_header == "FAKE_SESSION_TOKEN_XYZ"


async def test_sigv4_region_override_from_context() -> None:
    """Context region should take precedence over secret region."""
    from loom.auth.appliers import SigV4Applier

    applier = SigV4Applier()
    secret = _make_secret(region="us-east-1")  # secret says us-east-1
    context = {
        "method": "GET",
        "url": "https://s3.eu-west-1.amazonaws.com/bucket/",
        "service": "s3",
        "region": "eu-west-1",  # override
    }
    headers = await applier.apply(secret, context)
    auth = headers["Authorization"]
    # Credential scope should contain eu-west-1
    assert "eu-west-1" in auth


async def test_sigv4_defaults_region_from_secret() -> None:
    """When context has no region, falls back to secret.region."""
    from loom.auth.appliers import SigV4Applier

    applier = SigV4Applier()
    secret = _make_secret(region="ap-southeast-1")
    context = {
        "method": "GET",
        "url": "https://execute-api.ap-southeast-1.amazonaws.com/v1/test",
        "service": "execute-api",
        # no "region" key
    }
    headers = await applier.apply(secret, context)
    auth = headers["Authorization"]
    assert "ap-southeast-1" in auth


async def test_sigv4_secret_type_attribute() -> None:
    from loom.auth.appliers import SigV4Applier
    assert SigV4Applier.secret_type == "aws_sigv4"


async def test_sigv4_missing_botocore_raises_import_error(monkeypatch) -> None:
    """When botocore is absent, apply() raises ImportError with helpful message."""
    import sys

    # Inject None sentinels so the lazy `import botocore.auth` inside apply() fails.
    # We do NOT reload the appliers module — that would corrupt global state for
    # tests that run afterward (e.g. SSH appliers tests).
    monkeypatch.setitem(sys.modules, "botocore.auth", None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "botocore.awsrequest", None)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "botocore.credentials", None)  # type: ignore[arg-type]

    from loom.auth.appliers import SigV4Applier
    applier = SigV4Applier()

    with pytest.raises(ImportError, match="botocore"):
        await applier.apply(
            {
                "type": "aws_sigv4",
                "access_key_id": "AK",
                "secret_access_key": "SK",
                "session_token": None,
                "region": "us-east-1",
            },
            {"method": "GET", "url": "https://example.com/"},
        )
