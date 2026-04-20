import asyncio

import pytest

from loom.store.memory import MemoryStore


@pytest.fixture
def store(memory_dir, memory_index):
    return MemoryStore(memory_dir, memory_index)


@pytest.mark.asyncio
async def test_write_and_read(store):
    await store.write("test-key", "Hello world", category="notes", tags=["test"])
    entry = await store.read("test-key")
    assert entry is not None
    assert entry.content == "Hello world"
    assert entry.category == "notes"
    assert "test" in entry.tags


@pytest.mark.asyncio
async def test_read_nonexistent(store):
    entry = await store.read("nonexistent")
    assert entry is None


@pytest.mark.asyncio
async def test_search(store):
    await store.write("alpha", "Alpha uses gRPC and Go", category="projects", tags=["go"])
    await store.write("beta", "Beta uses Python and REST", category="projects", tags=["python"])
    hits = await store.search("gRPC")
    assert len(hits) >= 1
    assert any(h.key == "alpha" for h in hits)


@pytest.mark.asyncio
async def test_list_entries(store):
    await store.write("a", "Content A", category="notes")
    await store.write("b", "Content B", category="projects")
    entries = await store.list_entries()
    assert len(entries) >= 2


@pytest.mark.asyncio
async def test_list_by_category(store):
    await store.write("x", "Note X", category="notes")
    await store.write("y", "Project Y", category="projects")
    notes = await store.list_entries(category="notes")
    assert all(e.category == "notes" for e in notes)


@pytest.mark.asyncio
async def test_delete(store):
    await store.write("to-delete", "temporary", category="notes")
    assert await store.read("to-delete") is not None
    assert await store.delete("to-delete") is True
    assert await store.read("to-delete") is None


@pytest.mark.asyncio
async def test_delete_nonexistent(store):
    assert await store.delete("nonexistent") is False


@pytest.mark.asyncio
async def test_overwrite(store):
    await store.write("key", "version 1", category="notes")
    await store.write("key", "version 2", category="notes")
    entry = await store.read("key")
    assert entry.content == "version 2"


@pytest.mark.asyncio
async def test_recent(store):
    await store.write("r1", "Recent entry 1", category="notes")
    await store.write("r2", "Recent entry 2", category="notes")
    await store.write("r3", "Recent entry 3", category="notes")
    recent = store.recent(limit=2)
    assert len(recent) <= 2


@pytest.mark.asyncio
async def test_invalid_key(store):
    with pytest.raises(ValueError):
        await store.write("../escape", "hack", category="notes")


@pytest.mark.asyncio
async def test_persistence(memory_dir, memory_index):
    store1 = MemoryStore(memory_dir, memory_index)
    await store1.write("persist", "survives restart", category="notes")
    store2 = MemoryStore(memory_dir, memory_index)
    entry = await store2.read("persist")
    assert entry is not None
    assert "survives restart" in entry.content
