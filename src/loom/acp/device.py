"""ACP device identity — Ed25519 keypair management.

Each Loom instance has a stable device identity used to authenticate with an
ACP gateway. Keys are persisted to disk (PEM, mode 0600) and written
atomically. The ``cryptography`` package is imported lazily so the broader
``loom.acp`` package remains importable without the ``[acp]`` extra.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — type-only
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


DEFAULT_DEVICE_KEY_PATH = Path("~/.loom/device_key").expanduser()


@dataclass
class DeviceKeypair:
    """A loaded Ed25519 device keypair plus its public-key hex encoding."""

    private_key: "Ed25519PrivateKey"
    public_hex: str


def _require_cryptography():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except ImportError as exc:
        raise ImportError(
            "loom.acp requires the 'cryptography' package. "
            "Install with: pip install 'loom[acp]'"
        ) from exc
    return serialization, Ed25519PrivateKey


def load_or_create_keypair(
    path: Path | None = None,
) -> DeviceKeypair:
    """Load an existing Ed25519 keypair from *path* or generate and persist one.

    The private key is stored as PEM with mode 0600 and written atomically
    (sibling ``.tmp`` file + rename) to avoid corruption on partial writes.
    """
    serialization, Ed25519PrivateKey = _require_cryptography()
    key_path = path or DEFAULT_DEVICE_KEY_PATH

    if key_path.exists():
        pem = key_path.read_bytes()
        private_key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError(
                f"Expected Ed25519 key in {key_path}, "
                f"got {type(private_key).__name__}"
            )
    else:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        private_key = Ed25519PrivateKey.generate()
        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        tmp = key_path.with_suffix(".tmp")
        tmp.write_bytes(pem)
        os.chmod(tmp, 0o600)
        tmp.rename(key_path)

    pub_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return DeviceKeypair(private_key=private_key, public_hex=pub_bytes.hex())


def sign_challenge(
    keypair: DeviceKeypair,
    nonce: str,
    encoding: str = "hex",
) -> str:
    """Sign a nonce string with the device private key.

    Args:
        keypair: Loaded device keypair.
        nonce: Challenge nonce (UTF-8 encoded before signing).
        encoding: ``"hex"`` (default) or ``"base64"``.
    """
    sig_bytes = keypair.private_key.sign(nonce.encode("utf-8"))
    if encoding == "base64":
        return base64.b64encode(sig_bytes).decode("ascii")
    return sig_bytes.hex()
