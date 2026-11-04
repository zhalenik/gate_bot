"""GateBot HTTP interface.

Importing this module creates routes only. The explicit runtime owns background
workers; all gate commands are authorized by the shared AccessService.
"""
import csv
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
import zipfile
from collections import deque
from datetime import date, timedelta
from urllib.parse import quote, urlsplit

from flask import (
    Flask, Response, abort, flash, g, jsonify, redirect, render_template,
    request, send_file, session, url_for,
)
from werkzeug.exceptions import HTTPException

from access_service import AccessService
from admin_auth import AdminAuth
from branch_manager import BranchManager
from link_manager import LinkManager, access_is_active, normalize_phone
from storage import config
from timeutil import end_of_day, now, parse_datetime, timezone
import logger

app = Flask(__name__)
app.config.update(
    SECRET_KEY=config.SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=config.SESSION_COOKIE_SECURE,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=1024 * 1024,
)
LINKS_FILE = os.path.join(config.DATA_DIR, "links.json")
ADMINS_FILE = os.path.join(config.DATA_DIR, "admins.json")
link_manager = LinkManager(LINKS_FILE)
branch_manager = BranchManager(os.path.join(config.DATA_DIR, "branches.json"))
admin_auth = AdminAuth(ADMINS_FILE, branch_exists=lambda branch_id: branch_manager.get_branch(branch_id) is not None)
access_service = AccessService(link_manager, branch_manager)
_login_attempts = {}
_login_lock = threading.Lock()


def _csrf_token():
    value = session.get("csrf_token")
    if not isinstance(value, str) or len(value) < 32:
        value = secrets.token_urlsafe(32)
        session["csrf_token"] = value
    return value


@app.context_processor
def inject_context():
    return {"BASE_URL": config.BASE_URL, "csrf_token": _csrf_token}


@app.before_request
def protect_request():
    g.correlation_id = secrets.token_hex(12)
    logger.set_correlation_id(g.correlation_id)
    if request.path.startswith("/admin/api/") and not admin_auth.is_logged_in():
        return jsonify(status="error", message="Требуется вход в систему"), 401
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    # These disabled integration endpoints never trust or persist request data.
    if request.path.startswith("/webhook/yclients") or request.path == "/yclients/register":
        return None
    supplied = request.headers.get("X-CSRF-Token")
    if supplied is None and request.mimetype in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        supplied = request.form.get("csrf_token")
    expected = session.get("csrf_token")
    if not isinstance(supplied, str) or not isinstance(expected, str) or not hmac.compare_digest(supplied, expected):
        abort(403, description="Сессия формы истекла. Обновите страницу и повторите действие")


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Request-ID"] = getattr(g, "correlation_id", "")
    response.headers["Cache-Control"] = "no-store"
    return response


def _json_body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400, description="Ожидается JSON-объект")
    return data


def _branch_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        abort(400, description="Некорректный филиал")
    if isinstance(value, str) and not value.isascii():
        abort(400, description="Некорректный филиал")
    try:
        parsed = int(value)
    except (ValueError, TypeError):
        abort(400, description="Некорректный филиал")
    if parsed < 1:
        abort(400, description="Некорректный филиал")
    return parsed


def _admin():
    admin = admin_auth.get_current_admin()
    if not admin:
        abort(401, description="Требуется вход в систему")
    return admin


def _scope():
    admin = _admin()
    return None if admin.get("is_super") is True else _branch_id(admin.get("branch_id"))


def _choose_branch(value=None):
    scope = _scope()
    branch_id = scope if value is None and scope is not None else _branch_id(value)
    if scope is not None and branch_id != scope:
        abort(403, description="Нет доступа к этому филиалу")
    branch = branch_manager.get_branch(branch_id)
    if branch is None:
        abort(400, description="Филиал не настроен")
    return branch


def _token(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        abort(400, description="Некорректная ссылка")
    return value


def _scoped_link(value):
    token = _token(value)
    user = link_manager.load_links().get(token)
    scope = _scope()
    # Identical response for absent objects and other tenants prevents enumeration.
    if not isinstance(user, dict) or (scope is not None and str(user.get("branch_id")) != str(scope)):
        abort(404, description="Пользователь не найден")
    if branch_manager.get_branch(user.get("branch_id")) is None:
        abort(400, description="Филиал пользователя не настроен")
    return token, user


def _boolean(value, label):
    if type(value) is not bool:
        abort(400, description=f"Поле «{label}» должно иметь значение true или false")
    return value


def _days(value):
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 365:
        abort(400, description="Срок должен быть целым числом от 1 до 365 дней")
    return value


def _phone(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 40:
        abort(400, description="Некорректный номер телефона")
    normalized = normalize_phone(value)
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", normalized):
        abort(400, description="Введите телефон с кодом страны")
    return normalized


def _text(value, name, required=False, max_length=200):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        abort(400, description=f"Некорректное поле «{name}»")
    value = value.strip()
    if len(value) > max_length or (required and not value) or any(ord(ch) < 32 for ch in value):
        abort(400, description=f"Некорректное поле «{name}»")
    return value or None


def _external_id(value, name):
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        abort(400, description=f"Некорректное поле «{name}»")
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,20}", value) or int(value) < 1:
        abort(400, description=f"Некорректное поле «{name}»")
    return value


def _audit(event, branch_id=None, **details):
    logger.log_admin_action(event, session.get("admin_username"), {
        "branch_id": branch_id, "correlation_id": getattr(g, "correlation_id", None), **details,
    })


def _full_link(token):
    base = config.BASE_URL.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        abort(503, description="Администратору нужно настроить BASE_URL")
    return base + url_for("open_barrier", token=token)


def _send_link(phone, token, user):
    if not config.OUTBOUND_ENABLED:
        return {"status": "disabled", "message": "Отправка уведомлений отключена"}
    try:
        import chatpush
        result = chatpush.send_link(phone, _full_link(token), user.get("user_name"))
        if isinstance(result, dict) and result.get("status") == "success":
            return {"status": "sent", "message": "Сообщение принято сервисом отправки"}
    except Exception:
        logger.log_error("LINK_DELIVERY_FAILED", "Notification transport failed", {"branch_id": user.get("branch_id")})
    return {"status": "failed", "message": "Сервис отправки не подтвердил прием сообщения"}


def _admin_template(template):
    admin = _admin()
    branch_id = None if admin.get("is_super") is True else _branch_id(admin.get("branch_id"))
    return render_template(template, is_super=admin.get("is_super") is True,
                           admin_branch_id=branch_id,
                           admin_branch_name=branch_manager.get_branch_name(branch_id) if branch_id else None)


@app.route("/opn/<token>")
def open_barrier(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
        abort(403)
    user = link_manager.verify_token(token)
    if not user:
        # An expired known pass still identifies its own branch for support.
        # This lookup grants no access and never falls back to another branch.
        known_user = link_manager.load_links().get(token)
        branch = branch_manager.get_branch(known_user.get("branch_id")) if isinstance(known_user, dict) else None
        if branch is not None:
            contacts = []
            for button in branch.buttons:
                if button.type != "admin":
                    continue
                phone = normalize_phone(button.phone)
                if re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                    contacts.append({"label": button.label, "phone": phone})
            return render_template("403.html", branch_name=branch.name, admin_contacts=contacts), 403
        abort(403)
    branch = branch_manager.get_branch(user.get("branch_id"))
    if branch is None or not branch.enabled:
        abort(403)
    buttons = []
    for button in branch.buttons:
        if button.type == "gate":
            buttons.append({"id": button.id, "type": "gate", "label": button.label})
        elif button.type == "admin":
            phone = normalize_phone(button.phone)
            if re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                buttons.append({"type": "admin", "label": button.label, "phone": phone})
    def bot_url(platform, username):
        username = (username or "").lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", username):
            return None
        return f"https://{platform}/{username}?start={quote(token, safe='')}"
    logo = branch.logo or ""
    if logo and not (logo.startswith("/static/") or urlsplit(logo).scheme == "https"):
        logo = ""
    return render_template("access.html", user_name=user.get("user_name", "Клиент"),
                           branch_name=branch.name, branch_logo=logo, buttons=buttons,
                           token=token, timeout=config.timeout,
                           telegram_url=bot_url("t.me", config.BOT_USERNAME),
                           max_url=bot_url("max.ru", config.MAX_BOT_USERNAME))


@app.route("/open-gate", methods=["POST"])
def open_gate():
    data = _json_body()
    token = _token(data.get("token"))
    button_id = data.get("button_id")
    if not isinstance(button_id, str) or not button_id or len(button_id) > 128 or "phone" in data:
        abort(400, description="Выберите настроенные ворота филиала")
    result = dict(access_service.open_for_token(token, button_id))
    status_code = result.pop("http_status", 200 if result.get("status") == "success" else 403)
    result.setdefault("confirmed", False)
    return jsonify(result), status_code


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        key = (request.remote_addr or "", username[:200])
        moment = time.monotonic()
        with _login_lock:
            # Bounded in-memory throttling for the supported single runtime.
            for old_key in list(_login_attempts):
                while _login_attempts[old_key] and moment - _login_attempts[old_key][0] > 300:
                    _login_attempts[old_key].popleft()
                if not _login_attempts[old_key]:
                    del _login_attempts[old_key]
            attempts = _login_attempts.setdefault(key, deque(maxlen=10))
            blocked = len(attempts) >= 10 or len(_login_attempts) > 5000
        if blocked:
            flash("Слишком много попыток. Повторите вход через несколько минут", "error")
            return render_template("admin/login.html"), 429
        if admin_auth.login_admin(username, request.form.get("password")):
            with _login_lock:
                _login_attempts.pop(key, None)
            _audit("LOGIN", _scope())
            return redirect(url_for("admin_users"))
        with _login_lock:
            _login_attempts.setdefault(key, deque(maxlen=10)).append(moment)
        flash("Неверное имя пользователя или пароль", "error")
        return render_template("admin/login.html"), 401
    return render_template("admin/login.html")


@app.route("/admin/logout", methods=["POST"])
@admin_auth.require_auth
def admin_logout():
    _audit("LOGOUT", _scope())
    admin_auth.logout_admin()
    return redirect(url_for("admin_login"))


@app.route("/admin/users")
@admin_auth.require_auth
def admin_users():
    return _admin_template("admin/users.html")


@app.route("/admin/add-user")
@admin_auth.require_auth
def admin_add_user():
    return _admin_template("admin/create-link.html")


@app.route("/admin/create-link")
@admin_auth.require_auth
def admin_create_link_legacy():
    return redirect(url_for("admin_add_user"))


@app.route("/admin/temp-link")
@admin_auth.require_auth
def admin_temp_link():
    return _admin_template("admin/temp-link.html")


@app.route("/admin/change-password")
@admin_auth.require_auth
def admin_change_password():
    return _admin_template("admin/change-password.html")


@app.route("/admin/logs")
@admin_auth.require_auth
def admin_logs():
    return _admin_template("admin/logs.html")


@app.route("/admin/settings")
@admin_auth.require_auth
def admin_settings():
    return _admin_template("admin/settings.html")


def _public_branch_settings(branch):
    return {
        "branch_id": branch.branch_id, "name": branch.name, "timezone": branch.timezone,
        "enabled": branch.enabled, "logo": branch.logo,
        "buttons": [{"type": button.type, "label": button.label, "phone": button.phone}
                    for button in branch.buttons],
    }


def _logo_url(value):
    value = _text("" if value is None else value, "Логотип", max_length=2048) or ""
    if not value:
        return ""
    parsed = urlsplit(value)
    local = value.startswith("/static/") and ".." not in parsed.path.split("/") and not parsed.netloc
    remote = parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password
    if not (local or remote) or "\\" in value:
        abort(400, description="Логотип должен быть локальным /static/ файлом или HTTPS-ссылкой без учетных данных")
    return value


@app.route("/admin/api/branch-settings", methods=["GET", "POST"])
@admin_auth.require_auth
def api_branch_settings():
    scope = _scope()
    if request.method == "GET":
        value = request.args.get("branch_id")
        if value is None and scope is None:
            return jsonify(settings=None)
        branch = _choose_branch(value)
        return jsonify(settings=_public_branch_settings(branch))
    data = _json_body()
    allowed = {"branch_id", "create", "name", "timezone", "enabled", "logo", "buttons"}
    if set(data) - allowed:
        abort(400, description="Недопустимые поля настроек филиала")
    branch_id = _branch_id(data.get("branch_id", scope))
    create = _boolean(data.get("create", False), "Создать филиал")
    if scope is not None and (create or branch_id != scope):
        abort(403, description="Нет прав на изменение этого филиала")
    existing = branch_manager.get_branch(branch_id)
    if create and existing:
        abort(409, description="Филиал с таким ID уже существует")
    if not create and existing is None:
        abort(404, description="Филиал не найден")
    zone = _text(data.get("timezone"), "Часовой пояс", required=True, max_length=80)
    try:
        timezone(zone)
    except (KeyError, ValueError):
        abort(400, description="Неизвестный часовой пояс")
    buttons = data.get("buttons")
    if not isinstance(buttons, list) or len(buttons) > 12:
        abort(400, description="Можно настроить до 12 кнопок")
    validated_buttons = []
    for item in buttons:
        if not isinstance(item, dict) or set(item) != {"type", "label", "phone"} or item.get("type") not in {"gate", "admin"}:
            abort(400, description="Некорректная кнопка филиала")
        phone = _phone(item.get("phone"))
        if phone is None:
            abort(400, description="Для каждой кнопки нужен номер телефона")
        validated_buttons.append({"type": item["type"], "label": _text(item["label"], "Название кнопки", required=True, max_length=100), "phone": phone})
    settings = {"name": _text(data.get("name"), "Название филиала", required=True),
                "timezone": zone, "enabled": _boolean(data.get("enabled"), "Филиал активен"),
                "logo": _logo_url(data.get("logo")), "buttons": validated_buttons}
    try:
        updated = branch_manager.update_settings(branch_id, settings, create=create)
    except ValueError:
        abort(400, description="Проверьте настройки: номера кнопок одного типа не должны повторяться")
    if not updated:
        abort(404, description="Филиал не найден")
    _audit("BRANCH_CREATED" if create else "BRANCH_SETTINGS_UPDATED", branch_id, changed_fields=sorted(settings))
    return jsonify(status="success", settings=_public_branch_settings(branch_manager.get_branch(branch_id)))


def _users_in_scope():
    scope = _scope()
    return [user for user in link_manager.get_all_links()
            if scope is None or str(user.get("branch_id")) == str(scope)]


@app.route("/admin/api/users")
@admin_auth.require_auth
def api_get_users():
    users = []
    for user in _users_in_scope():
        user = dict(user)
        branch = branch_manager.get_branch(user.get("branch_id"))
        expires = user.get("access_expires")
        active = access_is_active(user) and branch is not None and branch.enabled
        days = "Бессрочная"
        if expires:
            try:
                expiry = parse_datetime(expires)
                current = now(branch.timezone if branch else config.TIMEZONE)
                days = max((expiry.astimezone(current.tzinfo).date() - current.date()).days, 0)
                active = active and expiry > current
            except (ValueError, TypeError):
                days, active = None, False
        user.update(branch_name=branch.name if branch else "Филиал не настроен",
                    access_expires_days=days, gate_access=active,
                    has_bot=bool(user.get("tg_id") or user.get("max_id")),
                    has_telegram=bool(user.get("tg_id")), has_max=bool(user.get("max_id")),
                    has_yclients=user.get("id_ycl") not in (None, ""))
        users.append(user)
    return jsonify(users=users)


@app.route("/admin/api/branches")
@admin_auth.require_auth
def api_get_branches():
    scope = _scope()
    return jsonify(branches=[{"id": b.branch_id, "name": b.name}
                             for b in branch_manager.get_all_branches()
                             if scope is None or str(b.branch_id) == str(scope)])


@app.route("/admin/api/toggle-access", methods=["POST"])
@admin_auth.require_auth
def api_toggle_access():
    data = _json_body()
    token, user = _scoped_link(data.get("token"))
    enabled = _boolean(data.get("enable"), "Доступ")
    if not link_manager.toggle_user_access(token, enabled):
        abort(404, description="Пользователь не найден")
    _audit("TOGGLE_ACCESS", user["branch_id"], enable=enabled)
    return jsonify(status="success")


@app.route("/admin/api/set-expires", methods=["POST"])
@admin_auth.require_auth
def api_set_expires():
    data = _json_body()
    token, user = _scoped_link(data.get("token"))
    days = _days(data.get("days"))
    if not link_manager.set_access_expires(token, days):
        abort(404, description="Пользователь не найден")
    _audit("SET_EXPIRES", user["branch_id"], days=days)
    return jsonify(status="success")


@app.route("/admin/api/create-link", methods=["POST"])
@admin_auth.require_auth
def api_create_link():
    data = _json_body()
    branch = _choose_branch(data.get("branch_id"))
    user = {"id_ycl": _external_id(data.get("id_ycl"), "YCLIENTS ID"),
            "user_name": _text(data.get("user_name"), "Имя", required=True),
            "phone": _phone(data.get("phone")), "branch_id": branch.branch_id,
            "mailing_enabled": False, "gate_access": True, "access_source": "manual"}
    send = _boolean(data.get("send_link", False), "Отправить ссылку")
    if send and not user["phone"]:
        abort(400, description="Для отправки нужен номер телефона")
    days = _days(data.get("expires_days"))
    token = link_manager.create_link(user, expires_days=days)
    _audit("CREATE_LINK", branch.branch_id, expires_days=days)
    delivery = _send_link(user["phone"], token, user) if send else {"status": "not_requested"}
    return jsonify(status="success" if delivery["status"] in {"sent", "not_requested"} else "partial",
                   created=True, token=token, link=url_for("open_barrier", token=token), delivery=delivery)


@app.route("/admin/api/create-temp-link", methods=["POST"])
@admin_auth.require_auth
def api_create_temp_link():
    data = _json_body()
    branch = _choose_branch(data.get("branch_id"))
    phone = _phone(data.get("phone"))
    send = _boolean(data.get("send_link", False), "Отправить ссылку")
    if send and not phone:
        abort(400, description="Для отправки нужен номер телефона")
    user = {"user_name": "Гость", "phone": phone, "branch_id": branch.branch_id,
            "mailing_enabled": False, "gate_access": True, "is_temporary": True,
            "access_source": "manual"}
    expiry = end_of_day(tz=getattr(branch, "timezone", None) or config.TIMEZONE)
    token = link_manager.create_link(user, expires_at=expiry)
    _audit("CREATE_TEMP_LINK", branch.branch_id, expires_at=expiry.isoformat())
    delivery = _send_link(phone, token, user) if send else {"status": "not_requested"}
    return jsonify(status="success" if delivery["status"] in {"sent", "not_requested"} else "partial",
                   created=True, token=token, link=url_for("open_barrier", token=token), delivery=delivery)


@app.route("/admin/api/send-link", methods=["POST"])
@admin_auth.require_auth
def api_send_link():
    data = _json_body()
    token, user = _scoped_link(data.get("token"))
    phone = _phone(data.get("phone", user.get("phone")))
    if not phone:
        abort(400, description="Для отправки нужен номер телефона")
    if "phone" in data and phone != user.get("phone"):
        link_manager.set_user_phone(token, phone)
    result = _send_link(phone, token, user)
    _audit("SEND_LINK", user["branch_id"], delivery_status=result["status"])
    ok = result["status"] == "sent"
    return jsonify(status="success" if ok else "error", phone=phone,
                   message=result["message"], delivery=result), 200 if ok else 503


@app.route("/admin/api/update-user", methods=["POST"])
@admin_auth.require_auth
def api_update_user():
    data = _json_body()
    token, user = _scoped_link(data.get("token"))
    updates = {}
    if "user_name" in data:
        updates["user_name"] = _text(data["user_name"], "Имя", required=True)
    if "phone" in data:
        updates["phone"] = _phone(data["phone"])
    if "tg_id" in data or "max_id" in data:
        abort(400, description="Привязка мессенджера выполняется пользователем через личную ссылку")
    for key in ("id_ycl",):
        if key in data:
            updates[key] = _external_id(data[key], key)
    if not updates:
        abort(400, description="Нет изменений")
    if not link_manager.update_user(token, updates):
        abort(404, description="Пользователь не найден")
    _audit("UPDATE_USER", user["branch_id"], changed_fields=sorted(updates))
    return jsonify(status="success")


@app.route("/admin/api/set-mailing", methods=["POST"])
@admin_auth.require_auth
def api_set_mailing():
    data = _json_body()
    token, user = _scoped_link(data.get("token"))
    enabled = _boolean(data.get("enabled"), "Рассылка")
    if not link_manager.set_mailing_enabled(token, enabled):
        abort(404, description="Пользователь не найден")
    _audit("SET_MAILING", user["branch_id"], mailing_enabled=enabled)
    return jsonify(status="success")


@app.route("/admin/api/delete-user", methods=["POST"])
@admin_auth.require_auth
def api_delete_user():
    token, user = _scoped_link(_json_body().get("token"))
    if not link_manager.delete_link(token):
        abort(404, description="Пользователь не найден")
    _audit("DELETE_USER", user["branch_id"])
    return jsonify(status="success")


@app.route("/admin/api/change-password", methods=["POST"])
@admin_auth.require_auth
def api_change_password():
    data = _json_body()
    branch_id = _scope()
    try:
        ok = admin_auth.change_password(session["admin_username"], data.get("old_password"), data.get("new_password"))
    except ValueError as exc:
        abort(400, description=str(exc))
    if not ok:
        abort(400, description="Неверный текущий пароль")
    _audit("CHANGE_PASSWORD", branch_id)
    return jsonify(status="success", csrf_token=_csrf_token())


@app.route("/admin/api/export-csv")
@admin_auth.require_auth
def api_export_csv():
    headers = ["token", "id_ycl", "user_name", "phone", "tg_id", "max_id", "mailing_enabled", "branch_id", "gate_access", "access_expires", "created"]
    output = io.StringIO(newline="")
    output.write("\ufeffsep=;\r\n")
    writer = csv.writer(output, delimiter=";", quoting=csv.QUOTE_ALL)
    writer.writerow(headers)
    def safe_cell(value):
        text = "" if value is None else str(value)
        if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
            text = "'" + text
        return text
    for user in _users_in_scope():
        writer.writerow([safe_cell(user.get(key)) for key in headers])
    _audit("EXPORT_CSV", _scope())
    return Response(output.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=users.csv"})


def _date_filter(value):
    if not value:
        return None
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        abort(400, description="Дата должна иметь формат ГГГГ-ММ-ДД")


@app.route("/admin/api/logs")
@admin_auth.require_auth
def api_get_logs():
    log_type = request.args.get("type", "all")
    if log_type not in {"all", "admin", "user", "error", "sim800"}:
        abort(400, description="Неизвестный тип событий")
    date_from, date_to = _date_filter(request.args.get("from")), _date_filter(request.args.get("to"))
    if date_from and date_to and date_from > date_to:
        abort(400, description="Начальная дата должна быть не позже конечной")
    scope = _scope()
    view_branch = branch_manager.get_branch(scope) if scope else None
    view_timezone = timezone(view_branch.timezone if view_branch else config.TIMEZONE)
    admin_events = {"LOGIN", "LOGOUT", "TOGGLE_ACCESS", "SET_EXPIRES", "CREATE_LINK", "CREATE_TEMP_LINK",
                    "SEND_LINK", "UPDATE_USER", "SET_MAILING", "DELETE_USER", "CHANGE_PASSWORD", "EXPORT_CSV",
                    "DOWNLOAD_LOGS_ZIP", "BRANCH_CREATED", "BRANCH_SETTINGS_UPDATED"}
    logs = []
    for event in logger.read_events(branch_id=scope, limit=500):
        if scope is not None and str(event.get("branch_id")) != str(scope):
            continue
        try:
            timestamp = parse_datetime(event.get("ts")).astimezone(view_timezone)
        except (TypeError, ValueError):
            continue
        if date_from and timestamp.date() < date_from or date_to and timestamp.date() > date_to:
            continue
        level = str(event.get("level", "INFO")).upper()
        kind = str(event.get("event", ""))
        actor = str(event.get("actor") or "")
        actor_type = event.get("actor_type") or event.get("details", {}).get("actor_type")
        if not actor_type:
            actor_type = "admin" if kind in admin_events else ("user" if actor else "system")
        if log_type == "error" and level not in {"ERROR", "CRITICAL"}:
            continue
        if log_type == "sim800" and "SIM800" not in kind.upper():
            continue
        if log_type == "admin" and actor_type != "admin":
            continue
        if log_type == "user" and actor_type != "user":
            continue
        message = kind + " | " + json.dumps(event.get("details", {}), ensure_ascii=False, sort_keys=True)
        logs.append({"time": timestamp.isoformat(timespec="seconds"), "logger": actor or "system",
                     "level": level, "message": message, "source": "audit",
                     "correlation_id": event.get("correlation_id")})
    return jsonify(logs=logs)


@app.route("/admin/api/logs-zip")
@admin_auth.require_super_admin
def api_logs_zip():
    # Export only structured, redacted events; never copy raw state/event spool files.
    events = logger.read_events(branch_id=None, limit=5000)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("events.jsonl", "\n".join(json.dumps(e, ensure_ascii=False) for e in events))
    stream.seek(0)
    _audit("DOWNLOAD_LOGS_ZIP")
    return send_file(stream, mimetype="application/zip", as_attachment=True, download_name="gatebot-logs.zip")


@app.route("/admin/api/webhook-debug-stats")
@admin_auth.require_super_admin
def api_webhook_debug_stats():
    return jsonify(status="disabled", message="Сбор сырых вебхуков отключен"), 503


@app.route("/webhook/yclients", methods=["GET", "POST"])
@app.route("/webhook/yclients/debug", methods=["GET", "POST"])
def webhook_yclients():
    # The archived handler was not implemented. Do not manufacture a signed contract
    # or issue grants from an unverified payload. Polling provides read-only sync.
    return jsonify(status="disabled", message="Вебхуки YCLIENTS пока отключены; используется синхронизация на чтение"), 503


@app.route("/yclients/register", methods=["GET", "POST"])
def yclients_register_branch():
    return render_template("ycl_redirect_register.html", registration_enabled=False), 503


@app.route("/unsubscribe", methods=["GET", "POST"])
def unsubscribe():
    token = request.args.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
        abort(404)
    user = link_manager.load_links().get(token)
    if not isinstance(user, dict):
        abort(404)
    if request.method == "POST":
        if not link_manager.set_mailing_enabled(token, False):
            abort(404)
        logger.log_user_action("UNSUBSCRIBE", "web", {"branch_id": user.get("branch_id")})
        return render_template("unsubscribe_success.html", user_name=user.get("user_name", "Клиент"))
    return render_template("unsubscribe.html", user_name=user.get("user_name", "Клиент"), token=token)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/healthz")
def health():
    return jsonify(status="ok", component="web")


@app.errorhandler(HTTPException)
def http_error(error):
    if request.path.startswith(("/admin/api/", "/open-gate", "/webhook/")):
        return jsonify(status="error", message=error.description, correlation_id=getattr(g, "correlation_id", None)), error.code
    if error.code in {403, 404}:
        return render_template(f"{error.code}.html", error_message=error.description), error.code
    return error.get_response()


@app.errorhandler(Exception)
def unexpected_error(error):
    logger.log_error("HTTP_UNHANDLED", type(error).__name__, {"correlation_id": getattr(g, "correlation_id", None)})
    return jsonify(status="error", message="Не удалось выполнить действие. Сообщите администратору номер запроса",
                   correlation_id=getattr(g, "correlation_id", None)), 500


if __name__ == "__main__":
    print("Для запуска приложения используйте: python run.py")
