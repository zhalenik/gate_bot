"""Concurrent runtime bootstrap must work before SQLite has entered WAL mode."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from bot_state import BotStateStore
from runtime_state import RuntimeState


def test_concurrent_runtime_bootstrap_preserves_all_outbox_entries(tmp_path):
    path = tmp_path / "runtime.sqlite3"

    def enqueue(index):
        state = RuntimeState(path)
        return state.enqueue("max", {"token": f"test-pass-{index}", "event": "granted"})

    with ThreadPoolExecutor(max_workers=8) as executor:
        identifiers = list(executor.map(enqueue, range(20)))

    state = RuntimeState(path)
    assert len(set(identifiers)) == 20
    assert state.notification_counts() == {"pending": 20}
    with state.connect() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


@pytest.mark.parametrize("store_class", [BotStateStore, RuntimeState])
def test_reopening_wal_store_does_not_request_mode_transition(tmp_path, monkeypatch, store_class):
    path = tmp_path / "state.sqlite3"
    store_class(path)
    statements = []
    connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        db = connect(*args, **kwargs)
        db.set_trace_callback(statements.append)
        return db

    monkeypatch.setattr(sqlite3, "connect", traced_connect)
    store_class(path)
    assert any(sql.strip().upper() == "PRAGMA JOURNAL_MODE" for sql in statements)
    assert not any(sql.replace(" ", "").upper().startswith("PRAGMAJOURNAL_MODE=") for sql in statements)
