"""Permission model for agent capabilities.

:class:`AgentPermissions` is a flat Pydantic model describing which
capabilities an agent is granted: memory writes, file edits, HTTP access,
delegate calls, etc. Used during runtime construction and by skill
handlers to gate operations.
"""

from __future__ import annotations

from pydantic import BaseModel


class AgentPermissions(BaseModel):
    soul_writable: bool = False
    identity_writable: bool = False
    user_writable: bool = True
    skills_creatable: bool = True
    skills_editable: bool = True
    skills_deletable: bool = False
    memory_writable: bool = True
    vault_writable: bool = True
    terminal_allowed: bool = False
    http_allowed: bool = True
    delegate_allowed: bool = True

    def can_edit_file(self, filename: str) -> bool:
        name = filename.upper()
        if name == "SOUL.MD" or name == "SOUL":
            return self.soul_writable
        if name == "IDENTITY.MD" or name == "IDENTITY":
            return self.identity_writable
        if name == "USER.MD" or name == "USER":
            return self.user_writable
        return False
