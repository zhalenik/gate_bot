"""Legacy-compatible JSON storage with atomic, cross-process transactions.

Every read/modify/write must occur inside transaction(). A malformed file is an
error, never an empty database: overwriting damaged customer data is forbidden.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
from filelock import FileLock

class JsonStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.path) + ".lock", timeout=10)

    def _read(self):
        if not self.path.exists():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("State file must contain a JSON object")
        return value

    def _write(self, value):
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(value, f, ensure_ascii=False, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def read(self):
        with self.lock:
            return self._read()

    def replace(self, value):
        if not isinstance(value, dict):
            raise ValueError("State must be an object")
        with self.lock:
            self._write(value)

    @contextmanager
    def transaction(self):
        with self.lock:
            value = self._read()
            before = json.dumps(value, sort_keys=True, ensure_ascii=False)
            yield value
            if json.dumps(value, sort_keys=True, ensure_ascii=False) != before:
                self._write(value)
