"""SSH credential appliers — password and key-based authentication."""

from __future__ import annotations

from loom.store.secrets import PasswordSecret, SshPrivateKeySecret


class SshConnectArgs(dict):
    """Normalized connection args fed to ``asyncssh.connect()``."""


class SshPasswordApplier:
    """Applies a ``password`` secret as SSH password authentication."""

    secret_type: str = "password"

    async def apply(self, secret: PasswordSecret, context: dict) -> SshConnectArgs:  # type: ignore[override]
        meta = context.get("metadata") or {}
        host = meta.get("hostname") or meta.get("host") or ""
        port = int(meta.get("port", 22))
        username = meta.get("username") or meta.get("user") or ""
        return SshConnectArgs(
            host=host,
            port=port,
            username=username,
            password=secret["value"],
        )


class SshKeyApplier:
    """Applies an ``ssh_private_key`` secret as SSH public-key authentication."""

    secret_type: str = "ssh_private_key"

    async def apply(self, secret: SshPrivateKeySecret, context: dict) -> SshConnectArgs:  # type: ignore[override]
        try:
            import asyncssh
        except ImportError as exc:
            raise ImportError(
                "asyncssh is required for SSH key auth. Install it with: pip install 'loom[ssh]'"
            ) from exc

        passphrase = secret.get("passphrase")
        key = asyncssh.import_private_key(
            secret["key_pem"],
            passphrase=passphrase.encode() if passphrase else None,
        )
        meta = context.get("metadata") or {}
        host = meta.get("hostname") or meta.get("host") or ""
        port = int(meta.get("port", 22))
        username = meta.get("username") or meta.get("user") or ""
        return SshConnectArgs(
            host=host,
            port=port,
            username=username,
            client_keys=[key],
        )
