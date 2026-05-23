"""loom.tools.ssh_session — SshSessionTool: persistent remote shells via tmux."""

from ._classify import _classify_error as _classify_error
from ._helpers import _valid_session_id as _valid_session_id
from ._pool import _ScopeState as _ScopeState
from ._session import SshSessionTool as SshSessionTool

__all__ = ["SshSessionTool"]
