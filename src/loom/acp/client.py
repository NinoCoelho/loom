"""ACP client — call external agents over a WebSocket gateway.

Protocol
--------
1. Client connects with optional ``Authorization: Bearer <token>`` header.
2. Server MAY send ``{"type": "challenge", "nonce": str}``. If so, the client
   responds with ``{"type": "auth", "device_id": <pubkey hex>, "signature":
   <ed25519 sig>}`` and waits for ``{"type": "auth_ok"}``. A server that skips
   the challenge frame is treated as anonymous.
3. Client sends ``{"type": "call", "agent_id": ..., "message": ...,
   "request_id": ...}``.
4. Server streams ``{"type": "delta", "text": ...}`` frames, terminated by a
   ``{"type": "done", "result": ...}`` or ``{"type": "error", "message": ...}``
   frame.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from loom.acp.device import DeviceKeypair, load_or_create_keypair, sign_challenge

NOT_CONFIGURED_MESSAGE = (
    "ACP bridge not configured. Set gateway_url (or LOOM_ACP_GATEWAY_URL) "
    "to enable external agent calls."
)


@dataclass
class AcpConfig:
    """Configuration for the ACP WebSocket client."""

    gateway_url: str = ""
    token: str = ""
    sig_encoding: str = "hex"
    open_timeout: float = 10.0
    recv_timeout: float = 10.0
    key_path: Path | None = None

    @classmethod
    def from_env(cls, *, prefix: str = "LOOM_ACP_") -> AcpConfig:
        """Build config from ``LOOM_ACP_*`` env vars.

        Recognised: ``LOOM_ACP_GATEWAY_URL``, ``LOOM_ACP_TOKEN``,
        ``LOOM_ACP_SIG_ENCODING``, ``LOOM_ACP_KEY_PATH``.
        """
        key_path_env = os.environ.get(f"{prefix}KEY_PATH", "")
        return cls(
            gateway_url=os.environ.get(f"{prefix}GATEWAY_URL", ""),
            token=os.environ.get(f"{prefix}TOKEN", ""),
            sig_encoding=os.environ.get(f"{prefix}SIG_ENCODING", "hex"),
            key_path=Path(key_path_env).expanduser() if key_path_env else None,
        )

    @property
    def configured(self) -> bool:
        return bool(self.gateway_url)


def _require_websockets():
    try:
        import websockets
    except ImportError as exc:
        raise ImportError(
            "loom.acp requires the 'websockets' package. Install with: pip install 'loom[acp]'"
        ) from exc
    return websockets


async def call_agent(
    agent_id: str,
    message: str,
    config: AcpConfig,
) -> str:
    """Call an external agent over an ACP WebSocket gateway.

    Returns the concatenated response text or a human-readable error string.
    Never raises on transport failures — error conditions are returned as text
    so the LLM tool-call boundary remains stable.
    """
    if not config.configured:
        return NOT_CONFIGURED_MESSAGE

    try:
        keypair = load_or_create_keypair(config.key_path)
    except Exception as exc:
        return f"ACP device key error: {exc}"

    websockets = _require_websockets()
    request_id = uuid.uuid4().hex

    try:
        headers = {"Authorization": f"Bearer {config.token}"} if config.token else {}
        async with websockets.connect(
            config.gateway_url,
            additional_headers=headers,
            open_timeout=config.open_timeout,
        ) as ws:
            # Auth handshake — optional challenge frame.
            raw = await asyncio.wait_for(ws.recv(), timeout=config.recv_timeout)
            challenge = json.loads(raw)
            if challenge.get("type") == "challenge":
                sig = sign_challenge(keypair, challenge["nonce"], encoding=config.sig_encoding)
                await ws.send(
                    json.dumps(
                        {
                            "type": "auth",
                            "device_id": keypair.public_hex,
                            "signature": sig,
                        }
                    )
                )
                auth_resp = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=config.recv_timeout)
                )
                if auth_resp.get("type") != "auth_ok":
                    return f"ACP auth failed: {auth_resp.get('reason', 'unknown')}"
            # else: anonymous gateway — the first frame was something else.
            # (Not currently consumed; gateways either challenge or accept the
            # call directly. We can extend this if a new frame type appears.)

            await ws.send(
                json.dumps(
                    {
                        "type": "call",
                        "agent_id": agent_id,
                        "message": message,
                        "request_id": request_id,
                    }
                )
            )

            parts: list[str] = []
            async for raw in ws:
                frame = json.loads(raw)
                ftype = frame.get("type")
                if ftype == "delta":
                    parts.append(frame.get("text", ""))
                elif ftype == "done":
                    parts.append(frame.get("result", ""))
                    break
                elif ftype == "error":
                    return f"ACP error: {frame.get('message', 'remote error')}"
            return "".join(parts) or "(empty response)"

    except TimeoutError:
        return "ACP error: connection timed out"
    except Exception as exc:
        return f"ACP error: {exc}"


__all__ = [
    "AcpConfig",
    "NOT_CONFIGURED_MESSAGE",
    "call_agent",
    "DeviceKeypair",
]
