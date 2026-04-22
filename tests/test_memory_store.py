import re
from datetime import datetime

import pytest

from loom.store.memory import MemoryStore
from loom.store.vault import FilesystemVaultProvider


@pytest.fixture
def store(memory_dir, memory_index):
    memory_store = MemoryStore(memory_dir, memory_index)
    yield memory_store
    memory_store.close()


@pytest.fixture
def vault_dir(tmp_dir):
    d = tmp_dir / "vault"
    d.mkdir()
    return d


@pytest.fixture
def vault_provider(vault_dir):
    vp = FilesystemVaultProvider(vault_dir)
    yield vp
    vp.close()


@pytest.fixture
def store_with_vault(vault_provider, tmp_dir):
    memory_dir = tmp_dir / "memory_standalone"
    memory_dir.mkdir()
    ms = MemoryStore(
        memory_dir,
        tmp_dir / "mem_idx.sqlite",
        vault_provider=vault_provider,
        vault_prefix="memory",
    )
    yield ms
    ms.close()


_DATE_DIR_RE = re.compile(r"\d{4}/\d{2}/\d{2}")


@pytest.mark.asyncio
async def test_write_and_read(store):
    await store.write("test-key", "Hello world", category="notes", tags=["test"])
    entry = await store.read("test-key")
    assert entry is not None
    assert entry.content == "Hello world"
    assert entry.category == "notes"
    assert "test" in entry.tags


@pytest.mark.asyncio
async def test_write_persists_utc_timestamps(store):
    await store.write("tz-key", "Hello UTC", category="notes")
    entry = await store.read("tz-key")
    assert entry is not None
    assert datetime.fromisoformat(entry.created).tzinfo is not None
    assert datetime.fromisoformat(entry.updated).tzinfo is not None


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
    store = MemoryStore(mem_dir, db)
    try:
        await store.write("k", "hello", category="notes")
        entry = await store.read("k")
        assert entry.pinned is False
        assert entry.importance == 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_persistence(memory_dir, memory_index):
    store1 = MemoryStore(memory_dir, memory_index)
    try:
        await store1.write("persist", "survives restart", category="notes")
    finally:
        store1.close()

    store2 = MemoryStore(memory_dir, memory_index)
    try:
        entry = await store2.read("persist")
        assert entry is not None
        assert "survives restart" in entry.content
    finally:
        store2.close()


# ── Vault-backed tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_vault_write_and_read(store_with_vault):
    await store_with_vault.write("test-key", "Hello vault", category="notes", tags=["vault"])
    entry = await store_with_vault.read("test-key")
    assert entry is not None
    assert entry.content == "Hello vault"
    assert entry.category == "notes"
    assert "vault" in entry.tags


@pytest.mark.asyncio
async def test_vault_write_creates_date_dir_file(vault_dir, store_with_vault):
    await store_with_vault.write("my-note", "Some content", category="notes")
    now = datetime.now()
    date_dir = vault_dir / "memory" / f"{now.year:04d}" / f"{now.month:02d}" / f"{now.day:02d}"
    path = date_dir / "my-note.md"
    assert path.exists()
    raw = path.read_text()
    assert "Some content" in raw
    assert raw.startswith("---")


@pytest.mark.asyncio
async def test_vault_write_creates_no_flat_file(vault_dir, store_with_vault):
    await store_with_vault.write("my-note", "Some content", category="notes")
    flat_path = vault_dir / "memory" / "my-note.md"
    assert not flat_path.exists()


@pytest.mark.asyncio
async def test_vault_date_dir_matches_created_date(vault_dir, store_with_vault):
    await store_with_vault.write("dated", "content", category="notes")
    entry = await store_with_vault.read("dated")
    assert entry is not None
    created = datetime.fromisoformat(entry.created)
    date_dir = vault_dir / "memory" / f"{created.year:04d}" / f"{created.month:02d}" / f"{created.day:02d}"
    assert (date_dir / "dated.md").exists()


@pytest.mark.asyncio
async def test_vault_overwrite_stays_in_original_dir(vault_dir, store_with_vault):
    await store_with_vault.write("stable", "version 1", category="notes")
    entry1 = await store_with_vault.read("stable")
    created1 = datetime.fromisoformat(entry1.created)
    date_dir1 = vault_dir / "memory" / f"{created1.year:04d}" / f"{created1.month:02d}" / f"{created1.day:02d}"

    await store_with_vault.write("stable", "version 2", category="notes")
    entry2 = await store_with_vault.read("stable")
    assert entry2.content == "version 2"

    assert (date_dir1 / "stable.md").exists()


@pytest.mark.asyncio
async def test_vault_vault_path_stored_in_db(store_with_vault):
    await store_with_vault.write("path-test", "content", category="notes")
    row = store_with_vault._db.execute(
        "SELECT vault_path FROM memory_meta WHERE key = ?", ("path-test",)
    ).fetchone()
    assert row is not None
    assert row[0] is not None
    assert _DATE_DIR_RE.search(row[0]) is not None


@pytest.mark.asyncio
async def test_vault_read_nonexistent(store_with_vault):
    entry = await store_with_vault.read("nonexistent")
    assert entry is None


@pytest.mark.asyncio
async def test_vault_delete(store_with_vault, vault_dir):
    await store_with_vault.write("to-delete", "temporary", category="notes")
    assert await store_with_vault.read("to-delete") is not None
    assert await store_with_vault.delete("to-delete") is True
    assert await store_with_vault.read("to-delete") is None


@pytest.mark.asyncio
async def test_vault_delete_nonexistent(store_with_vault):
    assert await store_with_vault.delete("nonexistent") is False


@pytest.mark.asyncio
async def test_vault_overwrite(store_with_vault):
    await store_with_vault.write("key", "version 1", category="notes")
    await store_with_vault.write("key", "version 2", category="notes")
    entry = await store_with_vault.read("key")
    assert entry.content == "version 2"


@pytest.mark.asyncio
async def test_vault_search_scoped(store_with_vault):
    await store_with_vault.write("alpha", "Alpha uses gRPC and Go", category="projects")
    await store_with_vault.write("beta", "Beta uses Python and REST", category="projects")
    hits = await store_with_vault.search("gRPC")
    assert len(hits) >= 1
    assert any(h.key == "alpha" for h in hits)


@pytest.mark.asyncio
async def test_vault_recall(store_with_vault):
    await store_with_vault.write("go", "Alpha uses gRPC and Go", category="projects")
    await store_with_vault.write("py", "Beta uses Python and REST", category="projects")
    hits = await store_with_vault.recall("gRPC")
    assert hits
    assert hits[0].key == "go"


@pytest.mark.asyncio
async def test_vault_recent(store_with_vault):
    await store_with_vault.write("r1", "Recent entry 1", category="notes")
    await store_with_vault.write("r2", "Recent entry 2", category="notes")
    recent = store_with_vault.recent(limit=2)
    assert len(recent) <= 2


@pytest.mark.asyncio
async def test_vault_touch_bumps_access_count(store_with_vault):
    await store_with_vault.write("t", "find me via query", category="notes")
    await store_with_vault.recall("query", touch=True)
    entry = await store_with_vault.read("t")
    assert entry is not None
    assert entry.access_count >= 1
    assert entry.last_recalled_at is not None


@pytest.mark.asyncio
async def test_vault_importance(store_with_vault):
    await store_with_vault.write("i", "stuff", category="notes")
    store_with_vault.set_importance("i", 99)
    entry = await store_with_vault.read("i")
    assert entry.importance == 3


@pytest.mark.asyncio
async def test_vault_pin(store_with_vault):
    await store_with_vault.write("p", "pin me", category="notes")
    store_with_vault.pin("p", True)
    entry = await store_with_vault.read("p")
    assert entry.pinned is True


@pytest.mark.asyncio
async def test_vault_pin_nonexistent(store_with_vault):
    store_with_vault.pin("nonexistent", True)


@pytest.mark.asyncio
async def test_vault_touch_nonexistent(store_with_vault):
    store_with_vault.touch("nonexistent")


@pytest.mark.asyncio
async def test_vault_set_importance_nonexistent(store_with_vault):
    store_with_vault.set_importance("nonexistent", 2)


@pytest.mark.asyncio
async def test_vault_recent_empty(store_with_vault):
    recent = store_with_vault.recent(limit=5)
    assert recent == []


@pytest.mark.asyncio
async def test_vault_recall_empty(store_with_vault):
    hits = await store_with_vault.recall("nothing")
    assert hits == []


@pytest.mark.asyncio
async def test_vault_list_by_category(store_with_vault):
    await store_with_vault.write("a", "note content", category="notes")
    await store_with_vault.write("b", "project content", category="projects")
    notes = await store_with_vault.list_entries(category="notes")
    assert all(e.category == "notes" for e in notes)


@pytest.mark.asyncio
async def test_vault_pin_syncs_sqlite(store_with_vault):
    await store_with_vault.write("x", "sync test", category="notes")
    store_with_vault.pin("x", True)
    entry = await store_with_vault.read("x")
    assert entry.pinned is True
    store_with_vault.pin("x", False)
    entry = await store_with_vault.read("x")
    assert entry.pinned is False


@pytest.mark.asyncio
async def test_vault_importance_syncs_sqlite(store_with_vault):
    await store_with_vault.write("y", "imp test", category="notes")
    store_with_vault.set_importance("y", 3)
    entry = await store_with_vault.read("y")
    assert entry.importance == 3
    store_with_vault.set_importance("y", 0)
    entry = await store_with_vault.read("y")
    assert entry.importance == 0


class FakeEmbedder:
    dim = 4

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[len(t) * 0.01, 0.5, 0.3, 0.2] for t in texts]


@pytest.fixture
def store_with_graphrag(tmp_dir, vault_dir):
    from loom.store.graphrag import GraphRAGConfig, GraphRAGEngine

    vp = FilesystemVaultProvider(vault_dir)
    engine = GraphRAGEngine(
        GraphRAGConfig(chunk_size=500, top_k=5),
        FakeEmbedder(),
        db_dir=tmp_dir / "graphrag",
    )
    memory_dir = tmp_dir / "memory_standalone"
    memory_dir.mkdir()
    ms = MemoryStore(
        memory_dir,
        tmp_dir / "mem_idx.sqlite",
        vault_provider=vp,
        vault_prefix="memory",
        graphrag=engine,
    )
    yield ms
    engine.close()
    vp.close()
    ms.close()


@pytest.mark.asyncio
async def test_graphrag_indexes_on_write(store_with_graphrag):
    await store_with_graphrag.write("gr-test", "Hello graphrag world", category="notes")
    chunks = store_with_graphrag._graphrag._chunk_db.execute(
        "SELECT id FROM chunks WHERE source_path LIKE ?", ("%gr-test%",)
    ).fetchall()
    assert len(chunks) > 0


@pytest.mark.asyncio
async def test_graphrag_removes_on_delete(store_with_graphrag):
    await store_with_graphrag.write("gr-del", "to be deleted from graphrag", category="notes")
    chunks_before = store_with_graphrag._graphrag._chunk_db.execute(
        "SELECT id FROM chunks WHERE source_path LIKE ?", ("%gr-del%",)
    ).fetchall()
    assert len(chunks_before) > 0
    await store_with_graphrag.delete("gr-del")
    chunks_after = store_with_graphrag._graphrag._chunk_db.execute(
        "SELECT id FROM chunks WHERE source_path LIKE ?", ("%gr-del%",)
    ).fetchall()
    assert len(chunks_after) == 0


@pytest.mark.asyncio
async def test_graphrag_indexes_standalone(tmp_dir):
    from loom.store.graphrag import GraphRAGConfig, GraphRAGEngine

    engine = GraphRAGEngine(
        GraphRAGConfig(chunk_size=500, top_k=5),
        FakeEmbedder(),
        db_dir=tmp_dir / "graphrag",
    )
    memory_dir = tmp_dir / "mem"
    memory_dir.mkdir()
    ms = MemoryStore(
        memory_dir,
        tmp_dir / "mem_idx.sqlite",
        graphrag=engine,
    )
    try:
        await ms.write("standalone", "standalone graphrag content", category="notes")
        chunks = engine._chunk_db.execute(
            "SELECT id FROM chunks WHERE source_path = ?", ("standalone",)
        ).fetchall()
        assert len(chunks) > 0
        await ms.delete("standalone")
        chunks_after = engine._chunk_db.execute(
            "SELECT id FROM chunks WHERE source_path = ?", ("standalone",)
        ).fetchall()
        assert len(chunks_after) == 0
    finally:
        ms.close()
        engine.close()


@pytest.mark.asyncio
async def test_vault_reindex_populates_vault_path(vault_dir, store_with_vault):
    await store_with_vault.write("reidx", "reindex content", category="notes")
    store_with_vault.reindex_all()
    row = store_with_vault._db.execute(
        "SELECT vault_path FROM memory_meta WHERE key = ?", ("reidx",)
    ).fetchone()
    assert row is not None
    assert row[0] is not None
    assert _DATE_DIR_RE.search(row[0]) is not None


@pytest.mark.asyncio
async def test_vault_reindex_restores_read(vault_dir, store_with_vault):
    await store_with_vault.write("reidx-r", "reindex read test", category="notes")
    store_with_vault.reindex_all()
    entry = await store_with_vault.read("reidx-r")
    assert entry is not None
    assert "reindex read test" in entry.content
