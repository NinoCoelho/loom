"""Tests for loom.store.secrets.SecretStore (RFC 0002 Phase A).

Covers: put/get/list/revoke/rotate for each secret type; Fernet key generated
on first use; storage file mode 0600; rotation bumps version; prefix filter;
missing scope returns None; revoke is idempotent; file on disk is encrypted.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from loom.store.secrets import (
    ApiKeySecret,
    BasicAuthSecret,
    BearerTokenSecret,
    OAuth2ClientCredentialsSecret,
    PasswordSecret,
    SecretStore,
    SshPrivateKeySecret,
)


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(
        path=tmp_path / "secrets.db",
        key_path=tmp_path / "keys" / "secrets.key",
    )


# ---------------------------------------------------------------------------
# Roundtrip tests per secret type
# ---------------------------------------------------------------------------


async def test_put_get_password(store: SecretStore) -> None:
    secret: PasswordSecret = {"type": "password", "value": "s3cr3t"}
    await store.put("scope-pw", secret)
    result = await store.get("scope-pw")
    assert result == secret


async def test_put_get_api_key(store: SecretStore) -> None:
    secret: ApiKeySecret = {"type": "api_key", "value": "ak-abc123"}
    await store.put("scope-ak", secret)
    result = await store.get("scope-ak")
    assert result == secret


async def test_put_get_basic_auth(store: SecretStore) -> None:
    secret: BasicAuthSecret = {"type": "basic_auth", "username": "user", "password": "pass"}
    await store.put("scope-ba", secret)
    result = await store.get("scope-ba")
    assert result == secret


async def test_put_get_bearer_token(store: SecretStore) -> None:
    secret: BearerTokenSecret = {
        "type": "bearer_token",
        "token": "tok-xyz",
        "expires_at": None,
    }
    await store.put("scope-bt", secret)
    result = await store.get("scope-bt")
    assert result == secret


async def test_put_get_oauth2_cc(store: SecretStore) -> None:
    secret: OAuth2ClientCredentialsSecret = {
        "type": "oauth2_client_credentials",
        "client_id": "cid",
        "client_secret": "csecret",
        "token_url": "https://auth.example.com/token",
        "scopes": ["read", "write"],
    }
    await store.put("scope-oauth2", secret)
    result = await store.get("scope-oauth2")
    assert result == secret


async def test_put_get_ssh_private_key(store: SecretStore) -> None:
    secret: SshPrivateKeySecret = {
        "type": "ssh_private_key",
        "key_pem": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----",
        "passphrase": None,
    }
    await store.put("scope-ssh", secret)
    result = await store.get("scope-ssh")
    assert result == secret


# ---------------------------------------------------------------------------
# Missing scope returns None
# ---------------------------------------------------------------------------


async def test_get_missing_scope_returns_none(store: SecretStore) -> None:
    result = await store.get("does-not-exist")
    assert result is None


# ---------------------------------------------------------------------------
# Revoke
# ---------------------------------------------------------------------------


async def test_revoke_existing(store: SecretStore) -> None:
    await store.put("scope-rev", {"type": "api_key", "value": "key"})
    removed = await store.revoke("scope-rev")
    assert removed is True
    assert await store.get("scope-rev") is None


async def test_revoke_idempotent(store: SecretStore) -> None:
    await store.put("scope-rev2", {"type": "api_key", "value": "key"})
    assert await store.revoke("scope-rev2") is True
    assert await store.revoke("scope-rev2") is False  # second call returns False


async def test_revoke_nonexistent(store: SecretStore) -> None:
    result = await store.revoke("never-existed")
    assert result is False


# ---------------------------------------------------------------------------
# Rotate
# ---------------------------------------------------------------------------


async def test_rotate_bumps_version(store: SecretStore) -> None:
    await store.put("scope-rot", {"type": "api_key", "value": "v1"})

    # Check initial version via list().
    meta_list = await store.list()
    meta = next(m for m in meta_list if m["scope"] == "scope-rot")
    assert meta["version"] == 1

    await store.rotate("scope-rot", {"type": "api_key", "value": "v2"})

    meta_list2 = await store.list()
    meta2 = next(m for m in meta_list2 if m["scope"] == "scope-rot")
    assert meta2["version"] == 2
    assert (await store.get("scope-rot"))["value"] == "v2"  # type: ignore[index]


async def test_rotate_missing_scope_raises(store: SecretStore) -> None:
    with pytest.raises(KeyError):
        await store.rotate("no-such-scope", {"type": "api_key", "value": "x"})


# ---------------------------------------------------------------------------
# List with prefix filter
# ---------------------------------------------------------------------------


async def test_list_all(store: SecretStore) -> None:
    await store.put("alpha/one", {"type": "api_key", "value": "a"})
    await store.put("alpha/two", {"type": "api_key", "value": "b"})
    await store.put("beta/one", {"type": "api_key", "value": "c"})

    all_meta = await store.list()
    scopes = {m["scope"] for m in all_meta}
    assert {"alpha/one", "alpha/two", "beta/one"} == scopes


async def test_list_with_prefix(store: SecretStore) -> None:
    await store.put("alpha/one", {"type": "api_key", "value": "a"})
    await store.put("alpha/two", {"type": "api_key", "value": "b"})
    await store.put("beta/one", {"type": "api_key", "value": "c"})

    alpha_meta = await store.list("alpha/")
    scopes = {m["scope"] for m in alpha_meta}
    assert scopes == {"alpha/one", "alpha/two"}
    assert "beta/one" not in scopes


async def test_list_metadata_shape(store: SecretStore) -> None:
    await store.put("shaped", {"type": "password", "value": "pw"}, metadata={"note": "test"})
    meta_list = await store.list()
    meta = next(m for m in meta_list if m["scope"] == "shaped")
    assert meta["secret_type"] == "password"
    assert "created_at" in meta
    assert meta["version"] == 1
    assert meta["metadata"]["note"] == "test"


# ---------------------------------------------------------------------------
# Security properties
# ---------------------------------------------------------------------------


async def test_fernet_key_generated_on_first_use(tmp_path: Path) -> None:
    key_path = tmp_path / "keys" / "secrets.key"
    assert not key_path.exists()
    store = SecretStore(path=tmp_path / "secrets.db", key_path=key_path)
    await store.put("s", {"type": "api_key", "value": "v"})
    assert key_path.exists()


async def test_key_file_mode_0600(tmp_path: Path) -> None:
    key_path = tmp_path / "keys" / "secrets.key"
    store = SecretStore(path=tmp_path / "secrets.db", key_path=key_path)
    await store.put("s", {"type": "api_key", "value": "v"})
    mode = stat.S_IMODE(os.stat(key_path).st_mode)
    assert mode == 0o600


async def test_storage_file_mode_0600(tmp_path: Path) -> None:
    db_path = tmp_path / "secrets.db"
    store = SecretStore(path=db_path, key_path=tmp_path / "keys" / "secrets.key")
    await store.put("s", {"type": "api_key", "value": "v"})
    mode = stat.S_IMODE(os.stat(db_path).st_mode)
    assert mode == 0o600


async def test_file_content_is_encrypted(tmp_path: Path) -> None:
    """The plaintext secret value must not appear in the stored file."""
    store = SecretStore(
        path=tmp_path / "secrets.db",
        key_path=tmp_path / "keys" / "secrets.key",
    )
    plaintext_value = "super-secret-api-key-XYZ"
    await store.put("s", {"type": "api_key", "value": plaintext_value})
    raw = (tmp_path / "secrets.db").read_bytes()
    assert plaintext_value.encode() not in raw


async def test_env_override_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """LOOM_SECRET_KEY env var overrides the key file."""
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("LOOM_SECRET_KEY", key)

    store = SecretStore(
        path=tmp_path / "secrets.db",
        key_path=tmp_path / "keys" / "secrets.key",
    )
    await store.put("env-scope", {"type": "api_key", "value": "env-val"})
    result = await store.get("env-scope")
    assert result is not None
    assert result["value"] == "env-val"  # type: ignore[index]
