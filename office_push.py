"""Device-specific Web Push for the branch administrator's office."""
import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

from identity_service import admin_receives_event
from pwa_service import normalize_subscription
from storage import config
from webpush_delivery import validate_endpoint


class OfficePush:
    def __init__(self, pwa):
        self.pwa, self.identity, self.store = pwa, pwa.identity, pwa.store
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS office_push_devices(id TEXT PRIMARY KEY, username TEXT NOT NULL, '
                       'person_id TEXT NOT NULL,branch_id TEXT NOT NULL,proof TEXT NOT NULL,endpoint_hash TEXT UNIQUE NOT NULL,'
                       'ciphertext TEXT NOT NULL,events TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS office_push_owner ON office_push_devices(username,branch_id)')

    def actor(self, db, username):
        admin = self.identity._admin(db, username)
        if admin.get('is_super') or not admin['person_id']:
            raise PermissionError('Мобильный кабинет доступен администраторам филиала.')
        self.identity._scope(db, username, admin['branch_id'])
        return admin

    def proof(self, admin):
        return self.identity._hash(json.dumps([admin['person_id'],admin['branch_id'],admin.get('password_hash'),
                                               admin.get('session_version', 0)]))

    def settings(self, username, endpoint=None):
        from product_notifications import TITLES
        with self.store.connect() as db:
            admin = self.actor(db, username)
            events = self.identity.admin_preferences(username)['events']
            row = None
            if endpoint:
                digest = hashlib.sha256(validate_endpoint(endpoint).encode()).hexdigest()
                row = db.execute('SELECT * FROM office_push_devices WHERE username=? AND endpoint_hash=? AND proof=?',
                                 (username, digest, self.proof(admin))).fetchone()
            return {'public_key': self.pwa._keys(db)['public'], 'connected': bool(row),
                    'events': {event:TITLES[event] for event in events if event in TITLES},
                    'selected': json.loads(row['events']) if row else ['client_message','pass_requested']}

    def subscribe(self, username, subscription, selected, consent):
        if consent is not True:
            raise ValueError('Разрешите push-уведомления на этом устройстве.')
        subscription = normalize_subscription(subscription)
        digest = hashlib.sha256(subscription['endpoint'].encode()).hexdigest()
        with self.store.connect() as db:
            admin = self.actor(db, username)
            available = self.identity.admin_preferences(username)['events']
            if not isinstance(selected,list) or any(not isinstance(e,str) or e not in available for e in selected):
                raise ValueError('Выберите события из списка.')
            row = db.execute('SELECT * FROM office_push_devices WHERE endpoint_hash=?',(digest,)).fetchone()
            if (row and row['person_id']!=admin['person_id'] or
                    db.execute('SELECT 1 FROM pwa_subscriptions WHERE endpoint_hash=?',(digest,)).fetchone()):
                raise PermissionError('Устройство уже связано с другим приложением или аккаунтом.')
            if not row and db.execute('SELECT count(*) FROM office_push_devices WHERE person_id=?',(admin['person_id'],)).fetchone()[0]>=12:
                raise ValueError('Достигнут предел подключённых устройств.')
            device = row['id'] if row else uuid.uuid4().hex
            cipher = self.identity._fernet().encrypt(json.dumps(subscription).encode()).decode()
            db.execute('INSERT OR REPLACE INTO office_push_devices VALUES(?,?,?,?,?,?,?,?)',
                       (device, username, admin['person_id'],str(admin['branch_id']),self.proof(admin),digest,cipher,json.dumps(sorted(set(selected)))))
            return device

    def disable(self, username, *, endpoint=None, device=None):
        with self.store.connect() as db:
            if device:
                db.execute('DELETE FROM office_push_devices WHERE username=? AND id=?',(username,device))
            elif endpoint:
                digest = hashlib.sha256(validate_endpoint(endpoint).encode()).hexdigest()
                db.execute('DELETE FROM office_push_devices WHERE username=? AND endpoint_hash=?',(username,digest))

    def targets(self, db, admin, event):
        if admin.get('is_super'):
            return []
        return [r['id'] for r in db.execute('SELECT id,events FROM office_push_devices WHERE person_id=? AND branch_id=? AND proof=?',
                (admin['person_id'],str(admin['branch_id']),self.proof(admin))) if event in json.loads(r['events'])]

    def recipient(self, db, device, branch, event):
        row = db.execute('SELECT * FROM office_push_devices WHERE id=? AND branch_id=?',(device,str(branch))).fetchone()
        if row:
            try:
                admin = self.actor(db, row['username'])
            except (ValueError, PermissionError):
                return None
            if self.proof(admin)==row['proof'] and admin_receives_event(admin,branch,event) and event in json.loads(row['events']):
                return row
        return None

    def deliver(self, device, context):
        with self.store.connect() as db:
            row = self.recipient(db,device,context.get('branch_id'),context.get('event'))
            if not row:
                return {'status':'skipped','code':'office_device_changed'}
            subscription = normalize_subscription(json.loads(self.identity._fernet().decrypt(row['ciphertext'].encode())))
            key = self.pwa._keys(db)['private']
        origin = urlsplit(config.BASE_URL)
        if origin.scheme!='https' or not origin.hostname:
            return {'status':'failed','code':'push_requires_https'}
        thread = str(context.get('thread_id') or '')
        url = '/office/messages?thread='+thread if re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',thread) else '/office/notifications'
        result = self.pwa.transport(subscription,{'title':'GateBot · Кабинет','body':'Откройте кабинет филиала, чтобы посмотреть новое событие.','url':url},
                                    key,origin.scheme+'://'+origin.netloc)
        if result.get('status')=='expired':
            self.disable(row['username'],device=device)
        return result
