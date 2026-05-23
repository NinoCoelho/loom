"""Security utilities for loom agents.

* :class:`BinaryRegistry` — hash-verified trusted binary allowlist.
  Prevents agents from executing untrusted or modified executables via
  the terminal tool or download tool.
"""

from loom.security.binary_registry import BinaryRegistry as BinaryRegistry

__all__ = ["BinaryRegistry"]
