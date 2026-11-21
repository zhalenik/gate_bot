"""MAX parking bot. MAX updates are handled in their own native schema.

Run explicitly with ``python max_bot.py`` or ``run_polling(stop_event)``.
Imports never start a bot, modify webhook subscriptions, or contact a service.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot_state import BotStateStore
from max_api import MaxApiClient, MaxApiError
from polling_lock import PollingAlreadyRunning, polling_lease


_RUNTIME: MaxBot | None = None
_READY = threading.Event()
_RUNNING = threading.Event()
PAGE_SIZE = 20
CALLBACK_MAX_AGE_SECONDS = 120


def _audit(event: str, *, level: str = "INFO", branch_id: Any = None, **details: Any) -> None:
    import logger
    logger.event(event, branch_id=branch_id, level=level, details={"platform": "max", **details})


class _Stopping(Exception):
    pass


def _id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class IncomingUpdate:
    kind: str
    user_id: str
    chat_id: int | None
    event_key: str
    previous_key: str = ""
    timestamp_ms: int | None = None
    text: str = ""
    start_payload: str = ""
    attachments: tuple[dict[str, Any], ...] = ()
    callback_id: str = ""
    callback_payload: str = ""
    forwarded: bool = False
    has_message: bool = True
    message_id: str = ''


def normalize_update(raw: Any) -> IncomingUpdate | None:
    """Accept only private user events; nullable MAX fields remain nullable."""
    if not isinstance(raw, dict):
        return None
    kind = raw.get("update_type")
    if kind not in {"bot_started", "message_created", "message_callback"}:
        return None
    stamp = _id(raw.get("timestamp"))
    text = start_payload = callback_id = callback_payload = ""
    attachments: tuple[dict[str, Any], ...] = ()
    forwarded = False
    has_message = True
    message_id = ''
    old_identity = None

    if kind == "bot_started":
        actor = raw.get("user")
        chat_id = _id(raw.get("chat_id"))
        if isinstance(raw.get("payload"), str):
            start_payload = raw["payload"]
        identity = f"started:{stamp}:{chat_id}"
    else:
        message = raw.get("message")
        if message is not None and not isinstance(message, dict):
            return None
        if message is None:
            # MAX permits a missing callback message when it was deleted.
            # We can acknowledge this callback, but never actuate from it.
            if kind != "message_callback":
                return None
            message = {}
            has_message = False
        recipient = message.get("recipient") or {}
        if not isinstance(recipient, dict):
            return None
        if has_message and recipient.get("chat_type") != "dialog":
            return None
        chat_id = _id(recipient.get("chat_id"))
        body = message.get("body") or {}
        if not isinstance(body, dict):
            return None
        message_id = body.get('mid') if isinstance(body.get('mid'), str) else ''
        if isinstance(body.get("text"), str):
            text = body["text"]
        raw_attachments = body.get("attachments") or []
        if not isinstance(raw_attachments, list):
            return None
        attachments = tuple(a for a in raw_attachments if isinstance(a, dict))
        link = message.get("link")
        forwarded = isinstance(link, dict) and link.get("type") == "forward"
        if kind == "message_callback":
            callback = raw.get("callback")
            if not isinstance(callback, dict) or not isinstance(callback.get("callback_id"), str):
                return None
            actor = callback.get("user")
            callback_id = callback["callback_id"]
            if not callback_id.strip():
                return None
            callback_payload = callback.get("payload") or ""
            if not isinstance(callback_payload, str):
                return None
            stamp = _id(callback.get("timestamp")) or stamp
            # In MAX callback_id identifies the keyboard; the schema does not
            # promise a new ID for every tap. Include the event time and action
            # so pressing the same keyboard later remains a new command.
            identity = json.dumps(
                ["callback", callback_id, stamp, callback_payload],
                ensure_ascii=False, separators=(",", ":"),
            )
            old_identity = json.dumps(["callback",callback_id,stamp,body.get("mid"),callback_payload],
                                      ensure_ascii=False,separators=(",",":"))
        else:
            actor = message.get("sender")
            mid = body.get("mid")
            if not isinstance(mid, str) or not mid:
                mid = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            old_identity = f"message:{mid}:{stamp}"
            identity = f"message:{mid}" if message_id else old_identity

    if not isinstance(actor, dict) or actor.get("is_bot") is True:
        return None
    user_id = _id(actor.get("user_id"))
    if user_id is None or user_id <= 0:
        return None
    # Store a digest, never a contact, access token, or raw callback payload.
    event_key = hashlib.sha256(f"{user_id}:{identity}".encode()).hexdigest()
    previous_key = (hashlib.sha256(f"{user_id}:{old_identity}".encode()).hexdigest()
                    if old_identity and identity != old_identity else '')
    return IncomingUpdate(
        kind=kind, user_id=str(user_id), chat_id=chat_id, event_key=event_key, previous_key=previous_key,
        timestamp_ms=stamp, text=text, start_payload=start_payload,
        attachments=attachments, callback_id=callback_id,
        callback_payload=callback_payload, forwarded=forwarded,
        has_message=has_message, message_id=message_id,
    )


def verified_contact_phone(update: IncomingUpdate, bot_token: str) -> str | None:
    """Verify MAX's request_contact HMAC before parsing its exact VCF text."""
    contacts = [a for a in update.attachments if a.get("type") == "contact"]
    if update.forwarded or len(contacts) != 1:
        return None
    payload = contacts[0].get("payload")
    if not isinstance(payload, dict):
        return None
    vcf, proof = payload.get("vcf_info"), payload.get("hash")
    if (not isinstance(vcf, str) or not 1 <= len(vcf) <= 16384
            or not isinstance(proof, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", proof)):
        return None
    expected = hmac.new(bot_token.encode("utf-8"), vcf.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, proof.lower()):
        return None
    info = payload.get("max_info")
    if info is not None:
        if not isinstance(info, dict) or info.get("is_bot") is True:
            return None
        owner_id = _id(info.get("user_id"))
        if owner_id is not None and str(owner_id) != update.user_id:
            return None
    # Unfold only after signature verification; never normalize before HMAC.
    unfolded = re.sub(r"\r?\n[ \t]", "", vcf)
    if "BEGIN:VCARD" not in unfolded or "END:VCARD" not in unfolded:
        return None
    numbers: set[str] = set()
    for line in unfolded.splitlines():
        match = re.fullmatch(r"(?:item\d+\.)?TEL(?:;[^:]*)?:(.*)", line, re.IGNORECASE)
        if not match:
            continue
        value = match.group(1).strip()
        if value.lower().startswith("tel:"):
            value = value[4:]
        if not re.fullmatch(r"\+?[0-9 ()-]{8,32}", value):
            return None
        digits = re.sub(r"[^0-9]", "", value)
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        elif len(digits) == 10 and digits.startswith("9"):
            digits = "7" + digits
        if not 8 <= len(digits) <= 15:
            return None
        numbers.add("+" + digits)
    return numbers.pop() if len(numbers) == 1 else None


def _action(kind: str, branch_id: Any, button_id: Any) -> str:
    value = f"{kind}:{branch_id}:{button_id}"
    if not re.fullmatch(r"[ga]:[A-Za-z0-9_-]{1,24}:[A-Za-z0-9_-]{1,32}", value):
        raise ValueError("Invalid branch/button identifier")
    return value


def _keyboard(rows: list[list[dict[str, str]]]) -> list[dict[str, Any]]:
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


class MaxBot:
    def __init__(
        self, api: MaxApiClient, service: Any, state: BotStateStore, *,
        token: str, clock: Any = time.time,
    ) -> None:
        self.api, self.service, self.state = api, service, state
        self._token, self._clock = token, clock
        self.namespace = "max:" + hashlib.sha256(token.encode()).hexdigest()[:16]
        self.workflows = None
        if getattr(service, 'identity', None) is not None:
            from bot_workflows import BotWorkflows
            self.workflows = BotWorkflows(service, state)

    def menu(self, user_id: str, *, page: int = 0, prefix: str = "") -> dict[str, Any]:
        sections = self.service.menu_for_identity("max", user_id)
        actions: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for section in sections:
            for button in section.get("buttons", []):
                if button.get("type") == "admin" and self.workflows and getattr(self.service, "desk", None):
                    continue
                if button.get("type") == "admin" or (section.get("active") and button.get("type") == "gate"):
                    actions.append((section, button))
        pages = max(1, (len(actions) + PAGE_SIZE - 1) // PAGE_SIZE)
        page = min(max(page, 0), pages - 1)
        rows: list[list[dict[str, str]]] = []
        for section, button in actions[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            label = f"{section['branch_name']} · {button['label']}"[:128]
            if button['type'] == 'gate' and section.get('retry_after'):
                rows.append([{'type': 'callback', 'text': f"Подождите {section['retry_after']} с", 'payload': 'm:0'}])
                continue
            rows.append([{
                "type": "callback", "text": label,
                "payload": _action("g" if button["type"] == "gate" else "a", section["branch_id"], button["id"]),
            }])
        if pages > 1:
            nav: list[dict[str, str]] = []
            if page > 0:
                nav.append({"type": "callback", "text": "← Назад", "payload": f"m:{page - 1}"})
            if page + 1 < pages:
                nav.append({"type": "callback", "text": "Далее →", "payload": f"m:{page + 1}"})
            rows.append(nav)
        rows.append([{"type": "callback", "text": "Обновить доступ", "payload": f"m:{page}"}])
        if self.workflows:
            for action in self.workflows.actions('max', user_id)[:20]:
                rows.append([{'type': 'link', 'text': action['label'][:128], 'url': action['url']} if action.get('url') else
                             {'type': 'callback', 'text': action['label'][:128], 'payload': action['callback']}])
        if not sections:
            rows.append([{"type": "request_contact", "text": "Поделиться номером"}])
            explanation = (
                "Чтобы найти ваши пропуски, нажмите «Поделиться номером». "
                "Номер должен совпадать с номером в записи на услугу. "
                "Можно также открыть персональную ссылку из уведомления."
            )
        elif not any(section.get("active") for section in sections):
            explanation = "Сейчас активных пропусков нет. Доступ появится в установленное время перед вашей записью. При необходимости свяжитесь с администратором."
        else:
            explanation = "Выберите ворота нужного филиала или свяжитесь с администратором."
            if pages > 1:
                explanation += f" Страница {page + 1} из {pages}."
        text = ((prefix + "\n\n") if prefix else "") + explanation
        return {"text": text[:4000], "attachments": _keyboard(rows)}

    def _send(self, update: IncomingUpdate, text: str, *, attachments: Any = None) -> None:
        target = {"chat_id": update.chat_id} if update.chat_id is not None else {"user_id": int(update.user_id)}
        self.api.send_message(text=text[:4000], attachments=attachments, **target)

    def _send_menu(self, update: IncomingUpdate, *, prefix: str = "") -> None:
        menu = self.menu(update.user_id, prefix=prefix)
        previous = self.state.get_checkpoint(self.namespace, 'menu:' + update.user_id, {})
        if previous.get('message_id') and hasattr(self.api, 'edit_message'):
            try:
                self.api.edit_message(previous['message_id'], **menu)
                self._remember_menu(update.user_id, previous['message_id'], menu)
                return
            except MaxApiError as error:
                if error.status_code != 404:
                    raise
        target = {'chat_id': update.chat_id} if update.chat_id is not None else {'user_id': int(update.user_id)}
        response = self.api.send_message(**menu, **target)
        mid = response.get('message', {}).get('body', {}).get('mid') if isinstance(response, dict) else None
        if isinstance(mid, str) and mid:
            self._remember_menu(update.user_id, mid, menu)

    def _remember_menu(self, user_id, message_id, menu):
        from bot_menu import fingerprint, next_refresh
        self.state.set_checkpoint(self.namespace, 'menu:' + user_id, {
            'message_id': message_id, 'fingerprint': fingerprint(menu['text'], menu.get('attachments')),
            'refresh_at': next_refresh(self.service, 'max', user_id)})

    @staticmethod
    def _workflow_menu(view):
        rows = [[{'type': 'link', 'text': a['label'], 'url': a['url']} if a.get('url') else
                 {'type': 'callback', 'text': a['label'], 'payload': a['callback']}] for a in view.get('actions', [])]
        rows.append([{'type': 'callback', 'text': 'Главное меню', 'payload': 'm:0'}])
        return {'text': view['text'][:4000], 'attachments': _keyboard(rows)}

    def refresh_menus(self):
        from bot_menu import fingerprint
        count = 0
        for key, previous in self.state.checkpoints(self.namespace, 'menu:'):
            if not previous.get('message_id') or previous.get('refresh_at', 0) > time.time():
                continue
            user_id = key[5:]
            menu = self.menu(user_id)
            try:
                if fingerprint(menu['text'], menu.get('attachments')) != previous.get('fingerprint'):
                    self.api.edit_message(previous['message_id'], **menu)
                self._remember_menu(user_id, previous['message_id'], menu)
            except MaxApiError as error:
                if error.status_code == 404:
                    self.state.set_checkpoint(self.namespace, key, {})
                else:
                    raise
            count += 1
            if count >= 10:
                break

    def _cancel_gate_update(self, raw: Any) -> None:
        update = normalize_update(raw)
        if update and update.kind == "message_callback" and update.callback_payload.startswith("g:"):
            if self.state.claim(self.namespace, update.event_key, previous_key=update.previous_key):
                self.state.finish(self.namespace, update.event_key, "cancelled", "stopping")

    def process_update(self, raw: Any, *, stop_event: threading.Event | None = None) -> None:
        update = normalize_update(raw)
        if update is None:
            return
        if stop_event is not None and stop_event.is_set():
            self._cancel_gate_update(raw)
            return
        # Claim ALL updates before sending, acknowledging or binding. A POST
        # timeout has an ambiguous outcome and must not send a second welcome/
        # menu on replay. GET /updates failures happen before this claim.
        if not self.state.claim(self.namespace, update.event_key, previous_key=update.previous_key):
            return
        try:
            self._dispatch(update, stop_event=stop_event)
        except _Stopping:
            self.state.finish(self.namespace, update.event_key, "cancelled", "stopping")
            _audit("BOT_COMMAND_CANCELLED", reason="stopping")
            return
        except MaxApiError as exc:
            outcome = "unknown" if exc.status_code is None else "failed"
            self.state.finish(self.namespace, update.event_key, outcome, exc.code)
            _audit("BOT_UPDATE_FAILED", level="ERROR", event_kind=update.kind,
                   event_ref=update.event_key[:16], outcome=outcome,
                   api_code=exc.code, http_status=exc.status_code)
            raise
        except Exception as exc:
            self.state.finish(self.namespace, update.event_key, "unknown", type(exc).__name__)
            _audit("BOT_UPDATE_FAILED", level="ERROR", event_kind=update.kind,
                   event_ref=update.event_key[:16], outcome="unknown", error_type=type(exc).__name__)
            raise
        self.state.finish(self.namespace, update.event_key, "handled")

    def _dispatch(self, update: IncomingUpdate, *, stop_event: threading.Event | None = None) -> None:
        if stop_event is not None and stop_event.is_set():
            raise _Stopping()
        if update.kind == "message_callback":
            self._handle_callback(update, stop_event=stop_event)
            return
        if update.kind == "bot_started" or update.text.strip().split(" ", 1)[0].lower() == "/start":
            token = update.start_payload
            if not token and update.text.strip().startswith("/start "):
                token = update.text.strip().split(maxsplit=1)[1]
            prefix = ""
            if token:
                result = self.service.bind_token("max", update.user_id, token)
                prefix = result.get("message", "")
            self._send_menu(update, prefix=prefix)
        elif any(a.get("type") == "contact" for a in update.attachments):
            phone = verified_contact_phone(update, self._token)
            if phone is None:
                self._send(update, "Не удалось подтвердить номер. Используйте кнопку «Поделиться номером» в этом боте.", attachments=_keyboard([
                    [{"type": "request_contact", "text": "Поделиться номером"}]
                ]))
            else:
                result = self.service.bind_verified_phone("max", update.user_id, phone)
                self._send_menu(update, prefix=result.get("message", ""))
        else:
            prefix = ""
            if self.workflows:
                try:
                    view = self.workflows.message('max', update.user_id, update.text)
                except (ValueError, PermissionError) as error:
                    view = {'text': str(error), 'actions': []}
                if view:
                    menu = self._workflow_menu(view)
                    previous = self.state.get_checkpoint(self.namespace, 'menu:' + update.user_id, {})
                    if previous.get('message_id'):
                        self.api.edit_message(previous['message_id'], **menu)
                    else:
                        self._send(update, **menu)
                    return
            if update.text.strip().lower() not in {"/menu", "/help", "меню", "помощь", ""}:
                prefix = "Для управления используйте кнопки. Введённый вручную номер телефона не подтверждает владельца."
            self._send_menu(update, prefix=prefix)

    def _handle_callback(self, update: IncomingUpdate, *, stop_event: threading.Event | None = None) -> None:
        answer = {"chat_id": update.chat_id or update.user_id}
        value = update.callback_payload
        if value.startswith('x:') and self.workflows:
            try:
                view = self.workflows.action('max', update.user_id, value)
            except (ValueError, PermissionError) as error:
                view = {'text': str(error), 'actions': []}
            self.api.answer_callback(update.callback_id, message=self._workflow_menu(view), **answer)
            previous = self.state.get_checkpoint(self.namespace, 'menu:' + update.user_id, {})
            if previous:
                previous['refresh_at'] = time.time() + 300
                self.state.set_checkpoint(self.namespace, 'menu:' + update.user_id, previous)
            return
        if re.fullmatch(r"m:\d{1,6}", value):
            menu = self.menu(update.user_id, page=int(value[2:]))
            if update.has_message:
                self.api.answer_callback(update.callback_id, message=menu, **answer)
            else:
                self.api.answer_callback(update.callback_id, notification="Меню обновлено", **answer)
                self._send(update, **menu)
            return
        match = re.fullmatch(r"([ga]):([A-Za-z0-9_-]{1,24}):([A-Za-z0-9_-]{1,32})", value)
        if match is None or not update.has_message or (match.group(1) == 'g' and not update.message_id):
            self.api.answer_callback(update.callback_id, notification="Кнопка устарела. Обновите меню командой /menu.", **answer)
            return
        kind, branch_id, button_id = match.groups()
        if kind == "a":
            for section in self.service.menu_for_identity("max", update.user_id):
                if str(section["branch_id"]) == branch_id:
                    for button in section.get("buttons", []):
                        if str(button["id"]) == button_id and button.get("type") == "admin":
                            if hasattr(self.service, '_emit'):
                                self.service._emit('admin_contact_requested', branch_id, event_id=update.event_key)
                            self.api.answer_callback(update.callback_id, **answer)
                            self._send(update, f"{section['branch_name']}\n{button['label']}: {button['phone']}")
                            return
            self.api.answer_callback(update.callback_id, notification="Контакт филиала недоступен. Обновите меню.", **answer)
            return
        age = self._clock() - (update.timestamp_ms / 1000) if update.timestamp_ms is not None else None
        if age is None or age > CALLBACK_MAX_AGE_SECONDS or age < -30:
            self.api.answer_callback(update.callback_id, notification="Команда устарела. Нажмите кнопку ещё раз.", **answer)
            return
        self.api.answer_callback(update.callback_id, notification='Проверяю доступ…', **answer)
        if stop_event is not None and stop_event.is_set():
            raise _Stopping()
        result = self.service.open_for_identity("max", update.user_id, branch_id, button_id)
        _audit("BOT_GATE_RESULT", branch_id=branch_id, status=result.get("status", "unknown"), command_id=result.get("command_id"))
        menu = self.menu(update.user_id, prefix=result.get('message') or 'Команда обработана')
        if update.message_id and hasattr(self.api, 'edit_message'):
            self.api.edit_message(update.message_id, **menu)
        else:
            self._send(update, **menu)
        if update.message_id:
            self._remember_menu(update.user_id, update.message_id, menu)

    def notify_access(self, payload: dict[str, Any]) -> bool:
        """Called by the durable outbox; failures propagate as delivery evidence."""
        from pass_notifications import timely_grant
        user_id = payload.get("max_id")
        if not user_id or not timely_grant(payload.get('event'), payload):
            return False
        try:
            recipient = int(user_id)
        except (TypeError, ValueError):
            return False
        if recipient <= 0:
            return False
        prefix = "Вам предоставлен доступ на парковку."
        self.api.send_message(user_id=recipient, **self.menu(str(recipient), prefix=prefix))
        return True

    def poll_once(self, *, stop_event: threading.Event | None = None) -> None:
        marker = self.state.get_checkpoint(self.namespace, "marker")
        batch = self.api.get_updates(marker=marker)
        for index, update in enumerate(batch["updates"]):
            if stop_event is not None and stop_event.is_set():
                # Cancel received physical commands, but retain non-physical
                # events for next startup by leaving the checkpoint unchanged.
                for pending in batch["updates"][index:]:
                    self._cancel_gate_update(pending)
                return
            try:
                self.process_update(update, stop_event=stop_event)
            except MaxApiError as exc:
                if exc.status_code not in {400, 403, 404}:
                    raise
                # A blocked user/deleted callback must not stall updates for all
                # branches. Retryable network/server errors retain the marker.
                _audit("BOT_DELIVERY_REJECTED", level="WARNING", http_status=exc.status_code, api_code=exc.code)
        # The marker is opaque: never add one or commit it before handlers finish.
        if batch.get("marker") is not None and not (stop_event is not None and stop_event.is_set()):
            self.state.set_checkpoint(self.namespace, "marker", batch["marker"])

    def _drain_webhooks(self, inbox, stop_event):
        while inbox.wake.is_set() and not stop_event.is_set():
            data=inbox.take()
            if data is not None:
                try:
                    self.process_update(data,stop_event=stop_event)
                except Exception as error:
                    _audit("BOT_PROCESSING_ERROR",level="ERROR",error_type=type(error).__name__)

    def run(self, stop_event: threading.Event, inbox=None) -> None:
        if stop_event.is_set():
            return
        from bot_webhooks import BotInbox
        if inbox is None:
            inbox=BotInbox('max',self._token,self.state)
        _RUNNING.clear()
        _READY.clear()
        try:
            subscriptions = self.api.get_subscriptions()
        except MaxApiError as exc:
            exc.stage = "subscriptions"
            raise
        urls=[sub.get('url') if isinstance(sub,dict) else None for sub in subscriptions]
        from bot_diagnostics import BotCheckError
        try:
            previous=inbox.check_ownership(urls)
        except BotCheckError:
            _audit("BOT_START_REFUSED",level="ERROR",reason="active_webhook")
            raise MaxApiError("active_webhook",stage="subscriptions") from None
        try:
            self.api.get_me()
        except MaxApiError as exc:
            exc.stage = "get_me"
            raise
        if inbox.url:
            from max_api import UPDATE_TYPES
            remote_types = subscriptions[0].get('update_types') if subscriptions else None
            if urls!=[inbox.url] or previous!=inbox.registration(True) or (remote_types and set(remote_types)!=set(UPDATE_TYPES)):
                # MAX's subscription identifies the URL. Re-create only a
                # checked URL when its address or secret has changed.
                for url in urls:
                    self.api.unsubscribe(url)
                inbox.remember(inbox.registration(False))
                self.api.subscribe(inbox.url,inbox.secret)
                inbox.remember(inbox.registration(True))
            _RUNNING.set()
            _READY.set()
            _audit("BOT_WEBHOOK_READY")
            try:
                while not stop_event.is_set():
                    if inbox.wake.wait(0.25):
                        self._drain_webhooks(inbox,stop_event)
            finally:
                _READY.clear()
                _RUNNING.clear()
                _audit("BOT_WEBHOOK_STOPPED")
            return
        for url in urls:
            self.api.unsubscribe(url)
        if previous:
            inbox.remember({})
        self._drain_webhooks(inbox,stop_event)
        _RUNNING.set()
        _audit("BOT_POLLING_STARTED")
        failures = 0
        try:
            while not stop_event.is_set():
                started = time.monotonic()
                try:
                    self.poll_once(stop_event=stop_event)
                    failures = 0
                    if not stop_event.is_set():
                        if not _READY.is_set():
                            _audit("BOT_POLL_READY")
                        _READY.set()
                except MaxApiError as exc:
                    _READY.clear()
                    if exc.status_code in {401, 403} or exc.code in {
                        "tls_trust_failed", "ca_bundle_invalid", "token_invalid_format",
                    }:
                        raise
                    failures += 1
                    _audit("BOT_POLL_ERROR", level="WARNING", api_code=exc.code,
                           http_status=exc.status_code, stage=exc.stage)
                    stop_event.wait(min(2 ** min(failures, 5), 30))
                except Exception as exc:
                    _READY.clear()
                    failures += 1
                    # Never log raw updates, exception text or traceback local values.
                    _audit("BOT_PROCESSING_ERROR", level="ERROR", error_type=type(exc).__name__)
                    stop_event.wait(min(2 ** min(failures, 5), 30))
                stop_event.wait(max(0.0, 0.3 - (time.monotonic() - started)))
        finally:
            _READY.clear()
            _RUNNING.clear()
            _audit("BOT_POLLING_STOPPED")


def create_bot(service: Any = None) -> MaxBot:
    from storage import config
    from access_service import AccessService

    token = config.MAX_BOT_TOKEN
    api = MaxApiClient(token, base_url=config.MAX_API_BASE_URL, ca_bundle=config.MAX_CA_BUNDLE)
    state = BotStateStore(Path(config.DATA_DIR) / "bot_state.sqlite3")
    return MaxBot(api, service if service is not None else AccessService(), state, token=token)


def run_delivery(stop_event: threading.Event | None = None, *, service: Any = None, inbox=None) -> None:
    from storage import config

    if not config.MAX_BOT_ENABLED:
        raise RuntimeError("MAX_BOT_ENABLED=false: бот отключён в локальной конфигурации.")
    if not config.MAX_BOT_TOKEN:
        raise ValueError("MAX_BOT_TOKEN is required")
    global _RUNTIME
    stop_event = stop_event or threading.Event()
    try:
        with polling_lease("max", config.MAX_BOT_TOKEN):
            _READY.clear()
            _RUNNING.clear()
            runtime = create_bot(service=service)
            _RUNTIME = runtime
            try:
                from bot_webhooks import BotInbox
                runtime.run(stop_event,inbox or BotInbox.configured('max'))
            except MaxApiError as exc:
                _audit("BOT_RUNTIME_ERROR", level="ERROR", api_code=exc.code,
                       http_status=exc.status_code, stage=exc.stage)
                raise
            finally:
                _READY.clear()
                _RUNNING.clear()
                _RUNTIME = None
    except PollingAlreadyRunning:
        _audit("BOT_START_REFUSED", level="ERROR", reason="polling_already_running")
        raise


def run_polling(stop_event: threading.Event | None = None, *, service: Any = None) -> None:
    from storage import config
    if config.MAX_BOT_MODE!='polling':
        raise RuntimeError('Для запуска polling выберите режим polling в системных настройках')
    run_delivery(stop_event, service=service)


def notify_access(payload: dict[str, Any]) -> bool:
    if not payload.get("max_id"):
        return False
    if _RUNTIME is None or not _READY.is_set():
        raise RuntimeError("MAX bot is not running")
    try:
        return _RUNTIME.notify_access(payload)
    except MaxApiError as exc:
        _audit("BOT_NOTIFICATION_ERROR", level="ERROR", branch_id=payload.get("branch_id"),
               api_code=exc.code, http_status=exc.status_code)
        raise


def notify_text(user_id, text, *, actions=None):
    if _RUNTIME is None or not _READY.is_set():
        return False
    try:
        for start in range(0, len(text), 3500):
            options = {}
            if actions and start+3500 >= len(text):
                options['attachments'] = _keyboard([[{'type':'callback','text':a['label'],'payload':a['callback']}] for a in actions])
            _RUNTIME.api.send_message(user_id=int(user_id), text=text[start:start+3500], **options)
    except MaxApiError as exc:
        # Recipient IDs and message bodies never belong in operational logs.
        _audit("BOT_NOTIFICATION_ERROR", level="ERROR", api_code=exc.code,
               http_status=exc.status_code)
        raise
    return True


def refresh_menus():
    if _RUNTIME is not None and _READY.is_set():
        _RUNTIME.refresh_menus()


def status() -> dict[str, Any]:
    from storage import config
    return {"running": _RUNNING.is_set(), "ready": _READY.is_set(), "mode": config.MAX_BOT_MODE}


def register_webhook(app: Any, bot: MaxBot, *, secret: str, enqueue: Any, path: str = "/webhook/max") -> None:
    """Register a receiver only; callers must supply a durable inbox enqueue.

    This does not subscribe the live bot or start a worker. Production activation
    and reverse-proxy delivery remain a separate, explicit deployment step.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,256}", secret):
        raise ValueError("A valid MAX webhook secret is required")
    if not callable(enqueue):
        raise ValueError("A durable enqueue function is required")
    from bot_webhooks import HEADERS,receive
    app.add_url_rule(path, endpoint="gatebot_max_webhook", view_func=lambda:receive(secret,HEADERS['max'],enqueue), methods=["POST"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        run_polling()
    except KeyboardInterrupt:
        pass
