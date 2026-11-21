"""Printed gate links and revocable, SMS-verified browser identities.

The printed token identifies a gate; it grants no client access. Device tokens
and OTPs are hashed, share the application database, and never select a route
from request data. Delivery uses the existing injected notification transport.
"""
import json
import math
import re
import secrets
import uuid

from identity_service import OtpRateLimit, valid_phone
from qr_delivery import settings as qr_settings
from link_manager import access_is_active, normalize_phone
from storage import config
from branch_hours import access_hours_state, CLOSED_MESSAGE


DEVICE_SECONDS = 30 * 24 * 3600
SESSION_DEVICE_SECONDS = 12 * 3600


class GateQRUnavailable(ValueError):
    pass


class GateQRService:
    def __init__(self, identity, links, branches, access):
        self.identity, self.links, self.branches, self.access = identity, links, branches, access
        self.product, self.clock = identity.product, identity.clock
        with self.product.connect() as db:
            for statement in (
                'CREATE TABLE IF NOT EXISTS gate_qr_links(branch_id TEXT NOT NULL,gate_id TEXT NOT NULL,token TEXT UNIQUE NOT NULL,enabled INTEGER NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,PRIMARY KEY(branch_id,gate_id))',
                'CREATE TABLE IF NOT EXISTS gate_qr_devices(token_hash TEXT PRIMARY KEY,branch_id TEXT NOT NULL,client_token TEXT NOT NULL,phone TEXT NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS gate_qr_challenges(challenge_id TEXT PRIMARY KEY,qr_hash TEXT NOT NULL,browser_hash TEXT NOT NULL,branch_id TEXT NOT NULL,client_token TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS gate_qr_probes(id TEXT PRIMARY KEY,ip_hash TEXT NOT NULL,created REAL NOT NULL)',
                'CREATE INDEX IF NOT EXISTS gate_qr_probe_limits ON gate_qr_probes(ip_hash,created)',
            ):
                db.execute(statement)

    def _gate(self, branch_id, gate_id, *, public=False):
        branch = self.branches.get_branch(branch_id)
        gate = next((gate for gate in branch.buttons if gate.type == 'gate' and gate.stable_id == gate_id), None) if branch else None
        if not gate or (public and not branch.enabled):
            raise GateQRUnavailable('Эти ворота недоступны. Обратитесь к администратору филиала.')
        return branch, gate

    def _public_gate(self, db, raw):
        if not isinstance(raw, str) or len(raw) > 64:
            raise GateQRUnavailable('Ссылка ворот недействительна.')
        row = db.execute('SELECT * FROM gate_qr_links WHERE token=? AND enabled=1', (raw,)).fetchone()
        if not row:
            raise GateQRUnavailable('Ссылка ворот недействительна или отключена.')
        branch, gate = self._gate(row['branch_id'], row['gate_id'], public=True)
        return row, branch, gate

    def admin_view(self, username, branch_id, gate_id, action=None):
        with self.product.connect() as db:
            self.identity._scope(db, username, branch_id)
            branch, gate = self._gate(branch_id, gate_id)
            row = db.execute('SELECT * FROM gate_qr_links WHERE branch_id=? AND gate_id=?', (str(branch_id), gate_id)).fetchone()
            if action not in {None, 'create', 'rotate', 'revoke'}:
                raise ValueError('Неизвестное действие')
            if action == 'revoke' and row:
                db.execute('UPDATE gate_qr_links SET enabled=0,updated=? WHERE branch_id=? AND gate_id=?', (self.clock(), str(branch_id), gate_id))
            elif action in {'create', 'rotate'} and (not row or action == 'rotate' or not row['enabled']):
                # Enabling an old revoked link always creates a new URL.
                raw = secrets.token_urlsafe(32)
                db.execute('INSERT INTO gate_qr_links VALUES(?,?,?,1,?,?) ON CONFLICT(branch_id,gate_id) DO UPDATE SET token=excluded.token,enabled=1,updated=excluded.updated',
                           (str(branch_id), gate_id, raw, self.clock(), self.clock()))
            row = db.execute('SELECT * FROM gate_qr_links WHERE branch_id=? AND gate_id=?', (str(branch_id), gate_id)).fetchone()
            active = bool(row and row['enabled'])
            return {'enabled': active, 'gate_id': gate.stable_id, 'gate_label': gate.label,
                    'branch_id': str(branch.branch_id), 'url': config.BASE_URL.rstrip('/') + '/gate/' + row['token'] if active else None}

    def _active(self, data, branch_id):
        if not data or str(data.get('branch_id')) != str(branch_id) or not access_is_active(data):
            return False
        return not self.access.billing or self.access.billing.subscription_view(str(branch_id))['available']

    def _device(self, db, branch_id, raw):
        if not isinstance(raw, str) or len(raw) > 128:
            return None
        row = db.execute('SELECT * FROM gate_qr_devices WHERE token_hash=? AND branch_id=? AND revoked=0 AND expires>?',
                         (self.identity._hash(raw), str(branch_id), self.clock())).fetchone()
        if row:
            data = self.links.load_links().get(row['client_token'])
            if data and str(data.get('branch_id')) == str(branch_id) and normalize_phone(data.get('phone')) == row['phone']:
                return row, data
            db.execute('UPDATE gate_qr_devices SET revoked=1 WHERE token_hash=?', (row['token_hash'],))
        return None

    def state(self, raw, device=None):
        with self.product.connect() as db:
            _, branch, gate = self._public_gate(db, raw)
            result = {'state': 'phone_required', 'branch_id': str(branch.branch_id), 'branch_name': branch.name,
                      'gate_label': gate.label, 'contacts': [{'label': b.label, 'phone': b.phone} for b in branch.buttons if b.type == 'admin'],
                      'branch_logo': branch.logo if branch.logo.startswith('/media/branch-logos/') or branch.logo.startswith('/static/') else '',
                      'retry_after': 0, 'server_time': self.clock()}
            hours=access_hours_state(self.branches,branch.branch_id,at=self.clock())
            result['access_hours']=hours
            if not hours['open']:
                return {**result,'state':'closed','message':CLOSED_MESSAGE}
            verified = self._device(db, branch.branch_id, device)
            if verified:
                row, data = verified
                result.update(state='active' if self._active(data, branch.branch_id) else 'no_pass', verified=True,
                              user_name=data.get('user_name', ''), phone_hint='••• ' + row['phone'][-4:],
                              retry_after=self.access.cooldown_remaining(data),
                              preferences_url=None if data.get('is_temporary') else '/preferences/' + row['client_token'],
                              is_temporary=bool(data.get('is_temporary')),
                              expires_at=None if data.get('admin_role_active') else data.get('access_expires'))
            return result

    def _probe(self, db, ip):
        now = self.clock()
        db.execute('DELETE FROM gate_qr_probes WHERE created<?', (now - 3600,))
        ip_hash = self.identity._hash(ip)
        if db.execute('SELECT count(*) FROM gate_qr_probes WHERE ip_hash=? AND created>?', (ip_hash, now - 600)).fetchone()[0] >= 30:
            raise OtpRateLimit(600, 'Слишком много попыток. Повторите через 10 минут.')
        db.execute('INSERT INTO gate_qr_probes VALUES(?,?,?)', (uuid.uuid4().hex, ip_hash, now))

    def request_code(self, raw, phone, browser, ip=''):
        now, challenge, code = self.clock(), uuid.uuid4().hex, f'{secrets.randbelow(1000000):06d}'
        with self.product.connect() as db:
            _, branch, _ = self._public_gate(db, raw)
            if not access_hours_state(self.branches,branch.branch_id,at=self.clock())['open']:
                return {'state':'closed','message':CLOSED_MESSAGE}
            phone=valid_phone(phone)
            self._probe(db, ip)
            records = [(token, data) for token, data in self.links.load_links().items()
                       if str(data.get('branch_id')) == str(branch.branch_id) and not data.get('system_phone') and normalize_phone(data.get('phone')) == phone]
            if not records:
                return {'state': 'not_found', 'message': 'Такой номер не найден среди клиентов этого филиала. Свяжитесь с администратором.'}
            if len(records) != 1:
                return {'state': 'ambiguous', 'message': 'Для этого номера найдено несколько записей. Администратор поможет уточнить пропуск.'}
            token, data = records[0]
            if not self._active(data, branch.branch_id):
                return {'state': 'no_pass', 'message': 'У вас нет действующего пропуска в этот филиал. Свяжитесь с администратором.'}
            recent = db.execute('SELECT created FROM identity_otp WHERE phone=? ORDER BY created DESC LIMIT 1', (phone,)).fetchone()
            if recent and recent['created'] > now - 60:
                raise OtpRateLimit(math.ceil(recent['created'] + 60 - now), 'Дождитесь окончания таймера повторной отправки.')
            ip_hash = self.identity._hash(ip)
            count = db.execute('SELECT count(*) FROM identity_otp WHERE created>? AND (phone=? OR ip_hash=?)', (now - 3600, phone, ip_hash)).fetchone()[0]
            if count >= 10:
                raise OtpRateLimit(3600, 'Лимит кодов исчерпан. Повторите через час.')
            channel = qr_settings(self.identity, db, branch.branch_id)['channel']
            subject = 'gate_qr:' + str(branch.branch_id) + ':' + self.identity._hash(phone)
            db.execute('UPDATE identity_otp SET state="superseded" WHERE subject=? AND state IN ("sending","ready")', (subject,))
            db.execute('INSERT INTO identity_otp(id,subject,phone,code_hash,expires,state,created,ip_hash) VALUES(?,?,?,?,?,"sending",?,?)',
                       (challenge, subject, phone, self.identity._code_hash(challenge, code), now + 300, now, ip_hash))
            db.execute('INSERT INTO gate_qr_challenges VALUES(?,?,?,?,?)',
                       (challenge, self.identity._hash(raw), self.identity._hash(browser), str(branch.branch_id), token))
        from message_templates import verification_code
        delivery = self.identity._send(channel, phone, verification_code(code),
                                      {'event': 'verification_code', 'purpose': 'otp', 'idempotency_key': challenge, 'branch_id': str(branch.branch_id)})
        ready = delivery.get('status') in {'sent', 'queued', 'success', 'network_accepted', 'accepted'} and delivery.get('delivery') not in {'simulated', 'dry_run', 'stub'}
        with self.product.connect() as db:
            # A later challenge may already have superseded this one.
            db.execute('UPDATE identity_otp SET state=?,delivery=? WHERE id=? AND state="sending"',
                       ('ready' if ready else 'failed', json.dumps({**delivery, 'channel':channel}), challenge))
        return {'state': 'code_required' if ready else 'delivery_failed', 'challenge_id': challenge,
                'retry_after': 60, 'phone_hint': '••• ' + phone[-4:], 'channel': channel, 'ready': ready,
                'message': 'Введите код из сообщения.' if ready else 'Не удалось отправить код. Повторите через минуту или обратитесь к администратору.'}

    def challenge_status(self, raw, browser):
        with self.product.connect() as db:
            _,branch,_=self._public_gate(db, raw)
            if not access_hours_state(self.branches,branch.branch_id,at=self.clock())['open']:
                return {'state':'closed','message':CLOSED_MESSAGE,'challenge_id':None,'ready':False,'retry_after':0}
            row = db.execute('SELECT o.* FROM identity_otp o JOIN gate_qr_challenges q ON q.challenge_id=o.id WHERE q.qr_hash=? AND q.browser_hash=? ORDER BY o.created DESC LIMIT 1',
                             (self.identity._hash(raw), self.identity._hash(browser))).fetchone()
            if not row:
                return {'challenge_id': None, 'ready': False, 'retry_after': 0}
            return {'challenge_id': row['id'], 'channel': json.loads(row['delivery'] or '{}').get('channel', 'sms'),
                    'ready': row['state'] == 'ready' and row['expires'] > self.clock() and row['attempts'] < 5,
                    'phone_hint': '••• ' + row['phone'][-4:], 'retry_after': max(0, math.ceil(row['created'] + 60 - self.clock()))}

    def verify_code(self, raw, browser, challenge, code, *, remember=True):
        device = None
        with self.product.connect() as db:
            _, branch, _ = self._public_gate(db, raw)
            if not access_hours_state(self.branches,branch.branch_id,at=self.clock())['open']:
                raise PermissionError(CLOSED_MESSAGE)
            if type(remember) is not bool:
                raise ValueError('Некорректная настройка запоминания устройства.')
            if not isinstance(challenge, str) or not re.fullmatch(r'[0-9a-f]{32}', challenge):
                raise ValueError('Запросите новый код.')
            row = db.execute('SELECT q.*,o.subject FROM gate_qr_challenges q JOIN identity_otp o ON o.id=q.challenge_id WHERE q.challenge_id=? AND q.qr_hash=? AND q.browser_hash=?',
                             (challenge, self.identity._hash(raw), self.identity._hash(browser))).fetchone()
            otp = self.identity._check_otp(db, row['subject'], challenge, code) if row else None
            if otp:
                data = self.links.load_links().get(row['client_token'])
                if data and normalize_phone(data.get('phone')) == otp['phone'] and self._active(data, branch.branch_id):
                    device = secrets.token_urlsafe(32)
                    db.execute('INSERT INTO gate_qr_devices VALUES(?,?,?,?,?,0,?)',
                               (self.identity._hash(device), str(branch.branch_id), row['client_token'], otp['phone'], self.clock() + (DEVICE_SECONDS if remember else SESSION_DEVICE_SECONDS), self.clock()))
                    db.execute('UPDATE identity_otp SET state="used" WHERE id=?', (challenge,))
        # Raising outside the transaction preserves the attempt budget.
        if device is None:
            raise ValueError('Код неверен или истёк, либо пропуск изменился. Запросите новый код.')
        return device

    def forget_device(self, raw, device):
        with self.product.connect() as db:
            _, branch, _ = self._public_gate(db, raw)
            db.execute('UPDATE gate_qr_devices SET revoked=1 WHERE token_hash=? AND branch_id=?',
                       (self.identity._hash(device or ''), str(branch.branch_id)))

    def open(self, raw, device, request_id):
        with self.product.connect() as db:
            _, branch, gate = self._public_gate(db, raw)
            if not access_hours_state(self.branches,branch.branch_id,at=self.clock())['open']:
                return {'status':'error','code':'outside_working_hours','http_status':403,'message':CLOSED_MESSAGE,'confirmed':False}
            verified = self._device(db, branch.branch_id, device)
            if not verified or not self._active(verified[1], branch.branch_id):
                raise PermissionError('Подтвердите номер телефона и действующий пропуск.')
            row, _ = verified
            token, phone = row['client_token'], row['phone']
        # The shared service rechecks authorization under its admission lock and
        # owns the quota, idempotency, cooldown and single hardware queue.
        return self.access.open_for_token(token, gate.id, request_id=request_id,
                                          expected_phone=phone, expected_gate_id=gate.stable_id)
