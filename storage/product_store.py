"""Shared transactional application database; construction performs no network I/O."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
import uuid

from filelock import FileLock

_connections = threading.local()


class ProductStore:
    def __init__(self, path=None):
        if path is None:
            from storage import config
            path = Path(config.DATA_DIR) / 'application.sqlite3'
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        if self.current_connection() is not None:
            return
        with FileLock(self.path + '.init.lock', timeout=30):
            db = sqlite3.connect(self.path, timeout=30)
            try:
                if db.execute('PRAGMA journal_mode').fetchone()[0].lower() != 'wal':
                    db.execute('PRAGMA journal_mode=WAL')
                db.execute('PRAGMA synchronous=FULL')
            finally:
                db.close()

    @contextmanager
    def connect(self):
        active = self.current_connection()
        if active is not None:
            savepoint = 'nested_' + uuid.uuid4().hex
            active.execute('SAVEPOINT ' + savepoint)
            try:
                yield active
                active.execute('RELEASE SAVEPOINT ' + savepoint)
            except BaseException:
                active.execute('ROLLBACK TO SAVEPOINT ' + savepoint)
                active.execute('RELEASE SAVEPOINT ' + savepoint)
                raise
            return
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            db.execute('BEGIN IMMEDIATE')
            if not hasattr(_connections, 'active'):
                _connections.active = {}
            _connections.active[self.path] = db
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            getattr(_connections, 'active', {}).pop(self.path, None)
            db.close()

    def current_connection(self):
        return self.connection_for(self.path)

    @staticmethod
    def connection_for(path):
        return getattr(_connections, 'active', {}).get(str(path))

    transaction = connect
