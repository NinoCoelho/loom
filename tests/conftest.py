import tempfile
from pathlib import Path

import pytest

from loom.home import AgentHome


@pytest.fixture
def tmp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


@pytest.fixture
def agent_home(tmp_dir):
    home = AgentHome(tmp_dir / "agents" / "test", "test")
    home.initialize()
    return home


@pytest.fixture
def memory_dir(tmp_dir):
    d = tmp_dir / "memory"
    d.mkdir()
    return d


@pytest.fixture
def memory_index(tmp_dir):
    return tmp_dir / "mem_idx.sqlite"


@pytest.fixture
def perms_default():
    from loom.permissions import AgentPermissions

    return AgentPermissions()


@pytest.fixture
def perms_full():
    from loom.permissions import AgentPermissions

    return AgentPermissions(soul_writable=True, identity_writable=True, user_writable=True)
