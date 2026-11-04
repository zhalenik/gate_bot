"""Telegram transport for GateBot's shared branch-scoped access service.

The module has no startup side effects. ``run_polling`` works in a supervisor
thread on Windows; no signal handler is installed by python-telegram-bot.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
from pathlib import Path
from typing import Any

from bot_state import BotStateStore
from polling_lock import PollingAlreadyRunning, polling_lease


_RUNTIME: TelegramBot | None = None
_LOOP: asyncio.AbstractEventLoop | None = None
_READY = threading.Event()
_RUNNING = threading.Event()
PAGE_SIZE = 20


def _audit(event: str, *, level: str = "INFO", branch_id: Any = None, **details: Any) -> None:
    import logger
    logger.event(event, branch_id=branch_id, level=level, details={"platform": "telegram", **details})


def _observed_bot(token: str, stop_event: threading.Event) -> Any:
    """Observe successful empty polls as well as errors; started != connected."""
    from telegram.ext import ExtBot

    class ObservedBot(ExtBot):
        async def get_updates(self, *args: Any, **kwargs: Any) -> Any:
            try:
                result = await super().get_updates(*args, **kwargs)
            except Exception as exc:
                _READY.clear()
                _audit("BOT_POLL_ERROR", level="WARNING", error_type=type(exc).__name__)
                raise
            if _RUNNING.is_set() and not stop_event.is_set():
                if not _READY.is_set():
                    _audit("BOT_POLL_READY")
                _READY.set()
            return result

    return ObservedBot(token=token)


def _private_user(update: Any) -> str | None:
    user, chat = update.effective_user, update.effective_chat
    if (user is None or chat is None or chat.type != "private"
            or user.is_bot or not isinstance(user.id, int) or isinstance(user.id, bool)):
        return None
    return str(user.id)


def _action(kind: str, branch_id: Any, button_id: Any) -> str:
    value = f"{kind}:{branch_id}:{button_id}"
    if (not re.fullmatch(r"[ga]:[A-Za-z0-9_-]{1,24}:[A-Za-z0-9_-]{1,32}", value)
            or len(value.encode("utf-8")) > 64):
        raise ValueError("Invalid Telegram branch/button identifier")
    return value


class TelegramBot:
    def __init__(self, service: Any, state: BotStateStore, *, token: str, bot: Any = None,
                 stop_event: threading.Event | None = None) -> None:
        self.service, self.state, self.bot = service, state, bot
        self.stop_event = stop_event or threading.Event()
        self.namespace = "telegram:" + hashlib.sha256(token.encode()).hexdigest()[:16]

    def menu(self, user_id: str, *, page: int = 0, prefix: str = "") -> tuple[str, Any]:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

        sections = self.service.menu_for_identity("telegram", user_id)
        if not sections:
            text = "Чтобы найти ваши пропуски, нажмите «Поделиться номером». Номер должен совпадать с номером в записи на услугу."
            return ((prefix + "\n\n") if prefix else "") + text, ReplyKeyboardMarkup(
                [[KeyboardButton("Поделиться номером", request_contact=True)]],
                resize_keyboard=True, one_time_keyboard=True,
            )
        actions: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for section in sections:
            for button in section.get("buttons", []):
                if button.get("type") == "admin" or (section.get("active") and button.get("type") == "gate"):
                    actions.append((section, button))
        pages = max(1, (len(actions) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(page, 0), pages - 1)
        rows = []
        for section, button in actions[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            label = f"{section['branch_name']} · {button['label']}"[:128]
            rows.append([InlineKeyboardButton(
                label, callback_data=_action("g" if button["type"] == "gate" else "a", section["branch_id"], button["id"]),
            )])
        if pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("← Назад", callback_data=f"m:{page - 1}"))
            if page + 1 < pages:
                nav.append(InlineKeyboardButton("Далее →", callback_data=f"m:{page + 1}"))
            rows.append(nav)
        rows.append([InlineKeyboardButton("Обновить доступ", callback_data=f"m:{page}")])
        text = (
            "Выберите ворота нужного филиала или свяжитесь с администратором."
            if any(section.get("active") for section in sections)
            else "Сейчас активных пропусков нет. Доступ появится в установленное время перед вашей записью. При необходимости свяжитесь с администратором."
        )
        if pages > 1:
            text += f" Страница {page + 1} из {pages}."
        return (((prefix + "\n\n") if prefix else "") + text)[:4000], InlineKeyboardMarkup(rows)

    async def _send_menu(self, message: Any, user_id: str, *, prefix: str = "") -> None:
        text, keyboard = self.menu(user_id, prefix=prefix)
        await message.reply_text(text, reply_markup=keyboard)

    async def on_start(self, update: Any, context: Any) -> None:
        user_id = _private_user(update)
        if user_id is None or update.effective_message is None:
            return
        message = update.effective_message
        args = context.args or []
        prefix = ""
        if args:
            result = await asyncio.to_thread(self.service.bind_token, "telegram", user_id, args[0])
            prefix = result.get("message", "")
        await self._send_menu(message, user_id, prefix=prefix)

    async def on_menu(self, update: Any, context: Any) -> None:
        user_id = _private_user(update)
        if user_id is not None and update.effective_message is not None:
            await self._send_menu(update.effective_message, user_id)

    async def on_message(self, update: Any, context: Any) -> None:
        user_id = _private_user(update)
        message = update.effective_message
        if user_id is None or message is None:
            return
        contact = message.contact
        if contact is not None:
            # Telegram only proves this is the sender's contact when user_id is
            # present and matches. Address-book contacts and forwards do not bind.
            forwarded = any(getattr(message, field, None) is not None for field in (
                "forward_origin", "forward_from", "forward_from_chat", "forward_date",
            ))
            if forwarded or contact.user_id is None or str(contact.user_id) != user_id:
                from telegram import KeyboardButton, ReplyKeyboardMarkup
                await message.reply_text(
                    "Нужен ваш собственный номер. Нажмите «Поделиться номером» в этом боте.",
                    reply_markup=ReplyKeyboardMarkup(
                        [[KeyboardButton("Поделиться номером", request_contact=True)]],
                        resize_keyboard=True, one_time_keyboard=True,
                    ),
                )
                return
            result = await asyncio.to_thread(
                self.service.bind_verified_phone, "telegram", user_id, contact.phone_number,
            )
            from telegram import ReplyKeyboardRemove
            await message.reply_text(result.get("message") or "Номер проверен.", reply_markup=ReplyKeyboardRemove())
            await self._send_menu(message, user_id)
            return
        await self._send_menu(
            message, user_id,
            prefix="Для управления используйте кнопки. Введённый вручную номер телефона не подтверждает владельца.",
        )

    async def on_callback(self, update: Any, context: Any) -> None:
        user_id, query = _private_user(update), update.callback_query
        if query is None:
            return
        if self.stop_event.is_set():
            return
        if user_id is None or query.message is None:
            await query.answer("Откройте личный диалог с ботом и обновите меню.")
            return
        value = query.data if isinstance(query.data, str) else ""
        if re.fullmatch(r"m:\d{1,6}", value):
            from telegram import InlineKeyboardMarkup
            text, keyboard = self.menu(user_id, page=int(value[2:]))
            await query.answer()
            if isinstance(keyboard, InlineKeyboardMarkup):
                # A repeated refresh can be unchanged; Telegram's BadRequest for
                # that exact condition is harmless, other failures are reported.
                from telegram.error import BadRequest
                try:
                    await query.edit_message_text(text, reply_markup=keyboard)
                except BadRequest as exc:
                    if "message is not modified" not in str(exc).lower():
                        raise
            else:
                await query.message.reply_text(text, reply_markup=keyboard)
            return
        match = re.fullmatch(r"([ga]):([A-Za-z0-9_-]{1,24}):([A-Za-z0-9_-]{1,32})", value)
        if match is None:
            # This rejects old callbacks containing a raw telephone number.
            await query.answer("Кнопка устарела. Обновите меню командой /menu.")
            return
        kind, branch_id, button_id = match.groups()
        if kind == "a":
            for section in self.service.menu_for_identity("telegram", user_id):
                if str(section["branch_id"]) == branch_id:
                    for button in section.get("buttons", []):
                        if str(button["id"]) == button_id and button.get("type") == "admin":
                            await query.answer()
                            await query.message.reply_text(f"{section['branch_name']}\n{button['label']}: {button['phone']}")
                            return
            await query.answer("Контакт филиала недоступен. Обновите меню.")
            return
        event_key = hashlib.sha256(f"callback:{query.id}".encode()).hexdigest()
        claimed = await asyncio.to_thread(self.state.claim, self.namespace, event_key)
        if not claimed:
            await query.answer("Это нажатие уже обработано. Для новой команды нажмите кнопку снова.")
            return
        try:
            await query.answer("Проверяю доступ…")
            if self.stop_event.is_set():
                self.state.finish(self.namespace, event_key, "cancelled", "stopping")
                return
            result = await asyncio.to_thread(
                self.service.open_for_identity, "telegram", user_id, branch_id, button_id,
            )
            _audit("BOT_GATE_RESULT", branch_id=branch_id, status=result.get("status", "unknown"), command_id=result.get("command_id"))
            await self._send_menu(
                query.message, user_id,
                prefix=result.get("message") or "Команда обработана. Проверьте состояние ворот.",
            )
        except Exception as exc:
            self.state.finish(self.namespace, event_key, "unknown", type(exc).__name__)
            _audit("BOT_UPDATE_FAILED", level="ERROR", branch_id=branch_id,
                   event_ref=event_key[:16], error_type=type(exc).__name__, outcome="unknown")
            raise
        self.state.finish(self.namespace, event_key, "handled")

    async def notify_access(self, payload: dict[str, Any]) -> bool:
        user_id = payload.get("tg_id")
        if not user_id:
            return False
        try:
            recipient = int(user_id)
        except (TypeError, ValueError):
            return False
        if recipient <= 0:
            return False
        if self.bot is None:
            raise RuntimeError("Telegram bot is not initialized")
        prefix = "Вам предоставлен доступ на парковку." if payload.get("event") == "granted" else "Ваш доступ на парковку обновлён."
        text, keyboard = self.menu(str(recipient), prefix=prefix)
        await self.bot.send_message(chat_id=recipient, text=text, reply_markup=keyboard)
        return True

    async def on_error(self, update: Any, context: Any) -> None:
        _audit("BOT_HANDLER_ERROR", level="ERROR", error_type=type(context.error).__name__)
        if update is not None and _private_user(update) is not None and update.effective_message is not None:
            try:
                await update.effective_message.reply_text("Не удалось выполнить действие. Попробуйте ещё раз или свяжитесь с администратором.")
            except Exception:
                _audit("BOT_ERROR_NOTICE_FAILED", level="WARNING")


async def _run(stop_event: threading.Event, service: Any = None) -> None:
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
    from storage import config
    from access_service import AccessService

    if config.BOT_MODE != "polling":
        raise RuntimeError("Этот запуск Telegram поддерживает polling. Production webhook будет подключён отдельным этапом.")
    if not config.BOT_TOKEN:
        raise ValueError("BOT_TOKEN is required")
    # Vendor error strings may contain Telegram's token-bearing URL. Persist
    # safe structured events ourselves instead of raw library tracebacks.
    for name in ("httpx", "httpcore", "telegram"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    application = Application.builder().bot(_observed_bot(config.BOT_TOKEN, stop_event)).build()
    runtime = TelegramBot(
        service if service is not None else AccessService(),
        BotStateStore(Path(config.DATA_DIR) / "bot_state.sqlite3"),
        token=config.BOT_TOKEN, bot=application.bot, stop_event=stop_event,
    )
    application.add_handler(CommandHandler("start", runtime.on_start))
    application.add_handler(CommandHandler(["menu", "help"], runtime.on_menu))
    application.add_handler(CallbackQueryHandler(runtime.on_callback))
    application.add_handler(MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), runtime.on_message))
    application.add_error_handler(runtime.on_error)

    global _RUNTIME, _LOOP
    initialized = False
    _READY.clear()
    try:
        await application.initialize()
        initialized = True
        webhook = await application.bot.get_webhook_info()
        if webhook.url:
            _audit("BOT_START_REFUSED", level="ERROR", reason="active_webhook")
            raise RuntimeError(
                "Telegram polling не запущен: у бота настроен webhook. "
                "Используйте тестового бота или явно отключите старый webhook."
            )
        _RUNTIME, _LOOP = runtime, asyncio.get_running_loop()
        await application.start()
        _RUNNING.set()
        # Only after confirming there is no active webhook. Pending remote
        # commands are dropped so old gate taps are not executed on startup.
        await application.updater.start_polling(
            drop_pending_updates=True, allowed_updates=["message", "callback_query"],
            timeout=25, bootstrap_retries=0,
        )
        _audit("BOT_POLLING_STARTED")
        while not stop_event.is_set():
            await asyncio.sleep(0.25)
    finally:
        _READY.clear()
        _RUNNING.clear()
        _RUNTIME, _LOOP = None, None
        if application.updater is not None and application.updater.running:
            await application.updater.stop()
        if application.running:
            await application.stop()
        if initialized:
            await application.shutdown()
        _audit("BOT_POLLING_STOPPED")


def run_polling(stop_event: threading.Event | None = None, *, service: Any = None) -> None:
    from storage import config

    if not config.BOT_ENABLED:
        raise RuntimeError("BOT_ENABLED=false: бот отключён в локальной конфигурации.")
    if not config.BOT_TOKEN:
        raise ValueError("BOT_TOKEN is required")
    try:
        with polling_lease("telegram", config.BOT_TOKEN):
            _READY.clear()
            _RUNNING.clear()
            try:
                asyncio.run(_run(stop_event or threading.Event(), service=service))
            except Exception as exc:
                _audit("BOT_RUNTIME_ERROR", level="ERROR", error_type=type(exc).__name__)
                raise
    except PollingAlreadyRunning:
        _audit("BOT_START_REFUSED", level="ERROR", reason="polling_already_running")
        raise


def notify_access(payload: dict[str, Any]) -> bool:
    """Synchronous outbox bridge; call from a worker outside the bot loop."""
    if not payload.get("tg_id"):
        return False
    runtime, loop = _RUNTIME, _LOOP
    if runtime is None or loop is None or not loop.is_running() or not _READY.is_set():
        raise RuntimeError("Telegram bot is not running")
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    if current_loop is loop:
        raise RuntimeError("Await TelegramBot.notify_access from the Telegram event loop")
    future = asyncio.run_coroutine_threadsafe(runtime.notify_access(payload), loop)
    try:
        return future.result(timeout=15)
    except TimeoutError:
        future.cancel()
        _audit("BOT_NOTIFICATION_ERROR", level="ERROR", branch_id=payload.get("branch_id"), error_type="delivery_timeout")
        raise RuntimeError("Telegram delivery timed out") from None
    except Exception as exc:
        _audit("BOT_NOTIFICATION_ERROR", level="ERROR", branch_id=payload.get("branch_id"), error_type=type(exc).__name__)
        raise


def status() -> dict[str, Any]:
    return {"running": _RUNNING.is_set(), "ready": _READY.is_set(), "mode": "polling"}


def main() -> None:
    """Compatibility entry point for the application supervisor."""
    run_polling()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        main()
    except KeyboardInterrupt:
        pass
