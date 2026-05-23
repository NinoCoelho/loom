from __future__ import annotations


def _classify_error(exc: Exception) -> str:
    try:
        import asyncssh
    except ImportError:
        pass
    else:
        if isinstance(exc, asyncssh.PermissionDenied):
            return "auth"
        if isinstance(exc, asyncssh.HostKeyNotVerifiable):
            return "auth"
        if isinstance(
            exc,
            (asyncssh.DisconnectError, asyncssh.ConnectionLost, asyncssh.ChannelOpenError),
        ):
            return "transport"
        if isinstance(exc, asyncssh.Error):
            msg = str(exc).lower()
            if any(k in msg for k in ("auth", "permission", "denied", "key", "password")):
                return "auth"
            return "transport"

    exc_type = type(exc).__name__.lower()
    exc_msg = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in exc_type or "timeout" in exc_msg:
        return "timeout"
    if any(k in exc_msg for k in ("auth", "permission", "denied", "key", "password")):
        return "auth"
    if any(k in exc_msg for k in ("connect", "refused", "reset", "broken pipe", "network")):
        return "transport"
    return "unknown"
