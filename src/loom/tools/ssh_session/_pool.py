from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loom.llm.redact import redact_sensitive_text

from ._classify import _classify_error
from ._helpers import _ToolError, _err

if TYPE_CHECKING:
    from loom.auth.resolver import CredentialResolver


@dataclass
class _ScopeState:
    connection: Any = None
    sessions: dict[str, Any] = field(default_factory=dict)


class SshConnectionPool:
    def __init__(
        self,
        credential_resolver: CredentialResolver,
        known_hosts_path: str | bool | None = None,
        connect_timeout: float = 10.0,
        command_timeout: float = 60.0,
    ) -> None:
        self._resolver = credential_resolver
        self._known_hosts_path = known_hosts_path
        self._connect_timeout = connect_timeout
        self._command_timeout = command_timeout
        self._scopes: dict[str, _ScopeState] = {}
        self._lock = asyncio.Lock()

    async def ensure_connection(self, scope: str):
        try:
            import asyncssh  # noqa: F401
        except ImportError:
            raise _ToolError(
                _err("asyncssh is not installed. Run: pip install 'loom[ssh]'", "transport")
            )

        async with self._lock:
            state = self._scopes.get(scope)
            if state and state.connection is not None and not state.connection.is_closed():
                return state.connection

            import asyncssh

            try:
                connect_args = await self._resolver.resolve_for(scope=scope, transport="ssh")
            except Exception as exc:
                raise _ToolError(
                    _err(
                        "credential resolution failed — " + redact_sensitive_text(str(exc)),
                        "auth",
                    )
                )

            if self._known_hosts_path is False:
                warnings.warn(
                    "[LOOM SECURITY] host key checking DISABLED for SSH connection to "
                    f"{connect_args.get('host', scope)!r}. This is insecure — enable "
                    "strict known_hosts checking in production.",
                    stacklevel=2,
                )
                connect_args["known_hosts"] = None
            elif self._known_hosts_path is not None:
                connect_args["known_hosts"] = self._known_hosts_path

            connect_args["connect_timeout"] = self._connect_timeout

            conn = await asyncssh.connect(**connect_args)
            if state is None:
                state = _ScopeState(connection=conn)
                self._scopes[scope] = state
            else:
                state.connection = conn
            return conn

    async def run_remote(
        self, conn, cmd: str, timeout: float | None = None
    ) -> tuple[int, str, str]:
        result = await asyncio.wait_for(
            conn.run(cmd, check=False),
            timeout=timeout if timeout is not None else self._command_timeout,
        )
        return (
            result.exit_status if result.exit_status is not None else -1,
            result.stdout or "",
            result.stderr or "",
        )

    async def aclose(self) -> None:
        for state in self._scopes.values():
            conn = state.connection
            if conn is not None and not conn.is_closed():
                conn.close()
                try:
                    await conn.wait_closed()
                except Exception:
                    pass
        self._scopes.clear()
