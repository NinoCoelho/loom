from loom.home import AgentHome


def test_initialize_creates_structure(agent_home):
    assert agent_home.path.exists()
    assert agent_home.soul_path.exists()
    assert agent_home.identity_path.exists()
    assert agent_home.user_path.exists()
    assert agent_home.skills_dir.exists()
    assert agent_home.memory_dir.exists()
    assert agent_home.vault_dir.exists()


def test_default_files_have_content(agent_home):
    soul = agent_home.read_soul()
    assert len(soul) > 0
    assert "Soul" in soul

    identity = agent_home.read_identity()
    assert len(identity) > 0
    assert "Identity" in identity

    user = agent_home.read_user()
    assert len(user) > 0


def test_write_identity_files(agent_home):
    agent_home.write_soul("# Custom Soul\nBe excellent.")
    assert "Be excellent" in agent_home.read_soul()

    agent_home.write_identity("# Custom Identity\nName: Tester")
    assert "Tester" in agent_home.read_identity()

    agent_home.write_user("# Custom User\nPrefers dark mode.")
    assert "dark mode" in agent_home.read_user()


def test_validate_clean(agent_home):
    issues = agent_home.validate()
    assert len(issues) == 0


def test_validate_missing_files(tmp_dir):
    home = AgentHome(tmp_dir / "missing", "missing")
    issues = home.validate()
    assert len(issues) > 0
    assert any("does not exist" in i for i in issues)


def test_name_from_path(tmp_dir):
    home = AgentHome(tmp_dir / "agents" / "my-agent")
    assert home.name == "my-agent"


def test_name_override(tmp_dir):
    home = AgentHome(tmp_dir / "agents" / "dir", name="custom")
    assert home.name == "custom"
