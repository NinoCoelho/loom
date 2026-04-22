from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from loom.types import ChatMessage, ContentPart, Role, TextPart


class SessionStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._closed = False
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                context TEXT,
                pending_question TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                model TEXT,
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
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
        self._db.commit()
        # Idempotent migrations: add columns if missing (for pre-existing DBs)
        cols = {
            row[1] for row in self._db.execute("PRAGMA table_info(sessions)").fetchall()
        }
        migrations = {
            "context": "TEXT",
            "title": "TEXT",
            "updated_at": "TIMESTAMP",
            "model": "TEXT",
            "input_tokens": "INTEGER DEFAULT 0",
            "output_tokens": "INTEGER DEFAULT 0",
            "tool_call_count": "INTEGER DEFAULT 0",
        }
        for col_name, col_def in migrations.items():
            if col_name not in cols:
                self._db.execute(
                    f"ALTER TABLE sessions ADD COLUMN {col_name} {col_def}"
                )
        self._db.commit()

    def close(self) -> None:
        if self._closed:
            return
        self._db.close()
        self._closed = True

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def get_or_create(
        self, session_id: str, title: str | None = None, context: str | None = None
    ) -> dict[str, Any]:
        row = self._db.execute(
            "SELECT id, title, context, pending_question, model, "
            "input_tokens, output_tokens, tool_call_count "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row:
            return {
                "id": row[0],
                "title": row[1],
                "context": row[2],
                "pending_question": row[3],
                "model": row[4],
                "input_tokens": row[5],
                "output_tokens": row[6],
                "tool_call_count": row[7],
            }
        self._db.execute(
            "INSERT INTO sessions (id, title, context) VALUES (?, ?, ?)",
            (session_id, title, context),
        )
        self._db.commit()
        return {"id": session_id, "title": title, "context": context}

    def _deserialize_content(self, raw: str | None) -> str | list[ContentPart] | None:
        if raw is None:
            return None
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list) and parsed and isinstance(parsed[0], dict) and "type" in parsed[0]:
                    from pydantic import TypeAdapter

                    adapter = TypeAdapter(list[ContentPart])
                    return adapter.validate_python(parsed)
            except (json.JSONDecodeError, Exception):
                pass
        return raw

    def get_history(self, session_id: str) -> list[ChatMessage]:
        rows = self._db.execute(
            "SELECT role, content, tool_calls, tool_call_id, name "
            "FROM messages WHERE session_id = ? ORDER BY seq",
            (session_id,),
        ).fetchall()
        messages: list[ChatMessage] = []
        for row in rows:
            tool_calls = None
            if row[2]:
                tc_data = json.loads(row[2])
                from loom.types import ToolCall

                tool_calls = [ToolCall(**tc) for tc in tc_data]
            messages.append(
                ChatMessage(
                    role=Role(row[0]),
                    content=self._deserialize_content(row[1]),
                    tool_calls=tool_calls,
                    tool_call_id=row[3],
                    name=row[4],
                )
            )
        return messages

    def _serialize_content(self, content: str | list[ContentPart] | None) -> str | None:
        if content is None:
            return None
        if isinstance(content, str):
            return content
        return json.dumps([p.model_dump() for p in content])

    def replace_history(self, session_id: str, messages: list[ChatMessage]) -> None:
        self._db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        for seq, msg in enumerate(messages):
            tc_json = (
                json.dumps(
                    [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in msg.tool_calls
                    ]
                )
                if msg.tool_calls
                else None
            )
            self._db.execute(
                "INSERT INTO messages "
                "(session_id, seq, role, content, tool_calls, tool_call_id, name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    seq,
                    msg.role.value,
                    self._serialize_content(msg.content),
                    tc_json,
                    msg.tool_call_id,
                    msg.name,
                ),
            )
        self._db.execute(
            "UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (session_id,),
        )
        self._db.commit()

    def set_title(self, session_id: str, title: str) -> None:
        self._db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
        self._db.commit()

    def set_pending_question(self, session_id: str, question: str | None) -> None:
        self._db.execute(
            "UPDATE sessions SET pending_question = ? WHERE id = ?",
            (question, session_id),
        )
        self._db.commit()

    def bump_usage(
        self, session_id: str, input_tokens: int, output_tokens: int, tool_calls: int
    ) -> None:
        self._db.execute(
            "UPDATE sessions SET "
            "input_tokens = input_tokens + ?, "
            "output_tokens = output_tokens + ?, "
            "tool_call_count = tool_call_count + ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (input_tokens, output_tokens, tool_calls, session_id),
        )
        self._db.commit()

    def set_context(self, session_id: str, context: str | None) -> None:
        self._db.execute(
            "UPDATE sessions SET context = ? WHERE id = ?",
            (context, session_id),
        )
        self._db.commit()

    def reset(self, session_id: str) -> None:
        """Clear message history for a session; preserves title, context, and usage."""
        self.replace_history(session_id, [])
        self.set_pending_question(session_id, None)

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT id, title, context, created_at, updated_at, model, "
            "input_tokens, output_tokens, tool_call_count "
            "FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "context": r[2],
                "created_at": r[3],
                "updated_at": r[4],
                "model": r[5],
                "input_tokens": r[6],
                "output_tokens": r[7],
                "tool_call_count": r[8],
            }
            for r in rows
        ]

    def delete_session(self, session_id: str) -> bool:
        cursor = self._db.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT DISTINCT s.id, s.title, s.updated_at "
            "FROM sessions s JOIN messages m ON s.id = m.session_id "
            "WHERE m.content LIKE ? ORDER BY s.updated_at DESC LIMIT ?",
            (f"%{query}%", limit),
        ).fetchall()
        return [{"id": r[0], "title": r[1], "updated_at": r[2]} for r in rows]
