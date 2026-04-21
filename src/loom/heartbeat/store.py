from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loom.heartbeat.types import HeartbeatRunRecord

_TS_FMT = "%Y-%m-%dT%H:%M:%S.%f"


def _to_ts(dt: datetime | None) -> str | None:
    return dt.strftime(_TS_FMT) if dt else None


def _from_ts(s: str | None) -> datetime | None:
    return datetime.strptime(s, _TS_FMT) if s else None


class HeartbeatStore:
    """Persists per-heartbeat runtime state: driver state dict, timing, errors.

    Keyed by (heartbeat_id, instance_id) so the same driver package can run
    as multiple independent instances without sharing state.
    """

    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._closed = False
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS heartbeat_state (
                heartbeat_id  TEXT NOT NULL,
                instance_id   TEXT NOT NULL DEFAULT 'default',
                state         TEXT NOT NULL DEFAULT '{}',
                last_check    TEXT,
                last_fired    TEXT,
                last_error    TEXT,
                PRIMARY KEY (heartbeat_id, instance_id)
            )
        """)
        self._db.commit()

    def get_run(
        self, heartbeat_id: str, instance_id: str = "default"
    ) -> HeartbeatRunRecord | None:
        row = self._db.execute(
            "SELECT state, last_check, last_fired, last_error "
            "FROM heartbeat_state WHERE heartbeat_id=? AND instance_id=?",
            (heartbeat_id, instance_id),
        ).fetchone()
        if not row:
            return None
        return HeartbeatRunRecord(
            heartbeat_id=heartbeat_id,
            instance_id=instance_id,
            state=json.loads(row[0]),
            last_check=_from_ts(row[1]),
            last_fired=_from_ts(row[2]),
            last_error=row[3],
        )

    def get_state(self, heartbeat_id: str, instance_id: str = "default") -> dict[str, Any]:
        run = self.get_run(heartbeat_id, instance_id)
        return run.state if run else {}

    def set_state(
        self,
        heartbeat_id: str,
        state: dict[str, Any],
        instance_id: str = "default",
    ) -> None:
        self._upsert(heartbeat_id, instance_id, state=json.dumps(state))

    def touch_check(self, heartbeat_id: str, instance_id: str = "default") -> None:
        self._upsert(heartbeat_id, instance_id, last_check=_to_ts(datetime.now(UTC)))

    def touch_fired(
        self,
        heartbeat_id: str,
        instance_id: str = "default",
        error: str | None = None,
    ) -> None:
        now = _to_ts(datetime.now(UTC))
        self._upsert(heartbeat_id, instance_id, last_fired=now, last_error=error)

    def list_runs(self) -> list[HeartbeatRunRecord]:
        rows = self._db.execute(
            "SELECT heartbeat_id, instance_id, state, last_check, last_fired, last_error "
            "FROM heartbeat_state ORDER BY heartbeat_id, instance_id"
        ).fetchall()
        return [
            HeartbeatRunRecord(
                heartbeat_id=r[0],
                instance_id=r[1],
                state=json.loads(r[2]),
                last_check=_from_ts(r[3]),
                last_fired=_from_ts(r[4]),
                last_error=r[5],
            )
            for r in rows
        ]

    def delete(self, heartbeat_id: str, instance_id: str = "default") -> None:
        self._db.execute(
            "DELETE FROM heartbeat_state WHERE heartbeat_id=? AND instance_id=?",
            (heartbeat_id, instance_id),
        )
        self._db.commit()

    def close(self) -> None:
        if self._closed:
            return
        self._db.close()
        self._closed = True

    def __enter__(self) -> HeartbeatStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def delete_all(self, heartbeat_id: str) -> None:
        self._db.execute(
            "DELETE FROM heartbeat_state WHERE heartbeat_id=?",
            (heartbeat_id,),
        )
        self._db.commit()

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _upsert(
        self,
        heartbeat_id: str,
        instance_id: str,
        **updates: Any,
    ) -> None:
        existing = self._db.execute(
            "SELECT 1 FROM heartbeat_state WHERE heartbeat_id=? AND instance_id=?",
            (heartbeat_id, instance_id),
        ).fetchone()

        if not existing:
            self._db.execute(
                "INSERT INTO heartbeat_state (heartbeat_id, instance_id) VALUES (?, ?)",
                (heartbeat_id, instance_id),
            )

        for col, val in updates.items():
            self._db.execute(
                f"UPDATE heartbeat_state SET {col}=? "  # noqa: S608 — col is internal
                "WHERE heartbeat_id=? AND instance_id=?",
                (val, heartbeat_id, instance_id),
            )
        self._db.commit()
