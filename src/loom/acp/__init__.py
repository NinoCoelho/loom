"""ACP transport — multi-agent calls over a WebSocket gateway.

Optional subpackage; requires the ``[acp]`` install extra
(``cryptography`` + ``websockets``). The module is importable without the
extras, but ``call_agent`` / ``load_or_create_keypair`` will raise
``ImportError`` on first use if they are missing.
"""

from loom.acp.client import (
    NOT_CONFIGURED_MESSAGE,
    AcpConfig,
    call_agent,
)
from loom.acp.device import (
    DEFAULT_DEVICE_KEY_PATH,
    DeviceKeypair,
    load_or_create_keypair,
    sign_challenge,
)
from loom.acp.tool import AcpCallTool

__all__ = [
    "AcpCallTool",
    "AcpConfig",
    "DEFAULT_DEVICE_KEY_PATH",
    "DeviceKeypair",
    "NOT_CONFIGURED_MESSAGE",
    "call_agent",
    "load_or_create_keypair",
    "sign_challenge",
]
