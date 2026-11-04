"""Persistent bot checkpoints and at-most-once claims for external effects.

The business access records remain owned by AccessService. This database stores
only delivery state. Each operation has its own SQLite connection, so the Flask
thread, bot threads, and local worker can share it safely.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class BotStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bot_claims (
                    namespace TEXT NOT NULL,
                    event_key TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'claimed',
                    error_code TEXT,
                    completed_at REAL,
                    PRIMARY KEY (namespace, event_key)
                );
                CREATE TABLE IF NOT EXISTS bot_checkpoints (
                    namespace TEXT NOT NULL,
                    checkpoint_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (namespace, checkpoint_key)
                );
            """)
            # Keep delivery state from earlier local builds. The immediate
            # transaction serializes startup migrations between the two bots.
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(bot_claims)")}
            for name, definition in (
                ("outcome", "TEXT NOT NULL DEFAULT 'claimed'"),
                ("error_code", "TEXT"), ("completed_at", "REAL"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE bot_claims ADD COLUMN {name} {definition}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def claim(self, namespace: str, event_key: str) -> bool:
        """Claim before effects; a crash never automatically repeats them.

        If a process stops after the claim but before dialing, that command is
        deliberately not retried automatically. MAX also claims nonphysical
        updates before replies or binding. The customer can act again, producing
        a new event key; the domain service rechecks access/cooldown.
        """
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute(
                "INSERT OR IGNORE INTO bot_claims(namespace,event_key,created_at) VALUES (?,?,?)",
                (namespace, event_key, time.time()),
            )
            return cursor.rowcount == 1

    def seen(self, namespace: str, event_key: str) -> bool:
        with self._connect() as db:
            return db.execute(
                "SELECT 1 FROM bot_claims WHERE namespace=? AND event_key=?",
                (namespace, event_key),
            ).fetchone() is not None

    def finish(self, namespace: str, event_key: str, outcome: str, error_code: str | None = None) -> None:
        if outcome not in {"handled", "failed", "unknown", "cancelled"}:
            raise ValueError("Invalid bot event outcome")
        if error_code is not None and not re.fullmatch(r"[A-Za-z0-9_.]{1,100}", error_code):
            error_code = "unclassified_error"
        with self._connect() as db:
            db.execute(
                "UPDATE bot_claims SET outcome=?,error_code=?,completed_at=? WHERE namespace=? AND event_key=?",
                (outcome, error_code, time.time(), namespace, event_key),
            )

    def event_state(self, namespace: str, event_key: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT outcome,error_code,created_at,completed_at FROM bot_claims WHERE namespace=? AND event_key=?",
                (namespace, event_key),
            ).fetchone()
        return dict(zip(("outcome", "error_code", "created_at", "completed_at"), row)) if row else None

    def get_checkpoint(self, namespace: str, key: str, default: Any = None) -> Any:
        with self._connect() as db:
            row = db.execute(
                "SELECT value FROM bot_checkpoints WHERE namespace=? AND checkpoint_key=?",
                (namespace, key),
            ).fetchone()
        return json.loads(row[0]) if row is not None else default

    def set_checkpoint(self, namespace: str, key: str, value: Any) -> None:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        with self._connect() as db:
            db.execute(
                "INSERT INTO bot_checkpoints(namespace,checkpoint_key,value) VALUES (?,?,?) "
                "ON CONFLICT(namespace,checkpoint_key) DO UPDATE SET value=excluded.value",
                (namespace, key, encoded),
            )
