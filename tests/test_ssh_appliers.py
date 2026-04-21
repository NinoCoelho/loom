"""Tests for loom.auth.appliers — SSH appliers (RFC 0003).

All tests are unit tests (no network).  asyncssh is used only for key
generation; the import is guarded so the test file can still be collected
even if the optional extra is absent (tests skip automatically).
"""

from __future__ import annotations

import pytest

from loom.auth.appliers import SshConnectArgs, SshKeyApplier, SshPasswordApplier
from loom.store.secrets import PasswordSecret, SshPrivateKeySecret

try:
    import asyncssh  # noqa: F401

    _ASYNCSSH_AVAILABLE = True
except ImportError:
    _ASYNCSSH_AVAILABLE = False

_skip_no_asyncssh = pytest.mark.skipif(
    not _ASYNCSSH_AVAILABLE,
    reason="asyncssh not installed (pip install 'loom[ssh]')",
)

# ---------------------------------------------------------------------------
# SshConnectArgs
# ---------------------------------------------------------------------------


def test_ssh_connect_args_is_dict() -> None:
    args = SshConnectArgs(host="h", port=22, username="u", password="p")
    assert args["host"] == "h"
    assert args["port"] == 22
    assert args["username"] == "u"
    assert args["password"] == "p"


# ---------------------------------------------------------------------------
# SshPasswordApplier
# ---------------------------------------------------------------------------


async def test_ssh_password_applier_secret_type() -> None:
    assert SshPasswordApplier.secret_type == "password"


async def test_ssh_password_applier_basic() -> None:
    applier = SshPasswordApplier()
    secret: PasswordSecret = {"type": "password", "value": "s3cr3t"}
    ctx = {"metadata": {"hostname": "db1.prod", "port": 2222, "username": "ops"}}
    result = await applier.apply(secret, ctx)
    assert result["host"] == "db1.prod"
    assert result["port"] == 2222
    assert result["username"] == "ops"
    assert result["password"] == "s3cr3t"


async def test_ssh_password_applier_default_port() -> None:
    applier = SshPasswordApplier()
    secret: PasswordSecret = {"type": "password", "value": "pw"}
    ctx = {"metadata": {"hostname": "myhost", "username": "admin"}}
    result = await applier.apply(secret, ctx)
    assert result["port"] == 22


async def test_ssh_password_applier_host_alias() -> None:
    """Accepts 'host' as an alias for 'hostname' in metadata."""
    applier = SshPasswordApplier()
    secret: PasswordSecret = {"type": "password", "value": "pw"}
    ctx = {"metadata": {"host": "althost", "username": "admin"}}
    result = await applier.apply(secret, ctx)
    assert result["host"] == "althost"


async def test_ssh_password_applier_empty_metadata() -> None:
    applier = SshPasswordApplier()
    secret: PasswordSecret = {"type": "password", "value": "pw"}
    result = await applier.apply(secret, {})
    assert result["host"] == ""
    assert result["port"] == 22
    assert result["username"] == ""
    assert result["password"] == "pw"


async def test_ssh_password_applier_returns_ssh_connect_args() -> None:
    applier = SshPasswordApplier()
    secret: PasswordSecret = {"type": "password", "value": "pw"}
    result = await applier.apply(secret, {"metadata": {"hostname": "h", "username": "u"}})
    assert isinstance(result, SshConnectArgs)


# ---------------------------------------------------------------------------
# SshKeyApplier
# ---------------------------------------------------------------------------


async def test_ssh_key_applier_secret_type() -> None:
    assert SshKeyApplier.secret_type == "ssh_private_key"


@_skip_no_asyncssh
async def test_ssh_key_applier_basic() -> None:
    import asyncssh

    # Generate an ephemeral RSA key for testing.
    key = asyncssh.generate_private_key("ssh-rsa")
    pem = key.export_private_key("pkcs8-pem").decode()

    applier = SshKeyApplier()
    secret: SshPrivateKeySecret = {"type": "ssh_private_key", "key_pem": pem, "passphrase": None}
    ctx = {"metadata": {"hostname": "srv.example.com", "port": 22, "username": "deploy"}}
    result = await applier.apply(secret, ctx)

    assert result["host"] == "srv.example.com"
    assert result["port"] == 22
    assert result["username"] == "deploy"
    assert "client_keys" in result
    assert len(result["client_keys"]) == 1


@_skip_no_asyncssh
async def test_ssh_key_applier_with_passphrase() -> None:
    import asyncssh

    passphrase = "hunter2"
    key = asyncssh.generate_private_key("ssh-rsa")
    pem = key.export_private_key("pkcs8-pem", passphrase=passphrase.encode()).decode()

    applier = SshKeyApplier()
    secret: SshPrivateKeySecret = {
        "type": "ssh_private_key",
        "key_pem": pem,
        "passphrase": passphrase,
    }
    ctx = {"metadata": {"hostname": "host", "username": "user"}}
    result = await applier.apply(secret, ctx)
    assert "client_keys" in result


@_skip_no_asyncssh
async def test_ssh_key_applier_returns_ssh_connect_args() -> None:
    import asyncssh

    key = asyncssh.generate_private_key("ssh-rsa")
    pem = key.export_private_key("pkcs8-pem").decode()

    applier = SshKeyApplier()
    secret: SshPrivateKeySecret = {"type": "ssh_private_key", "key_pem": pem, "passphrase": None}
    result = await applier.apply(secret, {"metadata": {"hostname": "h", "username": "u"}})
    assert isinstance(result, SshConnectArgs)


async def test_ssh_key_applier_no_asyncssh_raises_importerror(monkeypatch) -> None:
    """Applying an SSH key secret without asyncssh installed raises ImportError."""
    import builtins

    real_import = builtins.__import__

    def _mock_import(name, *args, **kwargs):
        if name == "asyncssh":
            raise ImportError("no module named asyncssh")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _mock_import)
    applier = SshKeyApplier()
    secret: SshPrivateKeySecret = {
        "type": "ssh_private_key",
        "key_pem": "not-a-real-key",
        "passphrase": None,
    }
    with pytest.raises(ImportError, match="asyncssh"):
        await applier.apply(secret, {})
