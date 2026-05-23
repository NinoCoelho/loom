from __future__ import annotations

from dataclasses import dataclass

from loom.home import AgentHome
from loom.loop import Agent, AgentConfig
from loom.permissions import AgentPermissions
from loom.store.memory import MemoryStore
from loom.store.session import SessionStore


@dataclass
class AgentRecord:
    agent: Agent
    home: AgentHome
    config: AgentConfig
    permissions: AgentPermissions
    session_store: SessionStore
    memory_store: MemoryStore
