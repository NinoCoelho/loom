from loom.prompt import (
    PromptBuilder,
    PromptSection,
    load_context_section,
    load_identity_sections,
    load_memory_preview,
    load_pending_section,
    load_skills_section,
)


class TestPromptSection:
    def test_create(self):
        s = PromptSection(name="test", content="hello", priority=50)
        assert s.name == "test"
        assert s.content == "hello"
        assert s.priority == 50
        assert not s.writable

    def test_repr(self):
        s = PromptSection(name="x", content="y", priority=10)
        assert "x" in repr(s)


class TestPromptBuilder:
    def test_empty(self):
        b = PromptBuilder()
        assert b.build() == ""

    def test_single_section(self):
        b = PromptBuilder()
        b.add(PromptSection(name="a", content="hello", priority=10))
        assert b.build() == "hello"

    def test_priority_ordering(self):
        b = PromptBuilder()
        b.add(PromptSection(name="late", content="last", priority=100))
        b.add(PromptSection(name="early", content="first", priority=1))
        b.add(PromptSection(name="mid", content="middle", priority=50))
        result = b.build()
        assert result.index("first") < result.index("middle") < result.index("last")

    def test_remove(self):
        b = PromptBuilder()
        b.add(PromptSection(name="a", content="hello", priority=10))
        b.remove("a")
        assert b.build() == ""

    def test_update(self):
        b = PromptBuilder()
        b.add(PromptSection(name="a", content="old", priority=10, writable=False))
        b.update("a", "new")
        assert "new" in b.build()

    def test_list_sections(self):
        b = PromptBuilder()
        b.add(PromptSection(name="b", content="b", priority=20))
        b.add(PromptSection(name="a", content="a", priority=10))
        sections = b.list_sections()
        assert sections[0].name == "a"
        assert sections[1].name == "b"

    def test_skip_empty(self):
        b = PromptBuilder()
        b.add(PromptSection(name="empty", content="  ", priority=10))
        b.add(PromptSection(name="full", content="content", priority=20))
        assert b.build() == "content"


class TestLoadIdentitySections:
    def test_loads_three_sections(self, agent_home, perms_full):
        sections = load_identity_sections(agent_home, perms_full)
        assert len(sections) == 3
        names = [s.name for s in sections]
        assert "soul" in names
        assert "identity" in names
        assert "user" in names

    def test_priorities(self, agent_home, perms_default):
        sections = load_identity_sections(agent_home, perms_default)
        by_name = {s.name: s for s in sections}
        assert by_name["soul"].priority < by_name["identity"].priority < by_name["user"].priority

    def test_writable_flags(self, agent_home, perms_full):
        sections = load_identity_sections(agent_home, perms_full)
        by_name = {s.name: s for s in sections}
        assert by_name["soul"].writable
        assert by_name["identity"].writable
        assert by_name["user"].writable

    def test_default_perms_no_soul_write(self, agent_home, perms_default):
        sections = load_identity_sections(agent_home, perms_default)
        by_name = {s.name: s for s in sections}
        assert not by_name["soul"].writable
        assert not by_name["identity"].writable
        assert by_name["user"].writable


class TestLoadMemoryPreview:
    def test_no_memories(self):
        s = load_memory_preview([])
        assert s is None

    def test_with_memories(self):
        s = load_memory_preview([("key1", "some content"), ("key2", "more content")])
        assert s is not None
        assert s.name == "memory"
        assert s.priority == 35
        assert "key1" in s.content


class TestLoadSkillsSection:
    def test_no_skills(self):
        assert load_skills_section([]) is None

    def test_with_skills(self):
        s = load_skills_section([("greet", "How to greet"), ("plan", "How to plan")])
        assert s is not None
        assert "greet" in s.content
        assert "plan" in s.content


class TestLoadContextSection:
    def test_none(self):
        assert load_context_section(None) is None

    def test_empty(self):
        assert load_context_section({}) is None

    def test_with_context(self):
        s = load_context_section({"target": "prod", "env": "staging"})
        assert "prod" in s.content


class TestLoadPendingSection:
    def test_none(self):
        assert load_pending_section(None) is None

    def test_with_question(self):
        s = load_pending_section("Should I proceed?")
        assert "proceed" in s.content
