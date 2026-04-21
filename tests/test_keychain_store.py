"""Tests for loom.store.keychain.KeychainStore (RFC 0002 Phase C).

Uses an in-process keyring backend (keyring.backend.KeyringBackend subclass)
to avoid touching the real OS keychain.  All tests use monkeypatching so the
keyring library dependency is used directly without real OS credential storage.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# In-memory keyring backend fixture
# ---------------------------------------------------------------------------


class InMemoryKeyring:
    """Minimal in-memory keyring backend compatible with the ``keyring`` API."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop((service, username), None)


@pytest.fixture
def mock_keyring(monkeypatch):
    """Replace the ``keyring`` module functions with an in-memory backend."""
    backend = InMemoryKeyring()

    import types
    fake_kr = types.ModuleType("keyring")
    fake_kr.get_password = backend.get_password  # type: ignore[attr-defined]
    fake_kr.set_password = backend.set_password  # type: ignore[attr-defined]
    fake_kr.delete_password = backend.delete_password  # type: ignore[attr-defined]

    import sys
    monkeypatch.setitem(sys.modules, "keyring", fake_kr)
    return backend


@pytest.fixture
def store(mock_keyring):
    """Return a KeychainStore backed by the in-memory keyring."""
    from loom.store.keychain import KeychainStore
    return KeychainStore(service="loom-test")


# ---------------------------------------------------------------------------
# put / get roundtrip
# ---------------------------------------------------------------------------


async def test_put_get_roundtrip(store) -> None:
    secret = {"type": "api_key", "value": "sk-test-123"}
    sid = await store.put("my-scope", secret)
    assert isinstance(sid, str) and len(sid) > 0

    result = await store.get("my-scope")
    assert result == secret


async def test_get_missing_scope_returns_none(store) -> None:
    result = await store.get("nonexistent")
    assert result is None


async def test_put_overwrites_existing(store) -> None:
    secret1 = {"type": "api_key", "value": "old"}
    secret2 = {"type": "api_key", "value": "new"}
    await store.put("scope-x", secret1)
    await store.put("scope-x", secret2)
    result = await store.get("scope-x")
    assert result == secret2


# ---------------------------------------------------------------------------
# metadata
# ---------------------------------------------------------------------------


async def test_put_stores_metadata(store) -> None:
    secret = {"type": "password", "value": "hunter2"}
    await store.put("meta-scope", secret, metadata={"env": "prod"})
    meta = await store.get_metadata("meta-scope")
    assert meta == {"env": "prod"}


async def test_get_metadata_missing_scope_returns_none(store) -> None:
    result = await store.get_metadata("missing")
    assert result is None


async def test_metadata_default_empty_dict(store) -> None:
    secret = {"type": "api_key", "value": "x"}
    await store.put("no-meta", secret)
    meta = await store.get_metadata("no-meta")
    assert meta == {}


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def test_list_returns_all_scopes(store) -> None:
    await store.put("scope-a", {"type": "api_key", "value": "a"})
    await store.put("scope-b", {"type": "api_key", "value": "b"})
    entries = await store.list()
    scopes = {e["scope"] for e in entries}
    assert "scope-a" in scopes
    assert "scope-b" in scopes


async def test_list_filters_by_prefix(store) -> None:
    await store.put("prod/svc1", {"type": "api_key", "value": "1"})
    await store.put("prod/svc2", {"type": "api_key", "value": "2"})
    await store.put("dev/svc3", {"type": "api_key", "value": "3"})
    entries = await store.list(scope_prefix="prod/")
    scopes = [e["scope"] for e in entries]
    assert "prod/svc1" in scopes
    assert "prod/svc2" in scopes
    assert "dev/svc3" not in scopes


async def test_list_empty_store(store) -> None:
    result = await store.list()
    assert result == []


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


async def test_revoke_removes_secret(store) -> None:
    await store.put("rev-scope", {"type": "api_key", "value": "bye"})
    result = await store.revoke("rev-scope")
    assert result is True
    assert await store.get("rev-scope") is None


async def test_revoke_missing_scope_returns_false(store) -> None:
    result = await store.revoke("not-there")
    assert result is False


async def test_revoke_removes_from_list(store) -> None:
    await store.put("to-revoke", {"type": "api_key", "value": "x"})
    await store.revoke("to-revoke")
    entries = await store.list()
    assert all(e["scope"] != "to-revoke" for e in entries)


# ---------------------------------------------------------------------------
# rotate
# ---------------------------------------------------------------------------


async def test_rotate_updates_secret(store) -> None:
    old = {"type": "api_key", "value": "old"}
    new = {"type": "api_key", "value": "new"}
    await store.put("rotate-scope", old)
    sid = await store.rotate("rotate-scope", new)
    assert isinstance(sid, str)
    result = await store.get("rotate-scope")
    assert result == new


async def test_rotate_bumps_version(store) -> None:
    await store.put("v-scope", {"type": "api_key", "value": "v1"})
    await store.rotate("v-scope", {"type": "api_key", "value": "v2"})
    entries = await store.list()
    entry = next(e for e in entries if e["scope"] == "v-scope")
    assert entry["version"] == 2


async def test_rotate_missing_scope_raises(store) -> None:
    with pytest.raises(KeyError):
        await store.rotate("missing", {"type": "api_key", "value": "x"})


# ---------------------------------------------------------------------------
# import guard
# ---------------------------------------------------------------------------


async def test_keychain_store_raises_without_keyring(monkeypatch) -> None:
    """Constructing KeychainStore with keyring absent raises ImportError."""
    import sys

    # Inject None sentinel so keyring import fails inside __init__.
    # We do NOT reload the keychain module — that would corrupt global state.
    monkeypatch.setitem(sys.modules, "keyring", None)  # type: ignore[arg-type]

    from loom.store.keychain import KeychainStore
    with pytest.raises(ImportError, match="keyring"):
        KeychainStore()
