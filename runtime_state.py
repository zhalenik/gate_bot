"""Durable runtime bookkeeping, separate from access permissions."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import uuid

from filelock import FileLock


class RuntimeState:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # Serialize first-time WAL/bootstrap across threads and processes.
        # Existing WAL databases must not request another mode transition
        # while another runtime instance is already reading or writing.
        with FileLock(self.path + '.init.lock', timeout=30), self.connect() as db:
            if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS cooldown (key TEXT PRIMARY KEY, expires REAL NOT NULL, claim TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, channel TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT "pending", created REAL NOT NULL, updated REAL NOT NULL, error TEXT)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def acquire_cooldown(self, key, seconds):
        stamp = time.time()
        claim = uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT expires FROM cooldown WHERE key=?', (key,)).fetchone()
            if row and row['expires'] > stamp:
                return None, max(1, int(row['expires'] - stamp + 0.999))
            db.execute('INSERT INTO cooldown(key,expires,claim) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET expires=excluded.expires,claim=excluded.claim', (key, stamp + seconds, claim))
            db.execute('DELETE FROM cooldown WHERE expires < ?', (stamp - 86400,))
        return claim, 0

    def release_cooldown(self, key, claim):
        with self.connect() as db:
            db.execute('DELETE FROM cooldown WHERE key=? AND claim=?', (key, claim))

    def enqueue(self, channel, payload):
        # One transition notification per channel, token, target and expiry.
        material = json.dumps([channel, payload.get('token'), payload.get('event'), payload.get('access_expires'), payload.get('tg_id'), payload.get('max_id'), payload.get('access_revision', 0)], ensure_ascii=False)
        identifier = hashlib.sha256(material.encode()).hexdigest()
        stamp = time.time()
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO outbox(id,channel,payload,created,updated) VALUES(?,?,?,?,?)', (identifier, channel, json.dumps(payload, ensure_ascii=False), stamp, stamp))
        return identifier

    def next_notification(self, channel):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM outbox WHERE channel=? AND state="pending" ORDER BY created LIMIT 1', (channel,)).fetchone()
            if row is None:
                return None
            db.execute('UPDATE outbox SET state="sending",updated=? WHERE id=?', (time.time(), row['id']))
        return {'id': row['id'], 'payload': json.loads(row['payload'])}

    def finish_notification(self, identifier, sent, error=None):
        with self.connect() as db:
            # Failed/ambiguous sends are not automatically retried. This avoids
            # duplicate unsolicited messages after a timeout or process crash.
            db.execute('UPDATE outbox SET state=?,updated=?,error=? WHERE id=?', ('sent' if sent else 'failed', time.time(), error, identifier))

    def recover_notifications(self):
        with self.connect() as db:
            db.execute('UPDATE outbox SET state="unknown",error="interrupted_send" WHERE state="sending"')

    def notification_counts(self):
        with self.connect() as db:
            return {row['state']: row['n'] for row in db.execute('SELECT state,COUNT(*) AS n FROM outbox GROUP BY state')}
