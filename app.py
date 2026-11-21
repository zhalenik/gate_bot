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
from branch_hours import access_hours_state
from admin_auth import AdminAuth
from branch_manager import BranchManager
from link_manager import LinkManager, access_is_active, normalize_phone
from marketplace_frame import FRAME_ANCESTORS, MarketplaceSessionInterface
from storage import config
from timeutil import now, parse_datetime, timezone
import logger

app = Flask(__name__)
app.session_interface = MarketplaceSessionInterface()
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
_product_cache = {}
_product_lock = threading.RLock()


def product_services():
    from application_services import ApplicationServices
    key = (str(link_manager.links_file), id(link_manager), id(branch_manager))
    with _product_lock:
        if key not in _product_cache:
            _product_cache[key] = ApplicationServices(link_manager, branch_manager)
        return _product_cache[key]


def product_provider():
    return product_services().provider(admin_auth, access_service)


def _csrf_token():
    value = session.get("csrf_token")
    if not isinstance(value, str) or len(value) < 32:
        value = secrets.token_urlsafe(32)
        session["csrf_token"] = value
    return value


@app.context_processor
def inject_context():
    from branding import BrandingSettings
    admin = admin_auth.get_current_admin()
    branch_ids = {str(v) for v in (admin or {}).get('branch_ids', [])}
    choices = [{'id': str(b.branch_id), 'name': b.name} for b in branch_manager.get_all_branches()
               if str(b.branch_id) in branch_ids] if len(branch_ids) > 1 else []
    active_branch = branch_manager.get_branch((admin or {}).get('branch_id'))
    pwa = None
    if request.endpoint in {'open_barrier', 'identity.preferences_page', 'pwa_gate', 'pwa_preferences'}:
        try:
            pwa = product_services().pwa.metadata((request.view_args or {}).get('token', ''))
            pwa.update(manifest_url=pwa['scope']+'manifest.webmanifest', allow_push=not pwa['guest'])
        except (ValueError, PermissionError):
            pass
    return {"BASE_URL": config.BASE_URL, "csrf_token": _csrf_token,
            'branding': BrandingSettings(product_services().identity).public_view(),
            'client_pwa': pwa, 'public_site_url': config.PUBLIC_SITE_URL if config.PUBLIC_SITE_URL.startswith(('http://','https://')) else '',
            'client_page': request.path.split('/', 2)[1] in {'opn','gate','join','preferences','pwa','verify-email','unsubscribe','shared'},
            'admin_yclients_enabled': bool((admin or {}).get('is_super') or (active_branch and active_branch.yclients_enabled)),
            'admin_sms_enabled': bool(active_branch and getattr(active_branch,'sms_notifications_enabled',False)),
            'admin_branch_choices': choices, 'active_branch_id': str((admin or {}).get('branch_id') or '')}


@app.before_request
def protect_request():
    g.correlation_id = secrets.token_hex(12)
    logger.set_correlation_id(g.correlation_id)
    if request.path.startswith("/admin/api/") and not admin_auth.is_logged_in():
        return jsonify(status="error", message="Требуется вход в систему"), 401
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    if request.endpoint == 'service_api.send_code':
        # This integration endpoint requires its own scoped Authorization header.
        return None
    # These disabled integration endpoints never trust or persist request data.
    if request.path.startswith("/webhook/yclients") or request.path == "/yclients/register":
        return None
    supplied = request.headers.get("X-CSRF-Token")
    if supplied is None and request.mimetype in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        supplied = request.form.get("csrf_token")
    expected = session.get("csrf_token")
    if not isinstance(supplied, str) or not isinstance(expected, str) or not hmac.compare_digest(supplied, expected):
        if request.is_json:
            return jsonify(status="error", code="csrf_expired",
                           message="Сессия формы истекла. Обновите страницу и повторите действие",
                           correlation_id=g.correlation_id), 403
        abort(403, description="Сессия формы истекла. Обновите страницу и повторите действие")


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.method == 'GET' and request.endpoint in {'yclients_marketplace.entry', 'yclients_marketplace.page'}:
        response.headers['Content-Security-Policy'] = FRAME_ANCESTORS
    else:
        response.headers['X-Frame-Options'] = 'DENY'
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Request-ID"] = getattr(g, "correlation_id", "")
    response.headers["Cache-Control"] = "no-cache" if request.endpoint == 'palette.public_styles' else "no-store"
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
    if not isinstance(value, str) or not re.fullmatch(r'[+0-9\s().-]{7,64}', value):
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


def _client_email(value):
    """Validate contact data without granting account/email verification."""
    from identity_service import valid_email
    if not isinstance(value, str):
        abort(400, description="Введите корректный email")
    try:
        return valid_email(value.strip())
    except ValueError as exc:
        abort(400, description=str(exc))


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


def _pass_delivery_channel(value):
    if value not in ('auto','messengers','sms','email'):
        abort(400,description='Выберите канал отправки ссылки')
    return value


def _send_link(phone, token, user, channel='auto'):
    from message_templates import pass_link
    if not config.outbound_enabled('client'):
        return {"status": "disabled", "message": "Отправка уведомлений отключена"}
    try:
        branch = branch_manager.get_branch(user.get('branch_id'))
        if branch is None:
            return {'status':'unavailable','message':'Филиал недоступен'}
        if channel=='auto':
            channel='messengers' if branch.yclients_enabled else 'sms'
        if channel=='email':
            email=_client_email(user.get('email',''))
            if not email:
                return {'status':'unavailable','message':'Укажите Email в карточке клиента для отправки ссылки.'}
            result=product_services().notifications.send('email',email,
                pass_link(_full_link(token)),
                {'branch_id':str(branch.branch_id),'event':'manual_pass','client_token':token,
                 'idempotency_key':secrets.token_hex(16)})
            if result.get('status') in {'success','accepted','sent'}:
                return {'status':'sent','message':'Письмо со ссылкой принято почтовым сервисом'}
            return {'status':'unknown' if result.get('status')=='unknown' else 'failed',
                    'message':result.get('message') or 'Не удалось отправить письмо. Проверьте Email и настройку почты сервиса.'}
        if channel=='sms':
            if not getattr(branch,'sms_notifications_enabled',False):
                return {'status':'unavailable','message':'SMS-уведомления выключены в настройках филиала'}
            result = product_services().notifications.send('sms', phone,
                pass_link(_full_link(token)),
                {'branch_id': str(branch.branch_id), 'event': 'manual_pass',
                 'client_token': token, 'preference_channel':'sms',
                 'idempotency_key': secrets.token_hex(16)})
            if result.get('status') in {'success', 'accepted', 'sent'} and result.get('delivery') != 'simulated':
                return {'status': 'sent', 'message': 'SMS принята в очередь отправки'}
            return {'status': 'unknown' if result.get('status')=='unknown' else 'failed',
                    'message':result.get('message') or 'SMS не отправлена: проверьте остаток SMS, модем и разрешение исходящих сообщений'}
        from messenger_service import MessengerService
        services = product_services()
        with services.store.connect() as db:
            plan = MessengerService(services.identity).plan(db, user.get('branch_id'), token)
        if not plan['connected']:
            reason = ('Клиент не связан с YCLIENTS. Для отправки по номеру телефона выберите подключённый ChatPush.'
                      if plan['reason'] == 'yclients_client_missing' else 'Подключите выбранный сервис рассылки в настройках филиала.')
            return {'status': 'unavailable', 'message': reason}
        result = services.notifications.send(plan['provider'], plan['target'], pass_link(_full_link(token)),
            {'branch_id': str(user['branch_id']), 'client_token': token, 'preference_channel': 'messengers',
             'event': 'manual_pass', 'idempotency_key': secrets.token_hex(16)})
        if isinstance(result, dict) and result.get("status") in {"success", "accepted"}:
            return {"status": "sent", "message": "Сообщение принято сервисом отправки"}
    except Exception:
        logger.log_error("LINK_DELIVERY_FAILED", "Notification transport failed", {"branch_id": user.get("branch_id")})
    return {"status": "failed", "message": "Сервис отправки не подтвердил прием сообщения"}


def _admin_template(template, **context):
    admin = _admin()
    branch_id = None if admin.get("is_super") is True else _branch_id(admin.get("branch_id"))
    return render_template(template, is_super=admin.get("is_super") is True,
                           admin_branch_id=branch_id,
                           admin_branch_name=branch_manager.get_branch_name(branch_id) if branch_id else None, **context)


@app.route("/opn/<token>")
def open_barrier(token):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
        abort(403)
    user = link_manager.get_link(token)
    if not user or not access_is_active(user):
        # An expired known pass still identifies its own branch for support.
        # This lookup grants no access and never falls back to another branch.
        known_user = user
        if link_manager.guest_pass_removed(token, known_user):
            return render_template('guest_removed.html', client_page=True), 410
        branch = branch_manager.get_branch(known_user.get("branch_id")) if isinstance(known_user, dict) else None
        if branch is not None:
            contacts = []
            for button in branch.buttons:
                if button.type != "admin":
                    continue
                phone = normalize_phone(button.phone)
                if re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                    contacts.append({"id": button.id, "label": button.label, "phone": phone})
            return render_template("403.html", branch_name=branch.name, admin_contacts=contacts, contact_token=token,
                                   access_hours=access_hours_state(branch_manager,branch.branch_id),
                                   preferences_url=None if known_user.get('is_temporary') else _client_preferences_url(token),
                                   is_temporary=bool(known_user.get('is_temporary')),
                                   client_page=True), 403
        abort(403)
    branch = branch_manager.get_branch(user.get("branch_id"))
    if branch is None or not branch.enabled:
        abort(403)
    buttons = []
    available = not getattr(access_service, 'billing', None) or access_service.billing.subscription_view(branch.branch_id)['available']
    for button in branch.buttons:
        if button.type == "gate" and available:
            buttons.append({"id": button.id, "type": "gate", "label": button.label})
        elif button.type == "admin":
            phone = normalize_phone(button.phone)
            if re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                buttons.append({"id": button.id, "type": "admin", "label": button.label, "phone": phone})
    def bot_url(platform, username):
        username = (username or "").lstrip("@")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,100}", username):
            return None
        return f"https://{platform}/{username}?start={quote(token, safe='')}"
    logo = branch.logo or ""
    if logo and not (logo.startswith(("/static/", "/media/branch-logos/")) or urlsplit(logo).scheme == "https"):
        logo = ""
    return render_template("access.html", user_name=user.get("user_name", "Клиент"),
                           branch_name=branch.name, branch_logo=logo, buttons=buttons,
                           access_hours=access_hours_state(branch_manager,branch.branch_id),
                           token=token, timeout=config.timeout,
                           expires_at=None if user.get('admin_role_active') else user.get('access_expires'),
                           is_temporary=bool(user.get('is_temporary')),
                           preferences_url=None if user.get('is_temporary') else _client_preferences_url(token),
                           telegram_url=bot_url("t.me", config.BOT_USERNAME),
                           max_url=bot_url("max.ru", config.MAX_BOT_USERNAME))


def _client_preferences_url(token):
    return '/pwa/' + token + '/preferences' if request.endpoint == 'pwa_gate' else '/preferences/' + token


@app.get('/pwa/<token>/gate')
def pwa_gate(token):
    return open_barrier(token)


@app.get('/pwa/<token>/preferences')
def pwa_preferences(token):
    # Reuse the same identity and permissions as the ordinary preferences page.
    try:
        return app.view_functions['identity.preferences_page'](token)
    except (ValueError, PermissionError):
        abort(403)


@app.get('/pwa/<token>/manifest.webmanifest')
def pwa_manifest(token):
    from branding import BrandingSettings
    try:
        metadata = product_services().pwa.metadata(_token(token))
    except (ValueError, PermissionError):
        abort(404)
    brand = BrandingSettings(product_services().identity).public_view()
    manifest = {'id': metadata['scope'], 'name': 'GateBot · ' + metadata['branch_name'][:100],
                'short_name': 'GateBot', 'lang': 'ru', 'dir': 'ltr',
                'description': 'Ваш пропуск и настройки уведомлений',
                'start_url': metadata['gate_url'], 'scope': metadata['scope'],
                'display': 'standalone', 'background_color': '#EFF0E8', 'theme_color': '#FFFFFF',
                'prefer_related_applications': False, 'categories': ['utilities'],
                'icons': [{'src': brand['icon192_url'], 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
                          {'src': brand['icon512_url'], 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'}]}
    if metadata['preferences_url']:
        manifest['shortcuts'] = [{'name': 'Настройки уведомлений', 'short_name': 'Уведомления',
                                  'url': metadata['preferences_url']}]
    return Response(json.dumps(manifest, ensure_ascii=False), mimetype='application/manifest+json')


@app.get('/pwa/sw.js')
def pwa_worker():
    response = send_file(os.path.join(app.static_folder, 'pwa', 'sw.js'), mimetype='application/javascript')
    response.headers['Service-Worker-Allowed'] = '/pwa/'
    return response


@app.route("/open-gate", methods=["POST"])
def open_gate():
    data = _json_body()
    token = _token(data.get("token"))
    button_id = data.get("button_id")
    if not isinstance(button_id, str) or not button_id or len(button_id) > 128 or "phone" in data:
        abort(400, description="Выберите настроенные ворота филиала")
    request_id = data.get('request_id')
    if request_id is not None and (not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id)):
        abort(400, description='Некорректный идентификатор запроса')
    result = dict(access_service.open_for_token(token, button_id, request_id=request_id)) if request_id else dict(access_service.open_for_token(token, button_id))
    status_code = result.pop("http_status", 200 if result.get("status") == "success" else 403)
    result.setdefault("confirmed", False)
    return jsonify(result), status_code


@app.post('/api/access/status')
def access_status():
    data = _json_body()
    token = _token(data.get('token'))
    user = link_manager.get_link(token)
    if not user:
        abort(404)
    if link_manager.guest_pass_removed(token, user):
        abort(410)
    active = access_is_active(user)
    # The waiting screen only needs to know when to render the pass page again.
    # A pending request should never read conversations or advance billing state.
    pass_only = data.get('pass_only') is True
    if pass_only and not active:
        return jsonify(active=False)
    branch = branch_manager.get_branch(user.get('branch_id'))
    if not branch:
        abort(404)
    active = active and branch.enabled
    if pass_only:
        return jsonify(active=active)
    if getattr(access_service, 'billing', None):
        active = active and access_service.billing.subscription_view(branch.branch_id)['available']
    remaining = access_service.cooldown_remaining(user)
    buttons = [{'id': b.id, 'type': b.type, 'label': b.label,
                **({'phone': b.phone} if b.type == 'admin' else {})}
               for b in branch.buttons if b.type == 'admin' or active]
    return jsonify(active=active, buttons=buttons, expires_at=None if user.get('admin_role_active') else user.get('access_expires'),
                   access_hours=access_hours_state(branch_manager,branch.branch_id),
                   server_time=now().isoformat(), retry_after=remaining,
                   cooldown_until=(now()+timedelta(seconds=remaining)).isoformat(),
                   is_temporary=bool(user.get('is_temporary')),
                   preferences_url=None if user.get('is_temporary') else '/preferences/' + token)


@app.get('/admin/notifications')
@admin_auth.require_auth
def notifications_page():
    return _admin_template('admin/notifications.html')


@app.get('/admin/api/notification-inbox')
@admin_auth.require_auth
def notification_inbox():
    from identity_service import admin_receives_event
    services = product_services()
    admin = services.identity.web_admin(session['admin_username'])
    if not admin.get('person_id'):
        return jsonify(items=[])
    with services.store.connect() as db:
        rows = db.execute('SELECT * FROM notification_inbox WHERE person_id=? ORDER BY created DESC LIMIT 100', (admin['person_id'],)).fetchall()
        unread = sum(r['n'] for r in db.execute('SELECT branch_id,event,count(*) AS n FROM notification_inbox WHERE person_id=? AND read_at IS NULL GROUP BY branch_id,event',
                     (admin['person_id'],)) if admin_receives_event(admin,r['branch_id'],r['event']))
    items = []
    for row in rows:
        if not admin_receives_event(admin, row['branch_id'], row['event']):
            continue
        items.append({'id': row['id'], 'event': row['event'], 'created': row['created'], 'read': bool(row['read_at']),
                      **services.notifications._decrypt(row['payload'])})
    return jsonify(items=items, unread=unread)


@app.post('/admin/api/notification-inbox/read-all')
@admin_auth.require_auth
def read_all_notifications():
    from identity_service import admin_receives_event
    services = product_services()
    admin = services.identity.web_admin(session['admin_username'])
    with services.store.connect() as db:
        rows = db.execute('SELECT id,branch_id,event FROM notification_inbox WHERE person_id=? AND read_at IS NULL',
                          (admin.get('person_id'),)).fetchall()
        db.executemany('UPDATE notification_inbox SET read_at=? WHERE id=? AND person_id=?',
                       [(time.time(),r['id'],admin['person_id']) for r in rows if admin_receives_event(admin,r['branch_id'],r['event'])])
    return jsonify(status='success')


@app.route('/admin/client-notices', methods=['GET','POST'])
@admin_auth.require_auth
def client_notices_page():
    from storage.sql_documents import read_document, write_document
    services = product_services()
    username = session['admin_username']
    page = request.args.get('page',1,type=int)
    if not 1 <= page <= 1000000:
        abort(400)
    with services.store.connect() as db:
        actor = services.identity._admin(db,username)
        branches = read_document(db,'branches.json')
        choices = {key:value.get('name',key) for key,value in branches.items()
                   if actor.get('is_super') or key in actor['branch_ids']}
        if request.method=='POST' and not request.form.get('branch_id'):
            abort(400)
        branch_id = (request.form if request.method=='POST' else request.args).get('branch_id') or next(iter(choices),None)
        if branch_id:
            if branch_id not in choices:
                abort(403)
            services.identity._scope(db,username,branch_id)
        if request.method == 'POST':
            if not branch_id or request.form.get('enabled') not in ('1','0'):
                abort(400)
            branches[branch_id]['client_notifications_enabled'] = request.form['enabled']=='1'
            write_document(db,'branches.json',branches)
            return redirect(url_for('client_notices_page',branch_id=branch_id))
        rows = db.execute("""SELECT channel,payload,created,state,id FROM message_outbox
            WHERE branch_id=? AND audience IN ('client','desk_reply')
            UNION ALL SELECT n.channel,n.payload,n.created,e.state,n.id FROM client_direct_notices n
            JOIN message_effects e ON e.id=n.id WHERE n.branch_id=?
            ORDER BY created DESC,id DESC LIMIT 51 OFFSET ?""",(branch_id,branch_id,(page-1)*50)).fetchall()
        items = []
        from timeutil import timezone as branch_zone
        from datetime import datetime
        zone=branch_zone(branches.get(branch_id,{}).get('timezone','Europe/Moscow'))
        clients=read_document(db,'links.json')
        for row in rows[:50]:
            detail = services.notifications._decrypt(row['payload'])
            card=clients.get(detail.get('token')) or {}
            recipient=(card.get('user_name') if str(card.get('branch_id'))==branch_id else '') or ''
            if not recipient and row['channel']!='push':
                recipient=detail.get('target','')
            items.append({'channel':row['channel'],'text':detail['text'],'state':row['state'],
                          'recipient':recipient,
                          'time':datetime.fromtimestamp(row['created'],zone).strftime('%d.%m.%Y %H:%M:%S')})
    return _admin_template('admin/client-notices.html', choices=choices, branch_id=branch_id, items=items, page=page,
                           has_next=len(rows)>50, enabled=branches.get(branch_id,{}).get('client_notifications_enabled',True),
                           system_enabled=config.outbound_enabled('client'))


@app.post('/api/access/contact')
def access_contact():
    payload = _json_body()
    token = _token(payload.get('token'))
    user = link_manager.load_links().get(token)
    branch = branch_manager.get_branch(user.get('branch_id')) if user else None
    button = next((b for b in branch.buttons if b.type == 'admin' and b.id == payload.get('button_id')), None) if branch else None
    if not button:
        abort(404)
    access_service._emit('admin_contact_requested', branch.branch_id, client_name=user.get('user_name'),
                         event_id=logger.reference(token + ':' + str(int(time.time() // 60))))
    return jsonify(status='success')


@app.post('/admin/api/notification-inbox/<identifier>/read')
@admin_auth.require_auth
def read_notification(identifier):
    from identity_service import admin_receives_event
    services = product_services()
    admin = services.identity.web_admin(session['admin_username'])
    with services.store.connect() as db:
        row = db.execute('SELECT branch_id,event FROM notification_inbox WHERE id=? AND person_id=?',
                         (identifier, admin.get('person_id'))).fetchone()
        if not row or not admin_receives_event(admin, row['branch_id'], row['event']):
            abort(404)
        db.execute('UPDATE notification_inbox SET read_at=? WHERE id=? AND person_id=?', (time.time(), identifier, admin.get('person_id')))
    return jsonify(status='success')


@app.route("/office/login", methods=["GET", "POST"])
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    office_login = request.path == '/office/login'
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
            return render_template("admin/login.html",office_login=office_login), 429
        from yclients_marketplace_web import SESSION_KEYS
        marketplace_session = {key: session[key] for key in SESSION_KEYS if key in session}
        office_destination = session.get('office_destination','/office/') if office_login else None
        previous_mobile_user = session.get('admin_username')
        previous_mobile_device = session.get('office_push_device')
        if admin_auth.login_admin(username, request.form.get("password")):
            session.update(marketplace_session)
            if previous_mobile_device and previous_mobile_user:
                if previous_mobile_user == session.get('admin_username'):
                    session['office_push_device'] = previous_mobile_device
                else:
                    product_services().pwa.office.disable(previous_mobile_user,device=previous_mobile_device)
            with _login_lock:
                _login_attempts.pop(key, None)
            _audit("LOGIN", _scope())
            return redirect(office_destination or ('/yclients/marketplace' if session.get('yc_marketplace_intent') else url_for("admin_users")))
        with _login_lock:
            _login_attempts.setdefault(key, deque(maxlen=10)).append(moment)
        flash("Неверное имя пользователя или пароль", "error")
        return render_template("admin/login.html",office_login=office_login), 401
    return render_template("admin/login.html",office_login=office_login)


@app.route("/admin/logout", methods=["POST"])
@admin_auth.require_auth
def admin_logout():
    _audit("LOGOUT", _scope())
    if session.get('office_push_device'):
        product_services().pwa.office.disable(session['admin_username'],device=session['office_push_device'])
    admin_auth.logout_admin()
    if request.form.get('mobile_office')=='1':return redirect('/office/login')
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
@admin_auth.require_super_admin
def admin_logs():
    return _admin_template("admin/logs.html")


@app.route("/admin/settings")
@admin_auth.require_auth
def admin_settings():
    modems = []
    if _scope() is None:
        from modem_settings import read_modems
        modems = [{'id': row['id'], 'name': row['name']} for row in read_modems(product_services().store)]
    return _admin_template("admin/settings.html", available_modems=modems)


@app.route("/admin/telephony")
@admin_auth.require_super_admin
def admin_telephony():
    return _admin_template("admin/telephony.html")


def _telephony_contacts():
    from telephony_contacts import contact_names
    return contact_names(link_manager.load_links())


@app.route("/admin/api/telephony/contacts/resolve", methods=["POST"])
@admin_auth.require_super_admin
def telephony_resolve_contact():
    from telephony_store import chat_key
    phone = _phone(_json_body().get('phone'))
    if not re.fullmatch(r'\+[1-9][0-9]{6,14}', phone or ''):
        abort(400)
    key = chat_key(phone)
    return jsonify(status='success', id=key, phone=phone, contact_name=_telephony_contacts().get(key, ''))


@app.route("/admin/api/telephony/records", methods=["GET", "POST"])
@admin_auth.require_super_admin
def telephony_records():
    from telephony_store import TelephonyStore
    data = _json_body() if request.method == 'POST' else request.args
    try:
        page = max(1, int(data.get('page', 1)))
    except (ValueError, TypeError):
        abort(400)
    kind, direction = data.get('kind', 'all'), data.get('direction', 'all')
    search = data.get('search', '')
    if kind not in {'all', 'call', 'sms'} or direction not in {'all', 'incoming', 'outgoing'} or not isinstance(search, str) or len(search) > 200:
        abort(400)
    return jsonify(status='success', **TelephonyStore().list_records(page, kind, direction, search, _telephony_contacts(), data.get('modem_id') or None))


@app.route("/admin/api/telephony/sms/chats", methods=["POST"])
@admin_auth.require_super_admin
def telephony_chats():
    from telephony_store import TelephonyStore
    data = _json_body()
    search = data.get('search', '')
    if not isinstance(search, str) or len(search) > 200:
        abort(400)
    try:
        page = max(1, int(data.get('page', 1)))
    except (ValueError, TypeError):
        abort(400)
    return jsonify(status='success', **TelephonyStore().chats(search, page, _telephony_contacts(), data.get('modem_id') or None))


def _chat_key(value):
    if not re.fullmatch(r'[a-f0-9]{64}', value):
        abort(400)
    return value


@app.route("/admin/api/telephony/sms/chats/<chat_id>", methods=["GET"])
@admin_auth.require_super_admin
def telephony_chat_messages(chat_id):
    from telephony_store import TelephonyStore
    try:
        page = max(1, int(request.args.get('page', 1)))
    except (ValueError, TypeError):
        abort(400)
    return jsonify(status='success', **TelephonyStore().messages(_chat_key(chat_id), page, _telephony_contacts(), request.args.get('modem_id') or None))


@app.route("/admin/api/telephony/sms/messages/<record_id>/delete", methods=["POST"])
@admin_auth.require_super_admin
def telephony_delete_message(record_id):
    from telephony_store import TelephonyStore
    if not re.fullmatch(r'[a-f0-9]{32}|[a-f0-9]{64}', record_id):
        abort(400)
    try:
        if not TelephonyStore().delete_message(record_id):
            abort(404)
    except ValueError as error:
        return jsonify(status='error', message=str(error)), 409
    logger.event('TELEPHONY_SMS_DELETED', actor=session['admin_username'], details={'record_id': record_id})
    return jsonify(status='success')


@app.route("/admin/api/telephony/sms/chats/<chat_id>/read", methods=["POST"])
@admin_auth.require_super_admin
def telephony_chat_read(chat_id):
    from telephony_store import TelephonyStore
    TelephonyStore().read_chat(_chat_key(chat_id), request.args.get('modem_id') or None)
    return jsonify(status='success')


@app.route("/admin/api/telephony/sms/clear", methods=["POST"])
@app.route("/admin/api/telephony/sms/chats/<chat_id>/clear", methods=["POST"])
@admin_auth.require_super_admin
def telephony_clear_sms(chat_id=None):
    from telephony_store import TelephonyStore
    try:
        deleted = TelephonyStore().clear_sms(_chat_key(chat_id) if chat_id else None, request.args.get('modem_id') or None)
    except ValueError as error:
        return jsonify(status='error', message=str(error)), 409
    logger.event('TELEPHONY_SMS_CLEARED', actor=session['admin_username'], details={'count': deleted})
    return jsonify(status='success', deleted=deleted)


@app.route("/admin/api/telephony/status")
@admin_auth.require_super_admin
def telephony_status():
    from sim800c import get_call_queue_status
    status = get_call_queue_status()
    from telephony_store import TelephonyStore
    result = {key: status[key] for key in ('enabled', 'dry_run', 'queue_length', 'stopping', 'modem')}
    result['modems'] = status.get('modems', [])
    result['archived_modems'] = TelephonyStore().modem_names()
    return jsonify(result)


@app.route("/admin/api/telephony/preview", methods=["POST"])
@admin_auth.require_super_admin
def telephony_preview():
    from sms_submit import encode_submit
    data = _json_body()
    phone = _phone(data.get('phone'))
    try:
        count = len(encode_submit(phone, data.get('text')))
    except ValueError as error:
        return jsonify(status='error', message=str(error)), 400
    return jsonify(status='success', parts=count, encoding='UTF-16BE')


@app.route("/admin/api/telephony/commands", methods=["POST"])
@admin_auth.require_super_admin
def telephony_command():
    from telephony_service import TelephonyService
    try:
        result = TelephonyService().submit(_admin(), session['admin_username'], _json_body())
    except ValueError as error:
        return jsonify(status='error', message=str(error)), 400
    except PermissionError:
        abort(403)
    logger.event('TELEPHONY_MANUAL_COMMAND', actor=session['admin_username'],
                 details={'record_id': result.get('record_id'), 'delivery': result.get('delivery'),
                          'error_code': result.get('code')})
    return jsonify(result), 202 if result.get('status') == 'success' else 503


@app.route("/admin/api/telephony/records/<record_id>/read", methods=["POST"])
@admin_auth.require_super_admin
def telephony_mark_read(record_id):
    from telephony_store import TelephonyStore
    if not TelephonyStore().update(record_id, read=True):
        abort(404)
    return jsonify(status='success')


def _public_branch_settings(branch, *, include_modems=False):
    raw = branch_manager.load_branches().get(str(branch.branch_id), {})
    return {
        "branch_id": branch.branch_id, "name": branch.name, "timezone": branch.timezone,
        "enabled": branch.enabled, "logo": branch.logo,
        "yclients_enabled": branch.yclients_enabled, "id_origin": branch.id_origin,
        "sms_notifications_enabled": getattr(branch,'sms_notifications_enabled',False),
        "client_notifications_enabled": raw.get('client_notifications_enabled', True),
        "hardware_routes_verified": raw.get('hardware_routes_verified', True),
        **({'modem_ids': raw.get('modem_ids')} if include_modems else {}),
        "pass_duration_hours": branch.pass_duration_hours,
        "auto_approve_pass_requests": branch.auto_approve_pass_requests,
        "buttons": [{"type": button.type, "label": button.label, "phone": button.phone,
                     "stable_id": button.stable_id}
                    for button in branch.buttons],
    }


def _logo_url(value):
    value = _text("" if value is None else value, "Логотип", max_length=2048) or ""
    if not value:
        return ""
    parsed = urlsplit(value)
    local = value.startswith("/static/") and ".." not in parsed.path.split("/") and not parsed.netloc
    local = local or bool(re.fullmatch(r'/media/branch-logos/[a-f0-9]{32}\.png', value))
    remote = parsed.scheme == "https" and bool(parsed.netloc) and not parsed.username and not parsed.password
    if not (local or remote) or "\\" in value:
        abort(400, description="Логотип должен быть локальным /static/ файлом или HTTPS-ссылкой без учетных данных")
    return value


_BRANCH_NOTIFICATION_SWITCHES = {
    'client_notifications_enabled': 'Уведомления клиентам',
    'sms_notifications_enabled': 'SMS-уведомления',
    'auto_approve_pass_requests': 'Автоматическая выдача пропуска',
}


@app.post('/admin/api/branches/<int:branch_id>/notification-settings')
@admin_auth.require_auth
def update_branch_notification_settings(branch_id):
    _choose_branch(branch_id)
    data = _json_body()
    if len(data) != 1 or not data.keys() <= _BRANCH_NOTIFICATION_SWITCHES.keys():
        abort(400, description='Выберите одну настройку уведомлений или запросов')
    field, value = next(iter(data.items()))
    value = _boolean(value, _BRANCH_NOTIFICATION_SWITCHES[field])
    if not branch_manager.update_settings(branch_id, {field: value}):
        abort(404, description='Филиал не найден')
    _audit('BRANCH_SETTINGS_UPDATED', branch_id, changed_fields=[field])
    return jsonify(status='success', **{field: value})


@app.route("/admin/api/branch-settings", methods=["GET", "POST"])
@admin_auth.require_auth
def api_branch_settings():
    scope = _scope()
    if request.method == "GET":
        value = request.args.get("branch_id")
        if value is None and scope is None:
            return jsonify(settings=None)
        branch = _choose_branch(value)
        return jsonify(settings=_public_branch_settings(branch, include_modems=scope is None))
    data = _json_body()
    allowed = {"branch_id", "create", "name", "timezone", "enabled", "logo", "buttons",
               "yclients_enabled", "pass_duration_hours", *_BRANCH_NOTIFICATION_SWITCHES}
    if 'modem_ids' in data and scope is not None:
        abort(403, description='Настройка модемов доступна только администрации сервиса')
    if set(data) - (allowed | {'modem_ids'}):
        abort(400, description="Недопустимые поля настроек филиала")
    create = _boolean(data.get("create", False), "Создать филиал")
    internal_create = create and data.get('yclients_enabled') is False
    if internal_create:
        if data.get('branch_id') not in (None, ''):
            abort(400, description='Номер самостоятельного филиала назначается автоматически')
        branch_id = None
    else:
        branch_id = _branch_id(data.get("branch_id", scope))
    if scope is not None and (create or branch_id != scope):
        abort(403, description="Нет прав на изменение этого филиала")
    existing = branch_manager.get_branch(branch_id)
    raw_branch = branch_manager.load_branches().get(str(branch_id), {})
    yclients_enabled = _boolean(data.get('yclients_enabled', raw_branch.get('yclients_enabled', True)), 'Использовать YCLIENTS')
    if scope is not None and yclients_enabled != raw_branch.get('yclients_enabled', True):
        abort(403, description='Для изменения типа филиала обратитесь в поддержку GateBot')
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
        if not isinstance(item, dict) or not {"type", "label", "phone"}.issubset(item) or set(item) - {"type", "label", "phone", "stable_id"} or item.get("type") not in {"gate", "admin"}:
            abort(400, description="Некорректная кнопка филиала")
        phone = _phone(item.get("phone"))
        if phone is None:
            abort(400, description="Для каждой кнопки нужен номер телефона")
        validated_buttons.append({"type": item["type"], "label": _text(item["label"], "Название кнопки", required=True, max_length=100), "phone": phone})
        if item.get('stable_id') is not None:
            stable = item['stable_id']
            if not isinstance(stable, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{12,64}', stable):
                abort(400, description='Некорректный идентификатор ворот')
            validated_buttons[-1]['stable_id'] = stable
    hours = data.get('pass_duration_hours',raw_branch.get('pass_duration_hours',8))
    if type(hours) is not int or not 1 <= hours <= 168:
        abort(400, description='Укажите срок разового пропуска от 1 до 168 часов')
    settings = {"name": _text(data.get("name"), "Название филиала", required=True),
                "timezone": zone, "enabled": _boolean(data.get("enabled"), "Филиал активен"),
                "logo": _logo_url(data.get("logo")), "buttons": validated_buttons,
                "yclients_enabled": yclients_enabled,
                "pass_duration_hours": hours}
    for field, label in _BRANCH_NOTIFICATION_SWITCHES.items():
        if field in data:
            settings[field] = _boolean(data[field], label)
    if 'modem_ids' in data:
        settings['modem_ids'] = data['modem_ids']
    try:
        if create:
            billing = product_services().billing
            # In the active SQL backend the branch and its first trial commit
            # together. Existing branches never receive another trial on edits.
            with branch_manager.store.lock, billing.store.connect() as db:
                if internal_create:
                    branch_id = branch_manager.allocate_internal_id(db)
                updated = branch_manager.update_settings(branch_id, settings, create=True)
                if updated:
                    billing.activate_trial(str(branch_id), timezone=zone, db=db)
        else:
            if existing.timezone != zone:
                billing = product_services().billing
                with branch_manager.store.lock, billing.store.connect() as db:
                    updated = branch_manager.update_settings(branch_id, settings, create=False)
                    if updated:
                        billing.schedule_timezone(str(branch_id), zone, actor=session['admin_username'], db=db)
            else:
                updated = branch_manager.update_settings(branch_id, settings, create=False)
            if updated and raw_branch.get('hardware_routes_verified') is False and any(b['type'] == 'gate' for b in validated_buttons):
                with branch_manager.store.transaction() as stored:
                    stored[str(branch_id)]['hardware_routes_verified'] = True
    except ValueError as error:
        if 'модем' in str(error):
            abort(400, description=str(error))
        abort(400, description="Проверьте настройки: номера кнопок одного типа не должны повторяться")
    if not updated:
        abort(404, description="Филиал не найден")
    _audit("BRANCH_CREATED" if create else "BRANCH_SETTINGS_UPDATED", branch_id, changed_fields=sorted(settings))
    return jsonify(status="success", settings=_public_branch_settings(branch_manager.get_branch(branch_id), include_modems=scope is None))


def _users_in_scope():
    scope = _scope()
    return [user for user in link_manager.get_all_links()
            if scope is None or str(user.get("branch_id")) == str(scope)]


@app.route("/admin/api/users")
@admin_auth.require_auth
def api_get_users():
    from storage.pass_changes import changed_at
    users = []
    for user in _users_in_scope():
        user = dict(user)
        branch = branch_manager.get_branch(user.get("branch_id"))
        expires = None if user.get('admin_role_active') else user.get("access_expires")
        active = access_is_active(user)
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
                    pass_changed_at=changed_at(user),
                    access_expires_days=days, gate_access=active,
                    access_available=bool(active and branch is not None and branch.enabled),
                    branch_access_reason=('branch_missing' if branch is None else
                        'branch_disabled' if not branch.enabled else None),
                    has_bot=bool(user.get("tg_id") or user.get("max_id")),
                    has_telegram=bool(user.get("tg_id")), has_max=bool(user.get("max_id")),
                    yclients_enabled=bool(branch and branch.yclients_enabled),
                    sms_notifications_enabled=bool(branch and getattr(branch,'sms_notifications_enabled',False)),
                    has_yclients=user.get("id_ycl") not in (None, ""))
        users.append(user)
    if is_sql_active(link_manager.store.path.parent):
        from client_notification_admin import ClientNotificationAdmin
        services=product_services();notifications=ClientNotificationAdmin(services.identity)
        with services.store.connect() as db:
            clients,accounts=services.identity._client_table_snapshot(db,session['admin_username'],[user['token'] for user in users])
            roles=services.identity._client_admin_states(db,clients,accounts=accounts)
            summaries=notifications._summaries(db,clients)
            for user in users:
                user['admin_role']=roles[user['token']]
                user['notification_status']=summaries[user['token']]
    else:
        # The normal runtime migrates first. A legacy maintenance view must not
        # create a second SQL writer just to render notification controls.
        for user in users:
            user['notification_status']={'available':False,'enabled':False,'effective_channels':{},'reason':'startup_required'}
    users.sort(key=lambda user:(user.get('pass_changed_at') or '',user['token']),reverse=True)
    return jsonify(users=users)


@app.route("/admin/api/branches")
@admin_auth.require_auth
def api_get_branches():
    scope = _scope()
    return jsonify(branches=[{"id": b.branch_id, "name": b.name, "yclients_enabled": b.yclients_enabled,
                              "sms_notifications_enabled":getattr(b,'sms_notifications_enabled',False)}
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
    if not branch.yclients_enabled and data.get('id_ycl') not in (None, ''):
        abort(400, description='Этот филиал работает без YCLIENTS')
    user = {"id_ycl": _external_id(data.get("id_ycl"), "YCLIENTS ID"),
            "user_name": _text(data.get("user_name"), "Имя", required=True),
            "phone": _phone(data.get("phone")), "branch_id": branch.branch_id,
            "email": _client_email(data.get("email", "")),
            "gate_access": True, "access_source": "manual"}
    send = _boolean(data.get("send_link", False), "Отправить ссылку")
    channel = _pass_delivery_channel(data.get('channel','auto'))
    if send and channel=='email' and not user['email']:
        abort(400,description='Укажите Email для отправки ссылки')
    if not user["phone"]:
        abort(400, description="Номер телефона клиента обязателен")
    days = _days(data.get("expires_days"))
    token = link_manager.create_link(user, expires_days=days)
    _audit("CREATE_LINK", branch.branch_id, expires_days=days)
    delivery = _send_link(user["phone"], token, user, channel) if send else {"status": "not_requested"}
    return jsonify(status="success" if delivery["status"] in {"sent", "not_requested"} else "partial",
                   created=True, token=token, link=url_for("open_barrier", token=token), delivery=delivery)


@app.route("/admin/api/create-temp-link", methods=["POST"])
@admin_auth.require_auth
def api_create_temp_link():
    data = _json_body()
    branch = _choose_branch(data.get("branch_id"))
    phone = _phone(data.get("phone"))
    send = _boolean(data.get("send_link", False), "Отправить ссылку")
    channel = _pass_delivery_channel(data.get('channel','auto'))
    email=_client_email(data.get('email',''))
    if send and channel=='email' and not email:
        abort(400,description='Укажите Email для отправки ссылки')
    if not phone:
        abort(400, description="Номер телефона клиента обязателен")
    user = {"user_name": "Гость", "phone": phone, "email":email, "branch_id": branch.branch_id,
            "mailing_enabled": False, "gate_access": True, "is_temporary": True,
            "access_source": "manual"}
    token = link_manager.create_guest(branch, phone, email=email)
    _audit("CREATE_TEMP_LINK", branch.branch_id, expires_at=link_manager.load_links()[token]['access_expires'])
    delivery = _send_link(phone, token, user, channel) if send else {"status": "not_requested"}
    return jsonify(status="success" if delivery["status"] in {"sent", "not_requested"} else "partial",
                   created=True, token=token, link=url_for("open_barrier", token=token), delivery=delivery)


@app.route("/admin/api/send-link", methods=["POST"])
@admin_auth.require_auth
def api_send_link():
    data = _json_body()
    channel = _pass_delivery_channel(data.get('channel','auto'))
    token, user = _scoped_link(data.get("token"))
    phone = _phone(data.get("phone", user.get("phone")))
    if not phone:
        abort(400, description="Для отправки нужен номер телефона")
    if "phone" in data and phone != user.get("phone"):
        link_manager.set_user_phone(token, phone)
        user=link_manager.load_links()[token]
    result = _send_link(phone, token, user, channel)
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
    if "email" in data:
        updates["email"] = _client_email(data["email"])
    if "tg_id" in data or "max_id" in data:
        abort(400, description="Привязка мессенджера выполняется пользователем через личную ссылку")
    for key in ("id_ycl",):
        if key in data:
            branch = branch_manager.get_branch(user['branch_id'])
            if branch is not None and not branch.yclients_enabled:
                abort(400, description='Этот филиал работает без YCLIENTS')
            updates[key] = _external_id(data[key], key)
    if not updates:
        abort(400, description="Нет изменений")
    if "email" in updates and is_sql_active(link_manager.store.path.parent):
        services = product_services()
        try:
            # A privileged recipient check and the contact update share the
            # document transaction, so a role change cannot race the write.
            with link_manager.store.lock, services.store.connect():
                token, user = _scoped_link(data.get("token"))
                if updates["email"] != user.get("email", ""):
                    services.identity.assert_client_email_change(session['admin_username'], token, updates["email"])
                updated = link_manager.update_user(token, updates)
        except PermissionError as error:
            abort(403, description=str(error))
    else:
        updated = link_manager.update_user(token, updates)
    if not updated:
        abort(404, description="Пользователь не найден")
    _audit("UPDATE_USER", user["branch_id"], changed_fields=sorted(updates))
    return jsonify(status="success")


@app.route("/admin/api/set-mailing", methods=["POST"])
@admin_auth.require_auth
def api_set_mailing():
    data = _json_body()
    token, user = _scoped_link(data.get("token"))
    enabled = _boolean(data.get("enabled"), "Рассылка")
    from notification_policy import DISABLED_MESSAGE
    branch = branch_manager.load_branches().get(str(user['branch_id']),{})
    if not config.outbound_enabled('client') or not branch.get('client_notifications_enabled',True):
        abort(403,description=DISABLED_MESSAGE)
    if not link_manager.set_mailing_enabled(token, enabled):
        abort(404, description="Пользователь не найден")
    _audit("SET_MAILING", user["branch_id"], mailing_enabled=enabled)
    return jsonify(status="success")


@app.route("/admin/api/delete-user", methods=["POST"])
@admin_auth.require_auth
def api_delete_user():
    data=_json_body();token, user = _scoped_link(data.get("token"))
    if is_sql_active(link_manager.store.path.parent):
        try:
            product_services().identity.delete_client(session['admin_username'],token,confirm_admin=data.get('confirm_admin') is True)
        except PermissionError as error:abort(403,description=str(error))
        except ValueError as error:abort(409,description=str(error))
    else:
        if user.get('admin_role_active'):
            abort(409,description='Перезапустите приложение для подготовки управления ролями')
        if not link_manager.delete_link(token):abort(404, description="Пользователь не найден")
    _audit("DELETE_USER", user["branch_id"])
    return jsonify(status="success")


@app.get('/admin/api/client-role')
@admin_auth.require_auth
def api_client_role():
    token,user=_scoped_link(request.args.get('token'))
    try:role=product_services().identity.client_admin_state(session['admin_username'],token)
    except PermissionError as error:abort(403,description=str(error))
    except ValueError as error:abort(404,description=str(error))
    return jsonify(admin_role=role)


@app.post('/admin/api/client-role/revoke')
@admin_auth.require_auth
def api_revoke_client_admin():
    token,user=_scoped_link(_json_body().get('token'))
    try:product_services().identity.revoke_client_admin(session['admin_username'],token)
    except PermissionError as error:abort(403,description=str(error))
    except ValueError as error:abort(409,description=str(error))
    _audit('CLIENT_ADMIN_REVOKED',user['branch_id'])
    return jsonify(status='success')


@app.get('/admin/api/branches/<int:branch_id>/deletion')
@admin_auth.require_super_admin
def api_branch_deletion_preview(branch_id):
    from branch_lifecycle import BranchLifecycleService
    try:preview=BranchLifecycleService(product_services().identity).preview(session['admin_username'],branch_id)
    except PermissionError as error:abort(403,description=str(error))
    except ValueError as error:abort(404,description=str(error))
    return jsonify(preview=preview)


@app.post('/admin/api/branches/<int:branch_id>/delete')
@admin_auth.require_super_admin
def api_delete_branch(branch_id):
    from branch_lifecycle import BranchLifecycleService
    data=_json_body()
    if set(data)-{'confirmation','revision'}:abort(400,description='Некорректное подтверждение удаления')
    try:
        result=BranchLifecycleService(product_services().identity).delete(session['admin_username'],branch_id,
                    confirmation=data.get('confirmation'),revision=data.get('revision'))
    except PermissionError as error:abort(403,description=str(error))
    except ValueError as error:abort(409,description=str(error))
    _audit('BRANCH_DELETED',branch_id,clients=result['clients'],admins=result['admins'])
    return jsonify(status='success',**result)


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
    headers = ["token", "id_ycl", "user_name", "phone", "email", "tg_id", "max_id", "mailing_enabled", "branch_id", "gate_access", "access_expires", "created"]
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
@admin_auth.require_super_admin
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
    if request.method == 'GET':
        return redirect('/register-branch')
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
    if request.is_json or request.path.startswith(("/admin/api/", "/api/", "/open-gate", "/webhook/")):
        return jsonify(status="error", message=error.description, correlation_id=getattr(g, "correlation_id", None)), error.code
    if error.code in {403, 404}:
        return render_template(f"{error.code}.html", error_message=error.description), error.code
    return error.get_response()


@app.errorhandler(Exception)
def unexpected_error(error):
    logger.log_error("HTTP_UNHANDLED", type(error).__name__, {"correlation_id": getattr(g, "correlation_id", None)})
    return jsonify(status="error", message="Не удалось выполнить действие. Сообщите администратору номер запроса",
                   correlation_id=getattr(g, "correlation_id", None)), 500


from identity_web import create_blueprint as identity_blueprint
from product_web import create_blueprint as billing_blueprint
from smtp_web import create_blueprint as smtp_blueprint
from system_settings_web import create_blueprint as system_settings_blueprint
from messenger_web import create_blueprint as messenger_blueprint
from client_notification_admin import create_blueprint as client_notification_blueprint
from branch_hours_web import create_blueprint as branch_hours_blueprint
from branch_logos import create_blueprint as logo_blueprint
from yclients_marketplace_web import create_blueprint as marketplace_blueprint
from gate_qr_web import create_blueprint as gate_qr_blueprint
from password_recovery_web import create_blueprint as password_recovery_blueprint
from help_support_web import create_blueprint as help_support_blueprint
from palette_web import create_blueprint as palette_blueprint
from branding import create_blueprint as branding_blueprint
from pwa_web import create_blueprint as pwa_blueprint
from office_web import create_blueprint as office_blueprint
app.register_blueprint(office_blueprint(product_provider))
from client_feedback import create_blueprint as feedback_blueprint
from branch_desk_web import create_blueprint as branch_desk_blueprint
app.register_blueprint(branch_desk_blueprint(product_provider))
app.register_blueprint(feedback_blueprint(product_provider))
from service_api import create_blueprint as service_api_blueprint
app.register_blueprint(service_api_blueprint(product_provider))
app.register_blueprint(branding_blueprint(product_provider))
app.register_blueprint(pwa_blueprint(product_provider))
app.register_blueprint(palette_blueprint(product_provider))
app.register_blueprint(password_recovery_blueprint(product_provider))
app.register_blueprint(help_support_blueprint(product_provider))
app.register_blueprint(smtp_blueprint(product_provider))
app.register_blueprint(system_settings_blueprint(product_provider))
app.register_blueprint(messenger_blueprint(product_provider))
from yclients_client_sync_web import create_blueprint as yclients_client_sync_blueprint
app.register_blueprint(yclients_client_sync_blueprint(product_provider))
app.register_blueprint(client_notification_blueprint(product_provider))
app.register_blueprint(branch_hours_blueprint(product_provider))
app.register_blueprint(logo_blueprint(product_provider))
app.register_blueprint(marketplace_blueprint(product_provider))
app.register_blueprint(gate_qr_blueprint(product_provider))
app.register_blueprint(identity_blueprint(product_provider))
app.register_blueprint(billing_blueprint(provider=product_provider, render_admin=_admin_template))

from storage.sql_documents import is_sql_active
if is_sql_active(config.DATA_DIR):
    product_services().attach(access_service)


if __name__ == "__main__":
    print("Для запуска приложения используйте: python run.py")
