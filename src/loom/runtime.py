from __future__ import annotations

from pathlib import Path

from loom.home import AgentHome
from loom.llm.base import LLMProvider
from loom.llm.registry import ProviderRegistry
from loom.loop import Agent, AgentConfig
from loom.permissions import AgentPermissions
from loom.skills.registry import SkillRegistry
from loom.store.memory import MemoryStore
from loom.store.session import SessionStore
from loom.tools.delegate import DelegateTool
from loom.tools.memory import MemoryToolHandler
from loom.tools.profile import EditIdentityTool
from loom.tools.registry import ToolRegistry


class AgentRuntime:
    def __init__(self, loom_home: Path | None = None) -> None:
        self._home = loom_home or Path.home() / ".loom"
        self._home.mkdir(parents=True, exist_ok=True)
        (self._home / "agents").mkdir(exist_ok=True)
        (self._home / "shared-skills").mkdir(exist_ok=True)
        self._agents: dict[str, Agent] = {}
        self._agent_homes: dict[str, AgentHome] = {}
        self._agent_configs: dict[str, AgentConfig] = {}
        self._agent_permissions: dict[str, AgentPermissions] = {}
        self._session_stores: dict[str, SessionStore] = {}
        self._memory_stores: dict[str, MemoryStore] = {}
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
        agent_home = AgentHome(self._home / "agents" / name, name)
        agent_home.initialize()

        perms = permissions or AgentPermissions()
        cfg = config or AgentConfig()

        agent_home.skills_dir.mkdir(exist_ok=True)

        shared = self.shared_skills_dir
        skill_registry = SkillRegistry(
            agent_home.skills_dir,
            additional_dirs=[shared] if shared.exists() else [],
        )
        if any(agent_home.skills_dir.iterdir()) or (shared.exists() and any(shared.iterdir())):
            skill_registry.scan()

        session_store = SessionStore(agent_home.sessions_db)
        self._session_stores[name] = session_store

        memory_store = MemoryStore(agent_home.memory_dir, agent_home.memory_index_db)
        self._memory_stores[name] = memory_store

        tool_registry = ToolRegistry()

        if perms.memory_writable:
            tool_registry.register(MemoryToolHandler(memory_store))

        if perms.soul_writable or perms.identity_writable or perms.user_writable:
            tool_registry.register(EditIdentityTool(agent_home, perms))

        if perms.delegate_allowed:
            tool_registry.register(DelegateTool(self))

        for extra_tool in cfg.extra_tools:
            tool_registry.register(extra_tool)

        agent = Agent(
            provider=provider,
            provider_registry=self.get_provider_registry() if self._provider_registry else None,
            tool_registry=tool_registry,
            skill_registry=skill_registry,
            config=cfg,
            agent_home=agent_home,
            permissions=perms,
            memory_store=memory_store,
        )

        self._agents[name] = agent
        self._agent_homes[name] = agent_home
        self._agent_configs[name] = cfg
        self._agent_permissions[name] = perms

        return agent

    def get_agent(self, name: str) -> Agent | None:
        return self._agents.get(name)

    def list_agents(self) -> list[str]:
        return list(self._agents.keys())

    def remove_agent(self, name: str) -> bool:
        if name not in self._agents:
            return False
        del self._agents[name]
        self._agent_homes.pop(name, None)
        self._agent_configs.pop(name, None)
        self._agent_permissions.pop(name, None)
        session_store = self._session_stores.pop(name, None)
        if session_store is not None:
            session_store.close()
        memory_store = self._memory_stores.pop(name, None)
        if memory_store is not None:
            memory_store.close()
        return True

    def close(self) -> None:
        for name in list(self._agents):
            self.remove_agent(name)
        self._session_stores.clear()
        self._memory_stores.clear()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def get_session_store(self, agent_name: str) -> SessionStore | None:
        return self._session_stores.get(agent_name)

    def get_memory_store(self, agent_name: str) -> MemoryStore | None:
        return self._memory_stores.get(agent_name)

    def get_agent_home(self, agent_name: str) -> AgentHome | None:
        return self._agent_homes.get(agent_name)

    def get_agent_permissions(self, agent_name: str) -> AgentPermissions | None:
        return self._agent_permissions.get(agent_name)
