"""The web process may request a restart only while the supervisor owns the runtime."""
from threading import Lock

_lock = Lock()
_restart = None


def bind_restart(callback):
    global _restart
    with _lock:
        _restart = callback


def restart_callback():
    with _lock:
        return _restart
