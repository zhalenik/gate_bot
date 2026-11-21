"""Integration tokens and authenticated, server-to-server API. No access via cookies."""
from datetime import datetime, timezone
import hashlib
import math
import re
import secrets
import uuid

from flask import Blueprint, abort, jsonify, render_template, request, session

from client_feedback import list_reviews
from identity_service import valid_phone
from qr_delivery import settings as qr_settings
from storage.sql_documents import read_document


SCOPES = {
    'reviews': 'Чтение отзывов',
    'system_sms': 'Системные SMS',
    'branch_sms': 'SMS за счёт филиала',
    'branch_chatpush': 'Коды через ChatPush филиала',
    'branch_yclients': 'Коды через YCLIENTS филиала',
}


def prepare(db):
    db.execute("""CREATE TABLE IF NOT EXISTS service_api_tokens (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, digest TEXT UNIQUE NOT NULL, scopes TEXT NOT NULL,
        rate_limit INTEGER NOT NULL, created REAL NOT NULL, requests INTEGER NOT NULL DEFAULT 0, last_used REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS service_api_requests (
        token_id INTEGER NOT NULL REFERENCES service_api_tokens(id) ON DELETE CASCADE, at REAL NOT NULL)""")
    db.execute('CREATE INDEX IF NOT EXISTS service_api_token_rate ON service_api_requests(token_id,at)')
    db.execute('CREATE INDEX IF NOT EXISTS service_api_rate_cleanup ON service_api_requests(at)')
    db.execute('CREATE TABLE IF NOT EXISTS service_code_attempts(phone_hash TEXT NOT NULL,at REAL NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS service_code_rate ON service_code_attempts(phone_hash,at)')
    db.execute('CREATE INDEX IF NOT EXISTS service_code_cleanup ON service_code_attempts(at)')


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat() if at is not None else None


def failure(code, status, wait=None):
    response = jsonify(error=code)
    response.status_code = status
    if wait:
        response.headers['Retry-After'] = str(wait)
    if status == 401:
        response.headers['WWW-Authenticate'] = 'Bearer realm="GateBot"'
    return response


def authorize(identity, scope):
    match = re.fullmatch(r'Bearer (gbt_[A-Za-z0-9_-]{43})', request.headers.get('Authorization', ''), re.ASCII | re.IGNORECASE)
    if not match:
        return failure('invalid_token', 401)
    digest = hashlib.sha256(match[1].encode('ascii')).hexdigest()
    now = identity.clock()
    with identity.product.connect() as db:
        prepare(db)
        token = db.execute('SELECT * FROM service_api_tokens WHERE digest=?', (digest,)).fetchone()
        if not token:
            return failure('invalid_token', 401)
        db.execute('UPDATE service_api_tokens SET requests=requests+1,last_used=? WHERE id=?', (now, token['id']))
        db.execute('DELETE FROM service_api_requests WHERE at<=?', (now-60,))
        count, first = db.execute('SELECT COUNT(*),MIN(at) FROM service_api_requests WHERE token_id=?', (token['id'],)).fetchone()
        if count >= token['rate_limit']:
            return failure('rate_limited', 429, max(1, math.ceil(first + 60 - now)))
        db.execute('INSERT INTO service_api_requests VALUES(?,?)', (token['id'], now))
        if scope not in token['scopes'].split(','):
            return failure('insufficient_scope', 403)
    return None


def claim_phone(identity, phone):
    now = identity.clock()
    with identity.product.connect() as db:
        db.execute('DELETE FROM service_code_attempts WHERE at<=?', (now-3600,))
        phone_hash = identity._hash('service-api-phone:' + phone)
        count, first, last = db.execute('SELECT COUNT(*),MIN(at),MAX(at) FROM service_code_attempts WHERE phone_hash=?',
                                        (phone_hash,)).fetchone()
        if count >= 10:
            return max(1, math.ceil(first + 3600 - now))
        if last is not None and last > now-60:
            return max(1, math.ceil(last + 60 - now))
        db.execute('INSERT INTO service_code_attempts VALUES(?,?)', (phone_hash, now))
        return 0


def send_yclients_code(identity, branches, branch, phone, text):
    """Resolve the complete phone in the issuing branch before using its delivery credentials."""
    from storage import config
    from yclients_client_sync import YClientsClientSync
    from yclients_delivery import YClientsDelivery
    directory = YClientsClientSync(identity, branches)
    with identity.product.connect() as db:
        if not directory._availability(db, branch)['ready']:
            return {'status': 'failed', 'code': 'yclients_not_connected'}
    try:
        matches = directory.crm.find_clients_by_phone(branch, phone)
    except Exception:
        return {'status': 'failed', 'code': 'directory_unavailable'}
    if not matches:
        return {'status': 'failed', 'code': 'client_not_found'}
    if len(matches) != 1 or matches[0]['phone'] != phone:
        return {'status': 'failed', 'code': 'client_ambiguous'}
    with identity.product.connect() as db:
        if not directory._availability(db, branch)['ready']:
            return {'status': 'failed', 'code': 'yclients_not_connected'}
        credentials = identity.integration_credentials(branch, 'yclients')
    return YClientsDelivery().send(branch, matches[0]['id'], text,
        {**credentials, 'partner_token': credentials.get('partner_token') or config.YC_PARTNER_TOKEN})


def create_blueprint(provider):
    bp = Blueprint('service_api', __name__)

    def owner():
        services = provider()
        actor = services['auth'].get_current_admin()
        if not actor or not actor.get('is_super'):
            abort(403)
        return services['service']

    @bp.get('/admin/service-api')
    def manage():
        identity = owner()
        with identity.product.connect() as db:
            prepare(db)
            tokens = [dict(row) for row in db.execute(
                'SELECT id,name,scopes,rate_limit,created,requests,last_used FROM service_api_tokens ORDER BY id DESC')]
        for token in tokens:
            token['permissions'] = [SCOPES[scope] for scope in token['scopes'].split(',') if scope in SCOPES]
            token['last_used'] = iso(token['last_used'])
        return render_template('admin/service-api.html', admin_page=True, is_super=True, tokens=tokens, scopes=SCOPES)

    @bp.get('/admin/service-api/guide')
    def guide():
        owner()
        return render_template('admin/service-api-guide.html', admin_page=True, is_super=True, scopes=SCOPES)

    @bp.post('/admin/api/service-tokens')
    def issue():
        identity = owner()
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return failure('invalid_token_settings', 400)
        name, scopes, limit = body.get('name'), body.get('scopes'), body.get('rate_limit', 30)
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
                or not isinstance(scopes, list) or not 1 <= len(scopes) <= len(SCOPES)
                or not all(isinstance(scope, str) and scope in SCOPES for scope in scopes)
                or type(limit) is not int or not 1 <= limit <= 600):
            return failure('invalid_token_settings', 400)
        raw = 'gbt_' + secrets.token_urlsafe(32)
        with identity.product.connect() as db:
            prepare(db)
            token_id = db.execute('INSERT INTO service_api_tokens(name,digest,scopes,rate_limit,created) VALUES(?,?,?,?,?)',
                       (name.strip(), hashlib.sha256(raw.encode('ascii')).hexdigest(),
                        ','.join(sorted(set(scopes))), limit, identity.clock())).lastrowid
        return jsonify(id=token_id, token=raw), 201

    @bp.delete('/admin/api/service-tokens/<int:token_id>')
    def revoke(token_id):
        identity = owner()
        with identity.product.connect() as db:
            prepare(db)
            db.execute('DELETE FROM service_api_tokens WHERE id=?', (token_id,))
        return '', 204

    @bp.route('/admin/api/branches/<int:branch_id>/qr-delivery', methods=['GET', 'POST'])
    def qr_channel(branch_id):
        services = provider()
        actor = services['auth'].get_current_admin()
        if not actor:
            abort(401)
        identity = services['service']
        with identity.product.connect() as db:
            try:
                identity._scope(db, session['admin_username'], str(branch_id))
                channel = None
                if request.method == 'POST':
                    body = request.get_json(silent=True)
                    if not isinstance(body, dict) or body.get('channel') not in ('sms', 'chatpush'):
                        raise ValueError('Выберите канал отправки кодов')
                    channel = body['channel']
                return jsonify(qr_settings(identity, db, branch_id, channel))
            except PermissionError:
                abort(403)
            except ValueError as error:
                return jsonify(message=str(error)), 400

    @bp.get('/api/v1/reviews')
    def reviews():
        identity = provider()['service']
        denied = authorize(identity, 'reviews')
        if denied is not None:
            return denied
        with identity.product.connect() as db:
            rows = list_reviews(db)
        return jsonify([{**row, 'created': iso(row['created'])} for row in rows])

    @bp.post('/api/v1/codes/sms', defaults={'branch_id': None, 'channel': 'sms'})
    @bp.post('/api/v1/branches/<int:branch_id>/codes/<channel>')
    def send_code(branch_id, channel):
        identity = provider()['service']
        if channel not in ('sms', 'chatpush', 'yclients'):
            return failure('unknown_channel', 404)
        scope = ('system_' if branch_id is None else 'branch_') + channel
        denied = authorize(identity, scope)
        if denied is not None:
            return denied
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'phone'}:
            return failure('invalid_request', 400)
        try:
            phone = valid_phone(body['phone'])
        except ValueError:
            return failure('invalid_phone', 400)
        branch = str(branch_id) if branch_id is not None else None
        if branch is not None:
            with identity.product.connect() as db:
                record = read_document(db, 'branches.json').get(branch)
                if not record or record.get('enabled', True) is False:
                    return failure('branch_unavailable', 404)
                if channel == 'chatpush' and not qr_settings(identity, db, branch)['available']:
                    return failure('chatpush_not_connected', 409)
                if channel == 'yclients' and record.get('yclients_enabled', True) is False:
                    return failure('yclients_not_connected', 409)
        wait = claim_phone(identity, phone)
        if wait:
            return failure('phone_rate_limited', 429, wait)
        from message_templates import verification_code
        code = f'{secrets.randbelow(1000000):06d}'
        result = identity._send(channel, phone, verification_code(code), {
            'purpose': 'otp', 'event': 'api_verification_code', 'branch_id': branch,
            'idempotency_key': uuid.uuid4().hex})
        if (result.get('status') in {'queued', 'accepted', 'network_accepted', 'sent', 'success'}
                and result.get('delivery') not in {'simulated', 'dry_run', 'stub'}):
            return jsonify(code)
        reason = result.get('code')
        if reason in {'client_not_found', 'client_ambiguous', 'yclients_not_connected', 'sms_quota_exhausted',
                      'subscription_expired', 'subscription_disabled', 'subscription_blocked',
                      'subscription_pending', 'subscription_cancelled', 'subscription_suspended'}:
            return failure(reason, 404 if reason == 'client_not_found' else 409)
        return failure('delivery_unavailable' if result.get('status') != 'unknown' else 'delivery_unknown', 503)

    return bp
