import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot_state import BotStateStore
from telegram_bot import TelegramBot


class Service:
    def __init__(self):
        self.calls = []
        self.sections = [
            {"branch_id": "10", "branch_name": "Филиал А", "active": True, "access_expires": None,
             "buttons": [{"id": "gate1", "type": "gate", "label": "Въезд"},
                         {"id": "admin1", "type": "admin", "label": "Администратор", "phone": "+79990000000"}]},
            {"branch_id": "20", "branch_name": "Филиал Б", "active": False, "access_expires": "2020-01-01",
             "buttons": [{"id": "gate2", "type": "gate", "label": "Въезд"}]},
        ]

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
        return {"status": "success", "message": "Тестовая команда принята", "confirmed": False}


def incoming(*, contact=None, text="", chat_type="private", callback=None, callback_id="test-tap"):
    message = SimpleNamespace(contact=contact, text=text, reply_text=AsyncMock(), forward_origin=None)
    query = None if callback is None else SimpleNamespace(
        id=callback_id, data=callback, message=message, answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=77, is_bot=False),
        effective_chat=SimpleNamespace(type=chat_type), effective_message=message,
        callback_query=query,
    )


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GATEBOT_LOG_DIR", str(tmp_path / "logs"))
    return TelegramBot(Service(), BotStateStore(tmp_path / "bots.sqlite3"), token="unit-test-telegram-token")


def test_start_binds_only_supplied_token_then_shows_menu(bot):
    update = incoming()
    asyncio.run(bot.on_start(update, SimpleNamespace(args=["test-pass"])))
    assert ("bind_token", "telegram", "77", "test-pass") in bot.service.calls
    assert update.effective_message.reply_text.await_count == 1


def test_phone_from_text_does_not_bind(bot):
    asyncio.run(bot.on_message(incoming(text="+79990000000"), None))
    assert not any(call[0].startswith("bind") for call in bot.service.calls)


@pytest.mark.parametrize("owner", [None, 88])
def test_foreign_or_unidentified_contact_does_not_bind(bot, owner):
    update = incoming(contact=SimpleNamespace(phone_number="+79990000000", user_id=owner))
    asyncio.run(bot.on_message(update, None))
    assert not any(call[0].startswith("bind") for call in bot.service.calls)


def test_forwarded_own_contact_does_not_bind(bot):
    update = incoming(contact=SimpleNamespace(phone_number="+79990000000", user_id=77))
    update.effective_message.forward_origin = SimpleNamespace(type="user")
    asyncio.run(bot.on_message(update, None))
    assert not any(call[0].startswith("bind") for call in bot.service.calls)


def test_verified_own_contact_binds_to_telegram_namespace(bot):
    update = incoming(contact=SimpleNamespace(phone_number="+79990000000", user_id=77))
    asyncio.run(bot.on_message(update, None))
    assert ("bind_phone", "telegram", "77", "+79990000000") in bot.service.calls


def test_no_private_data_or_gate_action_in_group(bot):
    update = incoming(contact=SimpleNamespace(phone_number="+79990000000", user_id=77), chat_type="group")
    asyncio.run(bot.on_message(update, None))
    assert not bot.service.calls
    update = incoming(chat_type="group", callback="g:10:gate1")
    asyncio.run(bot.on_callback(update, None))
    assert not bot.service.calls


def test_menu_keeps_branch_ids_and_has_no_gate_phone(bot):
    text, markup = bot.menu("77")
    encoded = str(markup.to_dict())
    assert "g:10:gate1" in encoded
    assert "g:20:gate2" not in encoded
    assert "+79990000000" not in encoded


def test_gate_callback_delegates_exact_scope_and_persists_claim(bot):
    update = incoming(callback="g:10:gate1")
    asyncio.run(bot.on_callback(update, None))
    assert ("open", "telegram", "77", "10", "gate1") in bot.service.calls
    restarted = TelegramBot(bot.service, BotStateStore(bot.state.path), token="unit-test-telegram-token")
    asyncio.run(restarted.on_callback(update, None))
    assert len([call for call in bot.service.calls if call[0] == "open"]) == 1


def test_reply_failure_after_command_does_not_cause_second_call(bot):
    update = incoming(callback="g:10:gate1")
    update.effective_message.reply_text.side_effect = RuntimeError("test send failed")
    with pytest.raises(RuntimeError):
        asyncio.run(bot.on_callback(update, None))
    asyncio.run(bot.on_callback(update, None))
    assert len([call for call in bot.service.calls if call[0] == "open"]) == 1


def test_legacy_phone_callback_is_rejected(bot):
    asyncio.run(bot.on_callback(incoming(callback="gate:10:+79990000000"), None))
    assert not any(call[0] == "open" for call in bot.service.calls)


def test_inactive_branch_admin_callback_does_not_leak_phone(bot):
    update = incoming(callback="a:20:admin1")
    asyncio.run(bot.on_callback(update, None))
    update.effective_message.reply_text.assert_not_awaited()


def test_admin_contact_available_after_access_expiry_for_bound_branch(bot):
    bot.service.sections[1]["buttons"].append({"id": "admin2", "type": "admin", "label": "Помощь", "phone": "+79990000000"})
    assert "a:20:admin2" in str(bot.menu("77")[1].to_dict())
    update = incoming(callback="a:20:admin2")
    asyncio.run(bot.on_callback(update, None))
    assert "+79990000000" in update.effective_message.reply_text.await_args.args[0]
    assert not any(call[0] == "open" for call in bot.service.calls)


def test_notification_uses_telegram_recipient_and_current_menu(bot):
    bot.bot = SimpleNamespace(send_message=AsyncMock())
    assert asyncio.run(bot.notify_access({"max_id": "77", "event": "granted"})) is False
    assert asyncio.run(bot.notify_access({"tg_id": "77", "event": "revoked"})) is True
    kwargs = bot.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 77
    assert "g:20:gate2" not in str(kwargs)


def fake_application(webhook_url=""):
    app = SimpleNamespace(
        running=False, bot=SimpleNamespace(get_webhook_info=AsyncMock(return_value=SimpleNamespace(url=webhook_url)), send_message=AsyncMock()),
        initialize=AsyncMock(), shutdown=AsyncMock(), add_handler=lambda _: None, add_error_handler=lambda _: None,
    )
    app.updater = SimpleNamespace(running=False, start_polling=AsyncMock(), stop=AsyncMock())
    async def start():
        app.running = True
    async def stop():
        app.running = False
    async def start_polling(**kwargs):
        app.updater.running = True
    async def stop_polling():
        app.updater.running = False
    app.start, app.stop = AsyncMock(side_effect=start), AsyncMock(side_effect=stop)
    app.updater.start_polling.side_effect = start_polling
    app.updater.stop.side_effect = stop_polling
    return app


def patch_runtime(monkeypatch, tmp_path, application):
    monkeypatch.setenv("GATEBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GATEBOT_LOG_DIR", str(tmp_path / "logs"))
    from storage import config
    import telegram.ext
    monkeypatch.setattr(config, "BOT_TOKEN", "unit-test-telegram-token")
    monkeypatch.setattr(config, "BOT_ENABLED", True)
    monkeypatch.setattr(config, "BOT_MODE", "polling")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    builder = SimpleNamespace(bot=lambda _: SimpleNamespace(build=lambda: application))
    monkeypatch.setattr(telegram.ext, "Application", SimpleNamespace(builder=lambda: builder))
    class FakeExtBot:
        def __init__(self, **_):
            pass
    monkeypatch.setattr(telegram.ext, "ExtBot", FakeExtBot)


def test_polling_lifecycle_works_in_supervisor_thread_without_signal_handlers(tmp_path, monkeypatch):
    from telegram_bot import run_polling, status
    app = fake_application()
    patch_runtime(monkeypatch, tmp_path, app)
    stop = threading.Event()
    stop.set()
    errors = []
    def target():
        try:
            run_polling(stop, service=Service())
        except Exception as error:
            errors.append(error)
    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=10)
    assert not thread.is_alive() and errors == []
    app.updater.start_polling.assert_awaited_once()
    assert app.updater.start_polling.await_args.kwargs["drop_pending_updates"] is True
    app.shutdown.assert_awaited_once()
    assert status()["ready"] is False


def test_polling_refuses_existing_webhook_without_deleting_it(tmp_path, monkeypatch):
    from telegram_bot import run_polling
    app = fake_application("https://example.invalid/webhook")
    patch_runtime(monkeypatch, tmp_path, app)
    with pytest.raises(RuntimeError, match="webhook"):
        run_polling(threading.Event(), service=Service())
    app.updater.start_polling.assert_not_awaited()
    app.shutdown.assert_awaited_once()


def test_sync_notification_bridge_runs_coroutine_on_bot_loop(bot, monkeypatch):
    import telegram_bot
    bot.bot = SimpleNamespace(send_message=AsyncMock())
    loop = asyncio.new_event_loop()
    started = threading.Event()
    def loop_worker():
        asyncio.set_event_loop(loop)
        loop.call_soon(started.set)
        loop.run_forever()
        loop.close()
    thread = threading.Thread(target=loop_worker)
    thread.start()
    assert started.wait(timeout=3)
    monkeypatch.setattr(telegram_bot, "_RUNTIME", bot)
    monkeypatch.setattr(telegram_bot, "_LOOP", loop)
    ready = threading.Event()
    ready.set()
    monkeypatch.setattr(telegram_bot, "_READY", ready)
    try:
        assert telegram_bot.notify_access({"tg_id": "77", "event": "granted"}) is True
        bot.bot.send_message.assert_awaited_once()
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)
    assert not thread.is_alive()


def test_telegram_readiness_tracks_failed_and_recovered_empty_polls(monkeypatch):
    import telegram.ext
    import telegram_bot
    values = [[], RuntimeError("private vendor error"), []]
    class FakeExtBot:
        def __init__(self, **_):
            pass
        async def get_updates(self, *_, **__):
            value = values.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
    monkeypatch.setattr(telegram.ext, "ExtBot", FakeExtBot)
    ready, running = threading.Event(), threading.Event()
    running.set()
    monkeypatch.setattr(telegram_bot, "_READY", ready)
    monkeypatch.setattr(telegram_bot, "_RUNNING", running)
    observed = telegram_bot._observed_bot("test-token", threading.Event())
    asyncio.run(observed.get_updates())
    assert telegram_bot.status()["ready"] is True
    with pytest.raises(RuntimeError):
        asyncio.run(observed.get_updates())
    assert telegram_bot.status() == {"ready": False, "running": True, "mode": "polling"}
    asyncio.run(observed.get_updates())
    assert telegram_bot.status()["ready"] is True


def test_telegram_standalone_requires_enabled_flag(monkeypatch):
    import telegram_bot
    from storage import config
    monkeypatch.setattr(config, "BOT_ENABLED", False)
    with pytest.raises(RuntimeError, match="BOT_ENABLED"):
        telegram_bot.run_polling(threading.Event())


def test_shutdown_during_callback_answer_prevents_telegram_call(bot):
    update = incoming(callback="g:10:gate1")
    async def stopping_answer(*_, **__):
        bot.stop_event.set()
    update.callback_query.answer.side_effect = stopping_answer
    asyncio.run(bot.on_callback(update, None))
    assert not any(call[0] == "open" for call in bot.service.calls)
