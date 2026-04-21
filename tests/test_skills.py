from loom.skills.registry import SkillRegistry


def test_empty_registry(tmp_dir):
    reg = SkillRegistry(tmp_dir / "skills")
    reg.scan()
    assert reg.list() == []


def test_descriptions_empty(tmp_dir):
    reg = SkillRegistry(tmp_dir / "skills")
    reg.scan()
    assert reg.descriptions() == []


def test_additional_dirs(tmp_dir):
    agent_dir = tmp_dir / "agent-skills"
    shared_dir = tmp_dir / "shared-skills"
    agent_dir.mkdir()
    shared_dir.mkdir()

    reg = SkillRegistry(agent_dir, additional_dirs=[shared_dir])
    reg.scan()
    assert reg.skills_dir == agent_dir
    assert shared_dir in reg.additional_dirs


def test_register_unregister(tmp_dir):
    from loom.skills.types import Skill

    reg = SkillRegistry(tmp_dir / "skills")
    skill = Skill(name="test", description="A test", body="body", source_dir=str(tmp_dir))
    reg.register(skill)
    assert reg.get("test") is not None
    reg.unregister("test")
    assert reg.get("test") is None


def test_reload(tmp_dir):
    from loom.skills.types import Skill

    reg = SkillRegistry(tmp_dir / "skills")
    reg.register(Skill(name="x", description="X", body="x", source_dir=str(tmp_dir)))
    assert reg.get("x") is not None
    reg.reload()
    assert reg.get("x") is None
