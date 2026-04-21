import sqlite3

import pytest

from loom.store.session import SessionStore
from loom.types import ChatMessage, Role


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "sessions.sqlite"


@pytest.fixture
def store(db_path):
    session_store = SessionStore(db_path)
    yield session_store
    session_store.close()


def test_get_or_create_with_context(store):
    sid = "test-ctx-1"
    result = store.get_or_create(sid, title="My session", context="You are a helpful assistant.")
    assert result["id"] == sid
    assert result["context"] == "You are a helpful assistant."

    # Re-fetch and verify persistence
    fetched = store.get_or_create(sid)
    assert fetched["context"] == "You are a helpful assistant."


def test_set_context_updates(store):
    sid = "test-ctx-2"
    store.get_or_create(sid, title="Session")
    store.set_context(sid, "New system prompt context.")
    fetched = store.get_or_create(sid)
    assert fetched["context"] == "New system prompt context."

    # Clear context
    store.set_context(sid, None)
    fetched = store.get_or_create(sid)
    assert fetched["context"] is None


def test_reset_clears_history_not_identity(store):
    sid = "test-reset-1"
    store.get_or_create(sid, title="Preserved title", context="Preserved context")
    store.bump_usage(sid, input_tokens=10, output_tokens=5, tool_calls=1)

    messages = [
        ChatMessage(role=Role.USER, content="Hello"),
        ChatMessage(role=Role.ASSISTANT, content="Hi there"),
    ]
    store.replace_history(sid, messages)
    assert len(store.get_history(sid)) == 2

    store.reset(sid)

    # History must be empty
    assert store.get_history(sid) == []

    # Identity fields preserved
    fetched = store.get_or_create(sid)
    assert fetched["title"] == "Preserved title"
    assert fetched["context"] == "Preserved context"
    # Usage counters are preserved (reset only clears history)
    assert fetched["input_tokens"] == 10
    assert fetched["output_tokens"] == 5
    assert fetched["tool_call_count"] == 1


def test_context_column_migration(tmp_path):
    """Open store against a legacy DB and verify missing context is migrated."""
    db_file = tmp_path / "legacy.sqlite"

    # Create a DB without the context column
    conn = sqlite3.connect(str(db_file))
    conn.execute("""
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            pending_question TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            model TEXT,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE messages (
            session_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls TEXT,
            tool_call_id TEXT,
            name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (session_id, seq),
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)
    conn.execute("INSERT INTO sessions (id, title) VALUES ('legacy-sid', 'Old session')")
    conn.commit()
    conn.close()

    # Opening the store must migrate without error
    store = SessionStore(db_file)
    try:
        result = store.get_or_create("legacy-sid")
        assert result["context"] is None

        # Can also create new sessions with context
        new = store.get_or_create("new-sid", context="injected context")
        assert new["context"] == "injected context"
    finally:
        store.close()


def test_list_sessions_includes_context(store):
    store.get_or_create("s1", title="First", context="ctx1")
    store.get_or_create("s2", title="Second")
    sessions = store.list_sessions()
    by_id = {s["id"]: s for s in sessions}
    assert by_id["s1"]["context"] == "ctx1"
    assert by_id["s2"]["context"] is None
