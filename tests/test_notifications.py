"""Regression tests for delivery preparation, stale grants and orderly shutdown."""
from types import SimpleNamespace
import sys

import pytest

from link_manager import LinkManager, set_notify_hook
from notifications import NotificationDispatcher
from runtime_state import RuntimeState
from storage import config
import logger


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(config, "OUTBOUND_ENABLED", True)
    set_notify_hook(None)
    links = LinkManager(tmp_path / "links.json")
    token = links.create_link({"branch_id": 1, "gate_access": True, "mailing_enabled": True})
    links.bind_identity("max", 101, token=token)
    state = RuntimeState(tmp_path / "runtime.sqlite3")
    sends = []

    def send(payload):
        sends.append(payload)
        return True

    dispatcher = NotificationDispatcher({"max": send}, state, {"max": lambda: {"ready": True}}, links)
    yield links, token, state, dispatcher, sends
    set_notify_hook(None)


def enqueue(links, token, state, event="granted"):
    payload = dict(links.load_links()[token], token=token, event=event)
    return state.enqueue("max", payload)


def test_expired_grant_does_not_send_if_scheduler_has_not_cleaned_it_up(delivery):
    links, token, state, dispatcher, sends = delivery
    links.set_expiry_at(token, "2000-01-01T23:59:59+03:00")
    enqueue(links, token, state)
    dispatcher.process_once()
    assert not sends and state.notification_counts() == {"failed": 1}


def test_revocation_notice_does_not_describe_currently_active_access(delivery):
    links, token, state, dispatcher, sends = delivery
    enqueue(links, token, state, "revoked")
    dispatcher.process_once()
    assert not sends and state.notification_counts() == {"failed": 1}


def test_permission_read_failure_keeps_unsent_notification_pending(delivery, monkeypatch):
    links, token, state, dispatcher, sends = delivery
    enqueue(links, token, state)
    original = links.load_links

    def unavailable():
        raise OSError("synthetic-private-details")

    monkeypatch.setattr(links, "load_links", unavailable)
    with pytest.raises(OSError):
        dispatcher.process_once()
    assert state.notification_counts() == {"pending": 1} and not sends
    monkeypatch.setattr(links, "load_links", original)
    dispatcher.process_once()
    assert len(sends) == 1 and state.notification_counts() == {"sent": 1}


@pytest.mark.parametrize("disabled", ["outbound", "not_ready"])
def test_disabled_delivery_never_claims_or_sends(delivery, monkeypatch, disabled):
    links, token, state, dispatcher, sends = delivery
    enqueue(links, token, state)
    if disabled == "outbound":
        monkeypatch.setattr(config, "OUTBOUND_ENABLED", False)
    else:
        dispatcher.ready["max"] = lambda: {"ready": False}
    dispatcher.process_once()
    assert not sends and state.notification_counts() == {"pending": 1}


class StopAfterCycles:
    def __init__(self, count):
        self.count, self.delays = count, []

    def is_set(self):
        return len(self.delays) >= self.count

    def wait(self, timeout):
        self.delays.append(timeout)
        return self.is_set()


def test_worker_survives_readiness_error_and_reports_only_safe_error_type(delivery):
    links, token, state, dispatcher, sends = delivery
    enqueue(links, token, state)
    attempts = []

    def readiness():
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("PRIVATE READINESS DETAILS")
        return {"ready": True}

    dispatcher.ready["max"] = readiness
    dispatcher._stop = StopAfterCycles(2)
    dispatcher._run()
    assert len(sends) == 1 and state.notification_counts() == {"sent": 1}
    assert dispatcher.status()["consecutive_failures"] == 0
    assert dispatcher.status()["last_success_at"] is not None
    records = logger.read_events()
    errors = [r for r in records if r["event"] == "NOTIFICATION_WORKER_ERROR"]
    assert len(errors) == 1 and errors[0]["details"]["error_type"] == "OSError"
    assert "PRIVATE READINESS" not in str(records)


def test_worker_backoff_is_bounded_after_repeated_database_errors(delivery, monkeypatch):
    _, _, _, dispatcher, _ = delivery

    def broken():
        raise RuntimeError("Synthetic database failure")

    monkeypatch.setattr(dispatcher, "process_once", broken)
    dispatcher._stop = StopAfterCycles(7)
    dispatcher._run()
    assert dispatcher._stop.delays == [2, 4, 8, 16, 30, 30, 30]
    assert dispatcher.status()["consecutive_failures"] == 7
    assert dispatcher.status()["last_error_type"] == "RuntimeError"


def test_supervisor_uses_shared_service_and_stops_intake_before_workers(delivery, monkeypatch):
    import run
    import scheduler
    import notifications
    import sim800c

    order = []
    service = SimpleNamespace(stop_accepting=lambda: order.append("stop_accepting"))
    monkeypatch.setitem(sys.modules, "app", SimpleNamespace(
        app=object(), link_manager=object(), branch_manager=object(), access_service=service))
    scheduler_stub = SimpleNamespace(start=lambda: None, stop=lambda: order.append("scheduler"))
    notifications_stub = SimpleNamespace(start=lambda: None, stop=lambda: order.append("notifications"),
                                         enqueue=lambda payload: None)
    monkeypatch.setattr(scheduler, "Scheduler", lambda *args, **kwargs: scheduler_stub)
    monkeypatch.setattr(notifications, "NotificationDispatcher", lambda *args, **kwargs: notifications_stub)
    server = SimpleNamespace(run=lambda: None, close=lambda: order.append("listener"))
    monkeypatch.setitem(sys.modules, "waitress", SimpleNamespace(create_server=lambda *args, **kwargs: server))
    monkeypatch.setattr(run.signal, "signal", lambda *args: None)
    monkeypatch.setattr(config, "BOT_ENABLED", False)
    monkeypatch.setattr(config, "MAX_BOT_ENABLED", False)

    def stop_modem(**kwargs):
        order.append("sim800")
        return True

    monkeypatch.setattr(sim800c, "shutdown", stop_modem)
    with pytest.raises(RuntimeError, match="Web server stopped"):
        run.main()
    assert order == ["stop_accepting", "listener", "sim800", "scheduler", "notifications"]
