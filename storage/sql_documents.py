"""Atomic legacy-document compatibility over the application's SQLite database.

JSON remains authoritative until explicit runtime-owned migration activates SQL.
After activation, old JSON files are immutable recovery copies, never dual-written.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

from filelock import FileLock
from storage.json_store import JsonStore
from storage.product_store import ProductStore


DOCUMENTS = ('branches.json', 'links.json', 'admins.json')
_active_connection = ContextVar('gatebot_document_connection', default=None)


def current_connection():
    """Join the active SQL document transaction (or None in legacy mode)."""
    return _active_connection.get()


def setup_documents(db):
    db.execute('CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, payload TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS migration_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')


def read_document(db, name):
    row = db.execute('SELECT payload FROM documents WHERE name=?', (name,)).fetchone()
    return json.loads(row['payload']) if row else {}


def write_document(db, name, value):
    if name == 'links.json':
        from storage.client_index import sync_client_index
        from storage.pass_changes import stamp_pass_changes
        previous=read_document(db,name)
        sync_client_index(db,value,previous)
        stamp_pass_changes(value,previous)
    elif name == 'admins.json':
        from storage.client_index import sync_admin_roles
        sync_admin_roles(db,value)
    elif name == 'branches.json':
        from notification_policy import clear_disabled_sms
        clear_disabled_sms(db, read_document(db, name), value)
    db.execute('INSERT INTO documents(name,payload) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload',
               (name, json.dumps(value, ensure_ascii=False)))


def is_sql_active(directory):
    return (Path(directory) / '.sql-active').is_file()


class DocumentStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.legacy = JsonStore(path)
        self.lock = self.legacy.lock

    def product(self):
        return ProductStore(self.path.parent / 'application.sqlite3')

    def read(self):
        if not is_sql_active(self.path.parent):
            return self.legacy.read()
        with self.product().connect() as db:
            return read_document(db, self.path.name)

    def replace(self, value):
        if not isinstance(value, dict):
            raise ValueError('State must be an object')
        if not is_sql_active(self.path.parent):
            if self.path.name=='links.json':
                from storage.pass_changes import stamp_pass_changes
                with self.lock:
                    stamp_pass_changes(value,self.legacy.read())
                    return self.legacy.replace(value)
            return self.legacy.replace(value)
        with self.product().connect() as db:
            write_document(db, self.path.name, value)

    def flush(self, value):
        """Make a document visible to a domain hook in the same SQL transaction."""
        db = current_connection()
        if db is None or not is_sql_active(self.path.parent):
            return False
        write_document(db, self.path.name, value)
        return True

    @contextmanager
    def transaction(self):
        if not is_sql_active(self.path.parent):
            with self.legacy.transaction() as value:
                from copy import deepcopy
                previous=deepcopy(value) if self.path.name=='links.json' else None
                yield value
                if previous is not None:
                    from storage.pass_changes import stamp_pass_changes
                    stamp_pass_changes(value,previous)
            return
        with self.lock, self.product().connect() as db:
            value = read_document(db, self.path.name)
            before = json.dumps(value, sort_keys=True)
            marker = _active_connection.set(db)
            try:
                yield value
                if json.dumps(value, sort_keys=True) != before:
                    write_document(db, self.path.name, value)
            finally:
                _active_connection.reset(marker)


def migrate_legacy(data_dir, activate=False):
    """Call with the sole runtime lock held. Counts only; never print records."""
    directory = Path(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if is_sql_active(directory):
        return {'status': 'already_active'}
    snapshots = {name: JsonStore(directory / name).read() for name in DOCUMENTS}
    report = {'status': 'preview', 'documents': {name: len(value) for name, value in snapshots.items()},
              'missing_phones': sum(not r.get('phone') for r in snapshots['links.json'].values()),
              'admins_without_person': sum(not r.get('person_id') for r in snapshots['admins.json'].values())}
    if not activate:
        return report
    product = ProductStore(directory / 'application.sqlite3')
    backup = directory / 'migration-backups' / str(time.time_ns())
    backup.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for name in DOCUMENTS:
        source = directory / name
        if source.exists():
            shutil.copy2(source, backup / name)
            manifest[name] = hashlib.sha256(source.read_bytes()).hexdigest()
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    with product.connect() as db:
        setup_documents(db)
        for name, value in snapshots.items():
            write_document(db, name, value)
        db.execute('INSERT OR REPLACE INTO migration_meta(key,value) VALUES(?,?)', ('json_import', json.dumps(report)))
    # No marker until the entire imported database is durable. Atomic replace is
    # safe across a crash; an interrupted pre-marker import can be repeated.
    marker = directory / '.sql-active.tmp'
    with marker.open('w', encoding='ascii') as out:
        out.write('application.sqlite3\n');out.flush();os.fsync(out.fileno())
    os.replace(marker, directory / '.sql-active')
    report['status'] = 'activated'
    return report
