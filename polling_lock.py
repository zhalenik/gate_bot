"""One local polling owner per provider token, independent of the data folder."""
from __future__ import annotations

import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from filelock import FileLock, Timeout


class PollingAlreadyRunning(RuntimeError):
    code = "polling_already_running"


@contextmanager
def polling_lease(platform: str, token: str) -> Iterator[None]:
    if platform not in {"max", "telegram"} or not token:
        raise ValueError("A provider and bot token are required for polling")
    directory = Path(tempfile.gettempdir()) / "gatebot-polling"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fingerprint = hashlib.sha256(token.encode()).hexdigest()
    lock = FileLock(str(directory / f"{platform}-{fingerprint}.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise PollingAlreadyRunning(
            f"Polling {platform} для этого бота уже запущен на этом компьютере. Остановите второй экземпляр."
        ) from None
    try:
        yield
    finally:
        lock.release()
