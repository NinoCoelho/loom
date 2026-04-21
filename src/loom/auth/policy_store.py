"""loom.auth.policy_store — file-backed persistence for CredentialPolicy objects.

Stores policies as a plain JSON file (not encrypted — policies are metadata,
not secrets).  Writes are atomic via ``loom.store.atomic.atomic_write`` and
the file is created with mode 0600.

Conventional location: ``$LOOM_HOME/policies.json`` — the caller passes the
path; this module is path-agnostic.

See docs/rfcs/0002-credentials-and-appliers.md for full design rationale.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from loom.auth.policies import CredentialPolicy, PolicyMode
from loom.store.atomic import atomic_write


def _policy_to_dict(policy: CredentialPolicy) -> dict:
    return {
        "scope": policy.scope,
        "mode": policy.mode.value,
        "window_start": policy.window_start.isoformat() if policy.window_start else None,
        "window_end": policy.window_end.isoformat() if policy.window_end else None,
        "uses_remaining": policy.uses_remaining,
        "prompt_message": policy.prompt_message,
    }


def _policy_from_dict(d: dict) -> CredentialPolicy:
    from datetime import UTC, datetime

    def _parse_dt(v: str | None) -> datetime | None:
        if v is None:
            return None
        dt = datetime.fromisoformat(v)
        # Ensure timezone-aware if missing (treat as UTC)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    return CredentialPolicy(
        scope=d["scope"],
        mode=PolicyMode(d["mode"]),
        window_start=_parse_dt(d.get("window_start")),
        window_end=_parse_dt(d.get("window_end")),
        uses_remaining=d.get("uses_remaining"),
        prompt_message=d.get("prompt_message"),
    )


class PolicyStore:
    """File-backed store for :class:`~loom.auth.policies.CredentialPolicy` objects.

    Args:
        path: Path to the JSON file.  The parent directory is created if
            necessary.  The file is written with mode 0600.

    All methods are ``async`` for API symmetry with ``SecretStore``; they
    perform no I/O-bound work and do not actually yield to the event loop.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    # ── Internal helpers ─────────────────────────────────────────────

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        with self._path.open() as f:
            return json.load(f)

    def _save(self, data: dict[str, dict]) -> None:
        content = json.dumps(data, indent=2)
        atomic_write(self._path, content)
        # Enforce 0600 — atomic_write may create with umask-derived mode
        os.chmod(self._path, 0o600)

    # ── Public API ───────────────────────────────────────────────────

    async def put(self, policy: CredentialPolicy) -> None:
        """Persist *policy*, replacing any existing entry for the same scope."""
        data = self._load()
        data[policy.scope] = _policy_to_dict(policy)
        self._save(data)

    async def get(self, scope: str) -> CredentialPolicy | None:
        """Return the policy for *scope*, or ``None`` if not found."""
        data = self._load()
        entry = data.get(scope)
        if entry is None:
            return None
        return _policy_from_dict(entry)

    async def delete(self, scope: str) -> bool:
        """Delete the policy for *scope*.  Returns ``True`` if it existed."""
        data = self._load()
        if scope not in data:
            return False
        del data[scope]
        self._save(data)
        return True

    async def list(self, scope_prefix: str | None = None) -> list[CredentialPolicy]:
        """Return all policies, optionally filtered to those whose scope starts with
        *scope_prefix*."""
        data = self._load()
        policies = [_policy_from_dict(v) for v in data.values()]
        if scope_prefix is not None:
            policies = [p for p in policies if p.scope.startswith(scope_prefix)]
        return policies

    async def decrement_uses(self, scope: str) -> int:
        """Decrement ``uses_remaining`` for *scope* and return the new value.

        Returns ``-1`` if there is no policy for *scope* or the policy has no
        ``uses_remaining`` counter (i.e. it is an unlimited policy).  Never
        raises for missing scopes.
        """
        data = self._load()
        entry = data.get(scope)
        if entry is None or entry.get("uses_remaining") is None:
            return -1
        new_val: int = entry["uses_remaining"] - 1
        entry["uses_remaining"] = new_val
        data[scope] = entry
        self._save(data)
        return new_val
