"""Tests for the ACP transport package."""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path

import pytest

from loom.acp import (
    NOT_CONFIGURED_MESSAGE,
    AcpCallTool,
    AcpConfig,
    call_agent,
    load_or_create_keypair,
    sign_challenge,
)

# ── Device key management ──────────────────────────────────────────────


class TestDeviceKeypair:
    def test_generate_and_persist(self, tmp_path: Path) -> None:
        key_path = tmp_path / "device_key"
        kp = load_or_create_keypair(key_path)
        assert key_path.exists()
        assert len(kp.public_hex) == 64  # 32 bytes hex
        mode = oct(key_path.stat().st_mode & 0o777)
        if os.name == "nt":
            # Windows doesn't expose POSIX mode bits the same way; verify
            # the file exists and is not world-executable.
            assert mode in {"0o600", "0o666"}
        else:
            assert mode == "0o600"

    def test_roundtrip_same_key(self, tmp_path: Path) -> None:
        key_path = tmp_path / "device_key"
        kp1 = load_or_create_keypair(key_path)
        kp2 = load_or_create_keypair(key_path)
        assert kp1.public_hex == kp2.public_hex

    def test_rejects_non_ed25519_pem(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

        key_path = tmp_path / "device_key"
        rsa = generate_private_key(public_exponent=65537, key_size=2048)
        pem = rsa.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key_path.write_bytes(pem)
        with pytest.raises(TypeError, match="Expected Ed25519"):
            load_or_create_keypair(key_path)

    def test_sign_verify_hex(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        kp = load_or_create_keypair(tmp_path / "device_key")
        nonce = "challenge-nonce-12345"
        sig_hex = sign_challenge(kp, nonce, encoding="hex")

        pub_bytes = bytes.fromhex(kp.public_hex)
        pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
        # Should not raise — signature verifies.
        pub.verify(bytes.fromhex(sig_hex), nonce.encode("utf-8"))

    def test_sign_verify_base64(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )

        kp = load_or_create_keypair(tmp_path / "device_key")
        nonce = "nonce-b64"
        sig_b64 = sign_challenge(kp, nonce, encoding="base64")

        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(kp.public_hex))
        pub.verify(base64.b64decode(sig_b64), nonce.encode("utf-8"))


# ── AcpConfig ──────────────────────────────────────────────────────────


class TestAcpConfig:
    def test_default_is_not_configured(self) -> None:
        cfg = AcpConfig()
        assert not cfg.configured

    def test_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LOOM_ACP_GATEWAY_URL", "ws://gateway.test/acp")
        monkeypatch.setenv("LOOM_ACP_TOKEN", "bearer-xyz")
        monkeypatch.setenv("LOOM_ACP_SIG_ENCODING", "base64")
        cfg = AcpConfig.from_env()
        assert cfg.gateway_url == "ws://gateway.test/acp"
        assert cfg.token == "bearer-xyz"
        assert cfg.sig_encoding == "base64"
        assert cfg.configured

    def test_from_env_missing_is_not_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LOOM_ACP_GATEWAY_URL", raising=False)
        monkeypatch.delenv("LOOM_ACP_TOKEN", raising=False)
        cfg = AcpConfig.from_env()
        assert not cfg.configured


# ── call_agent / AcpCallTool ───────────────────────────────────────────


class TestCallAgent:
    async def test_returns_not_configured_when_no_gateway(self) -> None:
        cfg = AcpConfig()
        result = await call_agent("peer-agent", "hello", cfg)
        assert result == NOT_CONFIGURED_MESSAGE


class TestAcpCallTool:
    def test_tool_spec_shape(self) -> None:
        tool = AcpCallTool(AcpConfig())
        spec = tool.tool
        assert spec.name == "acp_call"
        assert "agent_id" in spec.parameters["properties"]
        assert "message" in spec.parameters["properties"]
        assert set(spec.parameters["required"]) == {"agent_id", "message"}

    async def test_invoke_not_configured(self) -> None:
        tool = AcpCallTool(AcpConfig())
        result = await tool.invoke({"agent_id": "a", "message": "hi"})
        assert result.text == NOT_CONFIGURED_MESSAGE

    async def test_invoke_transport_failure_returns_error_text(self) -> None:
        # Pointing at an unreachable URL exercises the generic exception path
        # without requiring a live WS server.
        cfg = AcpConfig(
            gateway_url="ws://127.0.0.1:1/nowhere",
            open_timeout=0.5,
            key_path=Path(tempfile.mkdtemp()) / "device_key",
        )
        tool = AcpCallTool(cfg)
        result = await tool.invoke({"agent_id": "a", "message": "hi"})
        # Never raises — always returns a human-readable string.
        assert isinstance(result.text, str)
        assert result.text.startswith("ACP error")
