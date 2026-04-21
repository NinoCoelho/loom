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
async def test_recall_ranks_by_bm25(store):
    await store.write("go", "Alpha uses gRPC and Go", category="projects")
    await store.write("py", "Beta uses Python and REST", category="projects")
    hits = await store.recall("gRPC")
    assert hits
    assert hits[0].key == "go"
    assert 0.0 <= hits[0].score <= 1.5
    assert "bm25" in hits[0].components


@pytest.mark.asyncio
async def test_recall_pinned_boost(store):
    await store.write("a", "the cat sat on the mat", category="notes")
    await store.write("b", "the cat chased a mouse", category="notes")
    store.pin("b", True)
    hits = await store.recall("cat")
    # pinned entry should win when BM25 is close
    assert hits[0].key == "b"


@pytest.mark.asyncio
async def test_recall_budget_truncates(store):
    for i in range(5):
        await store.write(f"k{i}", "python " + ("x" * 200), category="notes")
    hits = await store.recall("python", limit=5, budget=400)
    total = sum(len(h.preview) for h in hits)
    assert total <= 400


@pytest.mark.asyncio
async def test_touch_bumps_access_count(store):
    await store.write("t", "find me via query", category="notes")
    await store.recall("query", touch=True)
    entry = await store.read("t")
    assert entry.access_count >= 1
    assert entry.last_recalled_at is not None


@pytest.mark.asyncio
async def test_recall_no_touch_opt_out(store):
    await store.write("t", "untouched content", category="notes")
    await store.recall("untouched", touch=False)
    entry = await store.read("t")
    assert entry.access_count == 0


@pytest.mark.asyncio
async def test_importance_clamped(store):
    await store.write("i", "stuff", category="notes")
    store.set_importance("i", 99)
    entry = await store.read("i")
    assert entry.importance == 3
    store.set_importance("i", -5)
    entry = await store.read("i")
    assert entry.importance == 0


@pytest.mark.asyncio
async def test_salience_columns_migrate(tmp_path):
    """Pre-existing DB without salience columns should be migrated on open."""
    import sqlite3 as _sq
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    db = mem_dir / "_index.sqlite"
    conn = _sq.connect(str(db))
    conn.execute(
        "CREATE TABLE memory_meta (key TEXT PRIMARY KEY, category TEXT, "
        "tags TEXT, created TEXT, updated TEXT)"
    )
    conn.commit()
    conn.close()
    # Opening the store should add the missing columns without error.
    store = MemoryStore(mem_dir, db)
    await store.write("k", "hello", category="notes")
    entry = await store.read("k")
    assert entry.pinned is False
    assert entry.importance == 1


@pytest.mark.asyncio
async def test_persistence(memory_dir, memory_index):
    store1 = MemoryStore(memory_dir, memory_index)
    await store1.write("persist", "survives restart", category="notes")
    store2 = MemoryStore(memory_dir, memory_index)
    entry = await store2.read("persist")
    assert entry is not None
    assert "survives restart" in entry.content
