from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS reports (
                code INTEGER NOT NULL,
                report_id TEXT NOT NULL,
                received_at REAL NOT NULL,
                payload TEXT NOT NULL,
                event_key TEXT,
                PRIMARY KEY (code, report_id)
            );
            CREATE TABLE IF NOT EXISTS events (
                event_key TEXT PRIMARY KEY,
                code INTEGER NOT NULL,
                event_time TEXT,
                state TEXT NOT NULL,
                message_id TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_serial INTEGER
            );
            CREATE INDEX IF NOT EXISTS events_lookup ON events(code, event_time);
            """
        )
        self.connection.commit()

    def report_seen(self, code: int, report_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM reports WHERE code=? AND report_id=?", (code, report_id)
        ).fetchone() is not None

    def report_event_key(self, code: int, report_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT event_key FROM reports WHERE code=? AND report_id=?", (code, report_id)
        ).fetchone()
        return str(row["event_key"]) if row and row["event_key"] else None

    def report_payload(self, code: int, report_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT payload FROM reports WHERE code=? AND report_id=?", (code, report_id)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def add_report(self, code: int, report_id: str, payload: dict[str, Any], event_key: str | None) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO reports(code, report_id, received_at, payload, event_key) VALUES(?,?,?,?,?)",
            (code, report_id, time.time(), json.dumps(payload, ensure_ascii=False), event_key),
        )
        self.connection.commit()

    def get_event(self, event_key: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM events WHERE event_key=?", (event_key,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["state"] = json.loads(result["state"])
        return result

    def events_for_time(self, code: int, event_time: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE code=? AND event_time=?", (code, event_time)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["state"] = json.loads(item["state"])
            result.append(item)
        return result

    def new_event_key(self, code: int, event_time: str | None) -> str:
        return f"{code}:{event_time or 'unknown'}:{uuid.uuid4().hex}"

    def save_event(
        self,
        event_key: str,
        code: int,
        event_time: str | None,
        state: dict[str, Any],
        message_id: str | None,
        last_serial: int | None = None,
    ) -> None:
        now = time.time()
        self.connection.execute(
            """
            INSERT INTO events(event_key, code, event_time, state, message_id, created_at, updated_at, last_serial)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(event_key) DO UPDATE SET
                event_time=excluded.event_time,
                state=excluded.state,
                message_id=COALESCE(excluded.message_id, events.message_id),
                updated_at=excluded.updated_at,
                last_serial=COALESCE(excluded.last_serial, events.last_serial)
            """,
            (event_key, code, event_time, json.dumps(state, ensure_ascii=False), message_id, now, now, last_serial),
        )
        self.connection.commit()

    def set_message_id(self, event_key: str, message_id: str) -> None:
        self.connection.execute(
            "UPDATE events SET message_id=?, updated_at=? WHERE event_key=?",
            (message_id, time.time(), event_key),
        )
        self.connection.commit()

    def mark_report(self, code: int, report_id: str, payload: dict[str, Any], event_key: str) -> None:
        self.add_report(code, report_id, payload, event_key)

    def prune(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        with self.connection:
            self.connection.execute("DELETE FROM reports WHERE received_at < ?", (cutoff,))
            result = self.connection.execute("DELETE FROM events WHERE updated_at < ?", (cutoff,))
        return result.rowcount

    def close(self) -> None:
        self.connection.close()
