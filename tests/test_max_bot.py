import hashlib
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bot_state import BotStateStore
from max_api import MaxApiError
from max_bot import MaxBot, normalize_update, register_webhook, verified_contact_phone


TOKEN = "unit-test-max-token"
NOW = 1800000000


def sections():
    return [
        {"branch_id": "10", "branch_name": "Филиал А", "active": True, "access_expires": None,
         "buttons": [{"id": "gate1", "type": "gate", "label": "Въезд"},
                     {"id": "admin1", "type": "admin", "label": "Администратор", "phone": "+79990000000"}]},
        {"branch_id": "20", "branch_name": "Филиал Б", "active": False, "access_expires": "2020-01-01T00:00:00",
         "buttons": [{"id": "gate2", "type": "gate", "label": "Въезд"}]},
    ]


class Service:
    def __init__(self):
        self.sections = sections()
        self.calls = []

    def menu_for_identity(self, platform, user_id):
        self.calls.append(("menu", platform, user_id))
        return self.sections

    def bind_token(self, *args):
        self.calls.append(("bind_token", *args))
        return {"status": "success", "message": "Пропуск привязан"}

    def bind_verified_phone(self, *args):
        self.calls.append(("bind_phone", *args))
        return {"status": "success", "message": "Номер подтверждён"}

    def open_for_identity(self, *args):
        self.calls.append(("open", *args))
        return {"status": "success", "message": "Тестовая команда принята, физический звонок не выполнялся", "confirmed": False}


class API:
    def __init__(self):
        self.sent, self.answers, self.markers, self.send_attempts = [], [], [], []
        self.batch = {"updates": [], "marker": 99}
        self.send_error = None

    def send_message(self, **kwargs):
        self.send_attempts.append(kwargs)
        if self.send_error:
            error, self.send_error = self.send_error, None
            raise error
        self.sent.append(kwargs)
        return {"message": {"body": {"mid": "response"}}}

    def answer_callback(self, callback_id, **kwargs):
        self.answers.append((callback_id, kwargs))
        return {"success": True}

    def get_updates(self, *, marker=None):
        self.markers.append(marker)
        return self.batch


def raw_message(text="/menu", *, attachments=None, user_id=77, mid="test-mid"):
    return {"update_type": "message_created", "timestamp": NOW * 1000,
            "message": {"sender": {"user_id": user_id, "is_bot": False},
                        "recipient": {"chat_type": "dialog", "chat_id": -50, "user_id": 999},
                        "body": {"mid": mid, "text": text, "attachments": attachments}}}


def raw_callback(payload="g:10:gate1", *, callback_id="tap-1", stamp=NOW * 1000):
    raw = raw_message()
    raw["update_type"] = "message_callback"
    raw["message"]["sender"] = {"user_id": 999, "is_bot": True}
    raw["callback"] = {"callback_id": callback_id, "payload": payload,
                       "timestamp": stamp, "user": {"user_id": 77, "is_bot": False}}
    return raw


def contact_payload(*, phone="79990000000", owner=77):
    vcf = f"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Тестовый пользователь\r\nTEL;TYPE=cell:{phone}\r\nEND:VCARD\r\n"
    proof = hmac.new(TOKEN.encode(), vcf.encode(), hashlib.sha256).hexdigest()
    return {"type": "contact", "payload": {"vcf_info": vcf, "hash": proof, "max_info": {"user_id": owner}}}


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GATEBOT_LOG_DIR", str(tmp_path / "logs"))
    return MaxBot(API(), Service(), BotStateStore(tmp_path / "bots.sqlite3"), token=TOKEN, clock=lambda: NOW)


def test_native_callback_actor_is_not_the_bot_sender():
    update = normalize_update(raw_callback())
    assert update.user_id == "77"
    assert update.chat_id == -50
    assert update.callback_payload == "g:10:gate1"


def test_nullable_fields_unknown_events_and_group_messages_are_safe():
    assert normalize_update(raw_message(attachments=None)).attachments == ()
    callback = raw_callback()
    callback["message"] = None
    assert normalize_update(callback).has_message is False
    message = raw_message()
    message["message"]["body"] = None
    assert normalize_update(message).text == ""
    message["message"]["recipient"]["chat_type"] = "chat"
    assert normalize_update(message) is None
    assert normalize_update({"update_type": "user_added"}) is None


def test_verified_contact_hash_uses_exact_cyrillic_vcf_and_crlf(bot):
    raw = raw_message(attachments=[contact_payload()])
    update = normalize_update(raw)
    assert verified_contact_phone(update, TOKEN) == "+79990000000"
    bot.process_update(raw)
    assert ("bind_phone", "max", "77", "+79990000000") in bot.service.calls


@pytest.mark.parametrize("change", ["missing_hash", "tampered", "wrong_owner", "forwarded", "ambiguous"])
def test_unverified_contact_never_binds(bot, change):
    contact = contact_payload()
    raw = raw_message(attachments=[contact])
    if change == "missing_hash":
        del contact["payload"]["hash"]
    elif change == "tampered":
        contact["payload"]["vcf_info"] = contact["payload"]["vcf_info"].replace("79990000000", "79990000001")
    elif change == "wrong_owner":
        contact["payload"]["max_info"]["user_id"] = 88
    elif change == "forwarded":
        raw["message"]["link"] = {"type": "forward"}
    elif change == "ambiguous":
        vcf = contact["payload"]["vcf_info"].replace("END:VCARD", "TEL:79990000001\r\nEND:VCARD")
        contact["payload"]["vcf_info"] = vcf
        contact["payload"]["hash"] = hmac.new(TOKEN.encode(), vcf.encode(), hashlib.sha256).hexdigest()
    bot.process_update(raw)
    assert not any(call[0] == "bind_phone" for call in bot.service.calls)


def test_typed_phone_is_not_authorization(bot):
    bot.process_update(raw_message("+79990000000"))
    assert not any(call[0].startswith("bind") for call in bot.service.calls)


def test_bot_started_passes_only_exact_token_to_shared_service(bot):
    bot.process_update({"update_type": "bot_started", "timestamp": NOW * 1000,
                        "chat_id": -50, "user": {"user_id": 77, "is_bot": False}, "payload": "test-pass"})
    assert ("bind_token", "max", "77", "test-pass") in bot.service.calls
    assert bot.api.sent


def test_keyboard_has_branch_and_button_ids_without_gate_phone(bot):
    menu = bot.menu("77")
    encoded = str(menu["attachments"])
    assert "g:10:gate1" in encoded and "a:10:admin1" in encoded
    assert "g:20:gate2" not in encoded
    assert "+79990000000" not in encoded


def test_admin_contact_remains_available_for_bound_expired_branch(bot):
    bot.service.sections[1]["buttons"].append({"id": "admin2", "type": "admin", "label": "Помощь", "phone": "+79990000000"})
    assert "a:20:admin2" in str(bot.menu("77")["attachments"])
    bot.process_update(raw_callback("a:20:admin2"))
    assert "+79990000000" in bot.api.sent[-1]["text"]
    assert not any(call[0] == "open" for call in bot.service.calls)


def test_callback_is_scoped_and_deduplicated_across_restart(bot):
    bot.process_update(raw_callback())
    assert ("open", "max", "77", "10", "gate1") in bot.service.calls
    restarted = MaxBot(bot.api, bot.service, BotStateStore(bot.state.path), token=TOKEN, clock=lambda: NOW)
    restarted.process_update(raw_callback())
    assert len([c for c in bot.service.calls if c[0] == "open"]) == 1


def test_new_tap_on_same_max_keyboard_is_not_mistaken_for_redelivery(bot):
    bot.process_update(raw_callback())
    bot._clock = lambda: NOW + 30
    bot.process_update(raw_callback(stamp=(NOW + 30) * 1000))
    assert len([call for call in bot.service.calls if call[0] == "open"]) == 2


def test_callback_never_redials_if_reply_failed_after_call(bot):
    bot.api.send_error = MaxApiError("network_error")
    with pytest.raises(MaxApiError):
        bot.process_update(raw_callback())
    bot.process_update(raw_callback())
    assert len([c for c in bot.service.calls if c[0] == "open"]) == 1


@pytest.mark.parametrize("payload", ["gate:10:+79990000000", "g:10:+79990000000", "g:10:gate1:extra", "unknown"])
def test_legacy_or_phone_bearing_callback_does_not_dial(bot, payload):
    bot.process_update(raw_callback(payload))
    assert not any(c[0] == "open" for c in bot.service.calls)


def test_stale_or_deleted_callback_does_not_dial(bot):
    bot.process_update(raw_callback(stamp=(NOW - 121) * 1000))
    assert not any(c[0] == "open" for c in bot.service.calls)
    raw = raw_callback(callback_id="another-tap")
    raw["message"] = None
    bot.process_update(raw)
    assert not any(c[0] == "open" for c in bot.service.calls)


def test_marker_stays_old_until_batch_processed_and_survives_restart(bot):
    bot.state.set_checkpoint(bot.namespace, "marker", 10)
    bot.api.batch = {"updates": [raw_message()], "marker": 90}
    bot.api.send_error = MaxApiError("network_error")
    with pytest.raises(MaxApiError):
        bot.poll_once()
    assert bot.state.get_checkpoint(bot.namespace, "marker") == 10
    bot.poll_once()
    assert bot.api.markers == [10, 10]
    state = BotStateStore(bot.state.path)
    assert state.get_checkpoint(bot.namespace, "marker") == 90
    assert len(bot.api.send_attempts) == 1
    assert state.event_state(bot.namespace, normalize_update(raw_message()).event_key)["outcome"] == "unknown"


def test_failed_safe_get_does_not_claim_or_skip_unseen_events(bot):
    bot.state.set_checkpoint(bot.namespace, "marker", 10)
    original = bot.api.get_updates
    def failed_get(**kwargs):
        raise MaxApiError("network_error", retryable=True)
    bot.api.get_updates = failed_get
    with pytest.raises(MaxApiError):
        bot.poll_once()
    assert not bot.state.seen(bot.namespace, normalize_update(raw_message()).event_key)
    assert bot.state.get_checkpoint(bot.namespace, "marker") == 10
    bot.api.get_updates = original
    bot.api.batch = {"updates": [raw_message()], "marker": 11}
    bot.poll_once()
    assert len(bot.api.sent) == 1


def test_shutdown_after_long_poll_cancels_gate_without_replay(bot):
    stop = threading.Event()
    raw = raw_callback()
    def response_after_stop(**kwargs):
        stop.set()
        return {"updates": [raw], "marker": 12}
    bot.api.get_updates = response_after_stop
    bot.poll_once(stop_event=stop)
    assert not any(call[0] == "open" for call in bot.service.calls)
    assert bot.state.get_checkpoint(bot.namespace, "marker") is None
    key = normalize_update(raw).event_key
    assert bot.state.event_state(bot.namespace, key)["outcome"] == "cancelled"
    stop.clear()
    bot.process_update(raw)
    assert not any(call[0] == "open" for call in bot.service.calls)


def test_shutdown_during_callback_answer_prevents_gate_call(bot):
    stop = threading.Event()
    answer = bot.api.answer_callback
    def stop_after_answer(*args, **kwargs):
        result = answer(*args, **kwargs)
        stop.set()
        return result
    bot.api.answer_callback = stop_after_answer
    bot.process_update(raw_callback(), stop_event=stop)
    assert not any(call[0] == "open" for call in bot.service.calls)
    assert bot.state.event_state(bot.namespace, normalize_update(raw_callback()).event_key)["outcome"] == "cancelled"


def test_update_failure_has_safe_durable_outcome_and_audit(bot, monkeypatch):
    import logger
    events = []
    monkeypatch.setattr(logger, "event", lambda name, **kwargs: events.append((name, kwargs)))
    bot.api.send_error = MaxApiError("network_error")
    with pytest.raises(MaxApiError):
        bot.process_update(raw_message())
    event = bot.state.event_state(bot.namespace, normalize_update(raw_message()).event_key)
    assert event["outcome"] == "unknown" and event["error_code"] == "network_error"
    assert any(name == "BOT_UPDATE_FAILED" for name, _ in events)
    assert TOKEN not in str(events) and "79990000000" not in str(events)


def test_standalone_max_respects_disabled_flag(monkeypatch):
    import max_bot
    from storage import config
    monkeypatch.setattr(config, "MAX_BOT_ENABLED", False)
    monkeypatch.setattr(max_bot, "create_bot", lambda **_: pytest.fail("Disabled bot must not initialize"))
    with pytest.raises(RuntimeError, match="MAX_BOT_ENABLED"):
        max_bot.run_polling(threading.Event())


def test_same_max_token_cannot_poll_from_two_data_directories(tmp_path, monkeypatch):
    import max_bot
    import polling_lock
    from storage import config
    monkeypatch.setattr(polling_lock.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(config, "MAX_BOT_ENABLED", True)
    monkeypatch.setattr(config, "MAX_BOT_MODE", "polling")
    monkeypatch.setattr(config, "MAX_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "first"))
    entered, release = threading.Event(), threading.Event()
    class Dummy:
        def run(self, _):
            entered.set()
            release.wait(timeout=5)
    monkeypatch.setattr(max_bot, "create_bot", lambda **_: Dummy())
    errors = []
    def first():
        try:
            max_bot.run_polling(threading.Event())
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(timeout=3)
    try:
        monkeypatch.setattr(config, "DATA_DIR", str(tmp_path / "second"))
        with pytest.raises(polling_lock.PollingAlreadyRunning):
            max_bot.run_polling(threading.Event())
    finally:
        release.set()
        thread.join(timeout=3)
    assert not thread.is_alive() and not errors


def test_polling_lock_keeps_providers_separate_and_filename_private(tmp_path, monkeypatch):
    import polling_lock
    monkeypatch.setattr(polling_lock.tempfile, "gettempdir", lambda: str(tmp_path))
    with polling_lock.polling_lease("max", TOKEN):
        with polling_lock.polling_lease("telegram", TOKEN):
            # Windows removes FileLock files on release. Their persistence
            # after release is not part of the provider-isolation guarantee.
            names = [path.name for path in (tmp_path / "gatebot-polling").iterdir()]
            assert len(names) == 2 and all(TOKEN not in name for name in names)


def test_blocked_user_does_not_stall_next_update(bot):
    bot.api.batch = {"updates": [raw_message(mid="first"), raw_message(mid="second", user_id=88)], "marker": 12}
    bot.api.send_error = MaxApiError("chat.denied", status_code=403)
    bot.poll_once()
    assert len(bot.api.sent) == 1
    assert bot.state.get_checkpoint(bot.namespace, "marker") == 12


def test_atomic_claim_does_not_share_identity_between_messengers(tmp_path):
    path = tmp_path / "state.sqlite3"
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: BotStateStore(path).claim("max", "same-tap"), range(20)))
    assert results.count(True) == 1
    state = BotStateStore(path)
    assert state.claim("telegram", "same-tap") is True


def test_existing_webhook_is_not_removed(bot):
    bot.api.get_subscriptions = lambda: [{"url": "https://example.invalid/webhook"}]
    bot.api.get_me = lambda: pytest.fail("Must refuse before starting")
    with pytest.raises(MaxApiError, match="active_webhook") as caught:
        bot.run(threading.Event())
    assert caught.value.stage == "subscriptions"


@pytest.mark.parametrize("failed_stage", ["subscriptions", "get_me"])
def test_startup_error_identifies_check_and_does_not_poll(bot, failed_stage, monkeypatch):
    import max_bot
    ready, running = threading.Event(), threading.Event()
    ready.set()
    running.set()
    monkeypatch.setattr(max_bot, "_READY", ready)
    monkeypatch.setattr(max_bot, "_RUNNING", running)
    def failure():
        raise MaxApiError("read_timeout")
    bot.api.get_subscriptions = failure if failed_stage == "subscriptions" else lambda: []
    bot.api.get_me = failure if failed_stage == "get_me" else lambda: {}
    bot.poll_once = lambda **_: pytest.fail("Must not poll before successful checks")
    with pytest.raises(MaxApiError) as caught:
        bot.run(threading.Event())
    assert caught.value.stage == failed_stage
    assert not ready.is_set() and not running.is_set()


def test_max_readiness_tracks_success_error_and_shutdown(bot, monkeypatch):
    import max_bot
    ready, running = threading.Event(), threading.Event()
    monkeypatch.setattr(max_bot, "_READY", ready)
    monkeypatch.setattr(max_bot, "_RUNNING", running)
    class Stop(threading.Event):
        def wait(self, timeout=None):
            return self.is_set()
    stop = Stop()
    observed = []
    bot.api.get_subscriptions = lambda: []
    bot.api.get_me = lambda: {}
    def poll_once(**kwargs):
        observed.append((ready.is_set(), running.is_set()))
        if len(observed) == 2:
            raise MaxApiError("network_error")
        if len(observed) == 3:
            stop.set()
    bot.poll_once = poll_once
    bot.run(stop)
    assert observed == [(False, True), (True, True), (False, True)]
    assert not ready.is_set() and not running.is_set()


def test_webhook_checks_official_secret_and_durable_enqueue(bot):
    from flask import Flask
    app = Flask(__name__)
    events = []
    register_webhook(app, bot, secret="test_secret", enqueue=events.append)
    client = app.test_client()
    assert client.post("/webhook/max", json={}, headers={"X-Webhook-Secret": "test_secret"}).status_code == 401
    response = client.post("/webhook/max", json=raw_message(), headers={"X-Max-Bot-Api-Secret": "test_secret"})
    assert response.status_code == 200 and len(events) == 1
    assert not bot.api.sent


def test_webhook_does_not_ack_when_enqueue_fails(bot):
    from flask import Flask
    app = Flask(__name__)
    app.config["PROPAGATE_EXCEPTIONS"] = False
    def unavailable(_):
        raise OSError("storage unavailable")
    register_webhook(app, bot, secret="test_secret", enqueue=unavailable)
    response = app.test_client().post("/webhook/max", json=raw_message(), headers={"X-Max-Bot-Api-Secret": "test_secret"})
    assert response.status_code == 500


def test_notification_targets_only_max_identity_and_uses_current_menu(bot):
    assert bot.notify_access({"tg_id": "77", "event": "granted"}) is False
    assert bot.notify_access({"max_id": "77", "event": "revoked"}) is True
    assert bot.api.sent[-1]["user_id"] == 77
    assert "g:20:gate2" not in str(bot.api.sent[-1])
