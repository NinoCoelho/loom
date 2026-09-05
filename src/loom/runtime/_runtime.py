"""Agent runtime — assembles all components into a runnable agent.

:class:`AgentRuntime` owns the loom home directory and provides a
high-level API to create, retrieve, and destroy agents, wiring together
the LLM provider registry, skill registry, memory store, session store,
and tool registry for each agent.
"""

from __future__ import annotations

from pathlib import Path

from loom.home import AgentHome
from loom.llm.base import LLMProvider
from loom.llm.registry import ProviderRegistry
from loom.loop import Agent, AgentConfig
from loom.permissions import AgentPermissions
from loom.runtime._factory import AgentFactory
from loom.runtime._record import AgentRecord
from loom.store.memory import MemoryStore
from loom.store.session import SessionStore


class AgentRuntime:
    def __init__(self, loom_home: Path | None = None) -> None:
        self._home = loom_home or Path.home() / ".loom"
        self._home.mkdir(parents=True, exist_ok=True)
        (self._home / "agents").mkdir(exist_ok=True)
        (self._home / "shared-skills").mkdir(exist_ok=True)
        self._records: dict[str, AgentRecord] = {}
        self._provider_registry: ProviderRegistry | None = None

    @property
    def loom_home(self) -> Path:
        return self._home

    @property
    def shared_skills_dir(self) -> Path:
        return self._home / "shared-skills"

    def set_provider_registry(self, registry: ProviderRegistry) -> None:
        self._provider_registry = registry

    def get_provider_registry(self) -> ProviderRegistry:
        if self._provider_registry is None:
            self._provider_registry = ProviderRegistry()
        return self._provider_registry

    def create_agent(
        self,
        name: str,
        config: AgentConfig | None = None,
        permissions: AgentPermissions | None = None,
        provider: LLMProvider | None = None,
    ) -> Agent:
        record = AgentFactory.create(
            name=name,
            agents_dir=self._home / "agents",
            shared_skills_dir=self.shared_skills_dir,
            provider_registry=self.get_provider_registry() if self._provider_registry else None,
            runtime=self,
            config=config,
            permissions=permissions,
            provider=provider,
        )
        self._records[name] = record
        return record.agent

    def get_agent(self, name: str) -> Agent | None:
        record = self._records.get(name)
        return record.agent if record else None

    def list_agents(self) -> list[str]:
        return list(self._records.keys())

    def remove_agent(self, name: str) -> bool:
        record = self._records.pop(name, None)
        if record is None:
            return False
        record.session_store.close()
        record.memory_store.close()
        return True

    def close(self) -> None:
        for name in list(self._records):
            self.remove_agent(name)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def get_session_store(self, agent_name: str) -> SessionStore | None:
        record = self._records.get(agent_name)
        return record.session_store if record else None

    def get_memory_store(self, agent_name: str) -> MemoryStore | None:
        record = self._records.get(agent_name)
        return record.memory_store if record else None

    def get_agent_home(self, agent_name: str) -> AgentHome | None:
        record = self._records.get(agent_name)
        return record.home if record else None

    def get_agent_permissions(self, agent_name: str) -> AgentPermissions | None:
        record = self._records.get(agent_name)
        return record.permissions if record else None
