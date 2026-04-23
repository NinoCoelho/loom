"""Transport-agnostic credential applier protocol."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Applier(Protocol):
    """Transport-agnostic credential applier protocol.

    Each concrete applier handles one secret type and one transport.
    """

    secret_type: str

    def apply(self, secret: Any, context: dict) -> Awaitable[Any]:
        """Turn *secret* into transport-ready material.

        Args:
            secret: The typed secret from ``SecretStore``.
            context: Transport-specific hints (``base_url``, ``version``, …).

        Returns:
            Transport-ready output (e.g. ``dict[str, str]`` headers or ``str``).

        """
        ...
