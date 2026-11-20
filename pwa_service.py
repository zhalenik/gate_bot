"""Private browser subscriptions, explicit consent and client-scoped delivery."""
import base64
import hashlib
import json
import math
import re
import uuid
from urllib.parse import quote, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from link_manager import access_is_active
from pass_notifications import timely_grant
from storage import config
from storage.sql_documents import read_document
from webpush_delivery import send_webpush, validate_endpoint


def _b64(value):
    return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')


def _decode(value, length):
    if not isinstance(value, str) or len(value) > 4*((length+2)//3) or not re.fullmatch(r'[A-Za-z0-9_-]+={0,2}', value):
        raise ValueError('Некорректный ключ push-подписки')
    try:
        result = base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, TypeError):
        raise ValueError('Некорректный ключ push-подписки') from None
    if len(result) != length:
        raise ValueError('Некорректный ключ push-подписки')
    return result


def normalize_subscription(value):
    if not isinstance(value, dict) or set(value) - {'endpoint','keys','expirationTime'}:
        raise ValueError('Некорректная push-подписка')
    endpoint = validate_endpoint(value.get('endpoint'))
    keys = value.get('keys')
    if not isinstance(keys, dict) or set(keys) != {'p256dh','auth'}:
        raise ValueError('Некорректные ключи push-подписки')
    public = _decode(keys['p256dh'], 65)
    ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    auth = _decode(keys['auth'], 16)
    expires = value.get('expirationTime')
    if expires is not None and (type(expires) not in (int, float) or not math.isfinite(expires) or expires <= 0):
        raise ValueError('Некорректный срок push-подписки')
    return {'endpoint':endpoint, 'keys':{'p256dh':_b64(public), 'auth':_b64(auth)}, 'expirationTime':expires}


class PwaService:
    def __init__(self, identity, *, transport=None):
        self.identity = identity
        self.store = identity.product
        self.transport = transport or send_webpush
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS pwa_keys(name TEXT PRIMARY KEY,ciphertext TEXT NOT NULL,created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS pwa_subscriptions(id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,person_id TEXT NOT NULL,client_token TEXT NOT NULL,endpoint_hash TEXT UNIQUE NOT NULL,ciphertext TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS pwa_subscription_client ON pwa_subscriptions(branch_id,person_id,client_token)')
        from office_push import OfficePush
        self.office = OfficePush(self)

    def _resolve(self, db, token, *, allow_guest=False):
        client = read_document(db, 'links.json').get(token)
        if client and allow_guest:
            person, branch, legacy = self.identity._client(db, token, client), str(client['branch_id']), token
        else:
            person, branch, legacy, client = self.identity._preference_identity(db, token)
        branch_data = read_document(db, 'branches.json').get(branch)
        if not branch_data:
            raise ValueError('Ссылка недействительна')
        return person, branch, legacy, client, branch_data

    def metadata(self, token):
        with self.store.connect() as db:
            _, branch, legacy, client, branch_data = self._resolve(db, token, allow_guest=True)
            scope = '/pwa/' + quote(legacy, safe='') + '/'
            return {'token':legacy, 'branch_id':branch, 'branch_name':branch_data.get('name') or 'GateBot',
                    'guest':bool(client.get('is_temporary')), 'allow_push':not bool(client.get('is_temporary')), 'scope':scope, 'gate_url':scope+'gate',
                    'preferences_url':None if client.get('is_temporary') else scope+'preferences'}

    def _keys(self, db):
        row = db.execute('SELECT ciphertext FROM pwa_keys WHERE name="vapid"').fetchone()
        if row:
            return json.loads(self.identity._fernet().decrypt(row['ciphertext'].encode()))
        private = ec.generate_private_key(ec.SECP256R1())
        keys = {'private':_b64(private.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                'public':_b64(private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))}
        ciphertext = self.identity._fernet().encrypt(json.dumps(keys).encode()).decode()
        db.execute('INSERT INTO pwa_keys VALUES("vapid",?,?)', (ciphertext, self.identity.clock()))
        return keys

    def status(self, token):
        with self.store.connect() as db:
            person, branch, legacy, _, _ = self._resolve(db, token)
            count = db.execute('SELECT count(*) FROM pwa_subscriptions WHERE branch_id=? AND person_id=? AND client_token=?', (branch,person,legacy)).fetchone()[0]
            prefs = self.identity.prefs_for_token(token)
            return {'supported':True, 'public_key':self._keys(db)['public'], 'subscription_count':count,
                    'channel_enabled':bool(prefs['effective_channels'].get('push')), 'preferences':prefs}

    def subscribe(self, token, subscription, *, consent=False):
        if consent is not True:
            raise ValueError('Подтвердите подключение push-уведомлений')
        subscription = normalize_subscription(subscription)
        fingerprint = hashlib.sha256(subscription['endpoint'].encode()).hexdigest()
        with self.store.connect() as db:
            person, branch, legacy, _, _ = self._resolve(db, token)
            existing = db.execute('SELECT * FROM pwa_subscriptions WHERE endpoint_hash=?', (fingerprint,)).fetchone()
            if db.execute('SELECT 1 FROM office_push_devices WHERE endpoint_hash=?',(fingerprint,)).fetchone():
                raise ValueError('Устройство уже подключено к кабинету филиала.')
            if existing and (existing['person_id'],existing['branch_id'],existing['client_token']) != (person,branch,legacy):
                raise ValueError('Это устройство уже связано с другим пропуском')
            if not existing and db.execute('SELECT count(*) FROM pwa_subscriptions WHERE client_token=?', (legacy,)).fetchone()[0] >= 20:
                raise ValueError('Достигнут предел подключённых устройств')
            self._keys(db)
            encrypted = self.identity._fernet().encrypt(json.dumps(subscription).encode()).decode()
            now = self.identity.clock()
            db.execute('INSERT INTO pwa_subscriptions VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(endpoint_hash) DO UPDATE SET ciphertext=excluded.ciphertext,updated=excluded.updated',
                (uuid.uuid4().hex,branch,person,legacy,fingerprint,encrypted,now,now))
            prefs = self.identity.prefs_for_token(token)
            channels = dict(prefs['channels']) if prefs['enabled'] else {channel:False for channel in prefs['channels']}
            channels['push'] = True
            self.identity.save_prefs_for_token(token, {'enabled':True, 'channels':channels})
        return self.status(token)

    def unsubscribe(self, token, endpoint):
        fingerprint = hashlib.sha256(validate_endpoint(endpoint).encode()).hexdigest()
        with self.store.connect() as db:
            person, branch, legacy, _, _ = self._resolve(db, token)
            db.execute('DELETE FROM pwa_subscriptions WHERE endpoint_hash=? AND branch_id=? AND person_id=? AND client_token=?', (fingerprint,branch,person,legacy))
            if not db.execute('SELECT 1 FROM pwa_subscriptions WHERE branch_id=? AND person_id=? AND client_token=?', (branch,person,legacy)).fetchone():
                prefs = self.identity.prefs_for_token(token)
                self.identity.save_prefs_for_token(token, {'enabled':prefs['enabled'], 'channels':{**prefs['channels'], 'push':False}})
        return self.status(token)

    def targets(self, db, token, branch, person):
        return [row['id'] for row in db.execute('SELECT id FROM pwa_subscriptions WHERE client_token=? AND branch_id=? AND person_id=? ORDER BY created,id', (token,str(branch),person))]

    def allowed(self, db, subscription_id, token, branch, *, revision=None, event=None):
        row = db.execute('SELECT * FROM pwa_subscriptions WHERE id=? AND client_token=? AND branch_id=?', (subscription_id,token,str(branch))).fetchone()
        if not row:
            return False
        try:
            person, resolved_branch, legacy, client, _ = self._resolve(db, token)
            if (person,resolved_branch,legacy) != (row['person_id'],row['branch_id'],row['client_token']):
                return False
            if revision is not None and client.get('access_revision',0) != revision:
                return False
            if event not in (None, 'desk_reply') and (not timely_grant(event, client) or not access_is_active(client)):
                return False
            return bool(self.identity.prefs_for_token(token)['effective_channels'].get('push'))
        except (ValueError, PermissionError):
            return False

    def deliver(self, subscription_id, context):
        token, branch = context.get('client_token'), context.get('branch_id')
        with self.store.connect() as db:
            if not self.allowed(db, subscription_id, token, branch, revision=context.get('access_revision'), event=context.get('event')):
                return {'status':'skipped', 'code':'push_recipient_changed', 'confirmed':False}
            row = db.execute('SELECT ciphertext FROM pwa_subscriptions WHERE id=?', (subscription_id,)).fetchone()
            subscription = normalize_subscription(json.loads(self.identity._fernet().decrypt(row['ciphertext'].encode())))
            private = self._keys(db)['private']
        # Network runs outside the application write transaction.
        origin = urlsplit(config.BASE_URL)
        if origin.scheme != 'https' or not origin.hostname:
            return {'status':'failed', 'code':'push_requires_https', 'confirmed':False}
        subject = origin.scheme + '://' + origin.netloc
        payload = {'title':'GateBot', 'body':'Администратор ответил на ваше обращение.' if context.get('event')=='desk_reply' else 'Ваш пропуск активен. Откройте приложение, чтобы открыть ворота.',
                   'url':'/pwa/'+quote(token,safe='')+'/gate', 'tag':'gatebot-pass'}
        if context.get('event')=='desk_reply':
            payload['tag']='gatebot-message'
        result = self.transport(subscription, payload, private, subject)
        if result.get('status') == 'expired':
            with self.store.connect() as db:
                db.execute('DELETE FROM pwa_subscriptions WHERE id=?', (subscription_id,))
        return result
