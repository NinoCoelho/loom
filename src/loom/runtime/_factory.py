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

from loom.runtime._record import AgentRecord


class AgentFactory:
    @staticmethod
    def create(
        name: str,
        agents_dir: Path,
        shared_skills_dir: Path,
        provider_registry: ProviderRegistry | None,
        runtime: object,
        config: AgentConfig | None = None,
        permissions: AgentPermissions | None = None,
        provider: LLMProvider | None = None,
    ) -> AgentRecord:
        agent_home = AgentHome(agents_dir / name, name)
        agent_home.initialize()

        perms = permissions or AgentPermissions()
        cfg = config or AgentConfig()

        agent_home.skills_dir.mkdir(exist_ok=True)

        shared = shared_skills_dir
        skill_registry = SkillRegistry(
            agent_home.skills_dir,
            additional_dirs=[shared] if shared.exists() else [],
        )
        if any(agent_home.skills_dir.iterdir()) or (shared.exists() and any(shared.iterdir())):
            skill_registry.scan()

        session_store = SessionStore(agent_home.sessions_db)
        memory_store = MemoryStore(agent_home.memory_dir, agent_home.memory_index_db)

        tool_registry = ToolRegistry()

        if perms.memory_writable:
            tool_registry.register(MemoryToolHandler(memory_store))

        if perms.soul_writable or perms.identity_writable or perms.user_writable:
            tool_registry.register(EditIdentityTool(agent_home, perms))

        if perms.delegate_allowed:
            tool_registry.register(DelegateTool(runtime))

        for extra_tool in cfg.extra_tools:
            tool_registry.register(extra_tool)

        agent = Agent(
            provider=provider,
            provider_registry=provider_registry,
            tool_registry=tool_registry,
            skill_registry=skill_registry,
            config=cfg,
            agent_home=agent_home,
            permissions=perms,
            memory_store=memory_store,
        )

        return AgentRecord(
            agent=agent,
            home=agent_home,
            config=cfg,
            permissions=perms,
            session_store=session_store,
            memory_store=memory_store,
        )
