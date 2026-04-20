from loom.permissions import AgentPermissions


def test_defaults():
    p = AgentPermissions()
    assert not p.soul_writable
    assert not p.identity_writable
    assert p.user_writable
    assert p.skills_creatable
    assert p.memory_writable
    assert not p.terminal_allowed
    assert p.delegate_allowed


def test_can_edit_file():
    p = AgentPermissions(soul_writable=True, user_writable=True)
    assert p.can_edit_file("soul")
    assert p.can_edit_file("SOUL")
    assert p.can_edit_file("SOUL.MD")
    assert p.can_edit_file("user")
    assert not p.can_edit_file("identity")
    assert not p.can_edit_file("IDENTITY")


def test_custom_permissions():
    p = AgentPermissions(
        soul_writable=True,
        identity_writable=True,
        user_writable=False,
        terminal_allowed=True,
        skills_deletable=True,
    )
    assert p.soul_writable
    assert p.identity_writable
    assert not p.user_writable
    assert p.terminal_allowed
    assert p.skills_deletable
