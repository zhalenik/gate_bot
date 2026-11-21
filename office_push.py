"""Authenticated administrator Web Push for browsers and the mobile office."""
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
            if 'surface' not in {r['name'] for r in db.execute('PRAGMA table_info(office_push_devices)')}:
                db.execute("ALTER TABLE office_push_devices ADD COLUMN surface TEXT NOT NULL DEFAULT 'office'")

    def actor(self, db, username, *, desktop=False):
        admin = self.identity._admin(db, username)
        if not admin['person_id']:
            raise PermissionError('Свяжите аккаунт с телефоном для получения уведомлений.')
        if admin.get('is_super') and not desktop:
            raise PermissionError('Мобильный кабинет доступен администраторам филиала.')
        if not admin.get('is_super'):
            self.identity._scope(db, username, admin['branch_id'])
        return admin

    def settings(self, username, endpoint=None, *, desktop=False):
        from product_notifications import TITLES
        with self.store.connect() as db:
            admin = self.actor(db, username, desktop=desktop)
            prefs = self.identity.admin_preferences(username)
            events = prefs['active_events']
            row = None
            if endpoint:
                digest = hashlib.sha256(validate_endpoint(endpoint).encode()).hexdigest()
                row = db.execute('SELECT * FROM office_push_devices WHERE username=? AND endpoint_hash=? AND proof=?',
                                 (username, digest, self.identity.push_proof(admin))).fetchone()
            choices = self.identity.saved_push_choices(db,admin['person_id'])
            legacy = json.loads(row['events']) if row else ['client_message','pass_requested']
            return {'public_key': self.pwa._keys(db)['public'], 'connected': bool(row), 'device': row['id'] if row else None,
                    'events': {event:TITLES[event] for event in events if event in TITLES},
                    'selected': [e for e in events if choices.get(e,e in legacy)]}

    def subscribe(self, username, subscription, selected, consent, *, desktop=False):
        if consent is not True:
            raise ValueError('Разрешите push-уведомления на этом устройстве.')
        subscription = normalize_subscription(subscription)
        digest = hashlib.sha256(subscription['endpoint'].encode()).hexdigest()
        with self.store.connect() as db:
            admin = self.actor(db, username, desktop=desktop)
            prefs = self.identity.admin_preferences(username)
            available = prefs['active_events']
            if selected is None:
                choices = prefs['events']
                if any('push' in channels for channels in choices.values()):
                    selected = [e for e in available if choices[e].get('push')]
                else:
                    selected = [e for e in ('client_message','pass_requested') if e in available]
            if not isinstance(selected,list) or any(not isinstance(e,str) or e not in available for e in selected):
                raise ValueError('Выберите события из списка.')
            row = db.execute('SELECT * FROM office_push_devices WHERE endpoint_hash=?',(digest,)).fetchone()
            if (row and (row['person_id']!=admin['person_id'] or row['username']!=username) or
                    db.execute('SELECT 1 FROM pwa_subscriptions WHERE endpoint_hash=?',(digest,)).fetchone()):
                raise PermissionError('Устройство уже связано с другим приложением или аккаунтом.')
            if not row and db.execute('SELECT count(*) FROM office_push_devices WHERE person_id=?',(admin['person_id'],)).fetchone()[0]>=12:
                raise ValueError('Достигнут предел подключённых устройств.')
            device = row['id'] if row else uuid.uuid4().hex
            cipher = self.identity._fernet().encrypt(json.dumps(subscription).encode()).decode()
            db.execute('INSERT OR REPLACE INTO office_push_devices(id,username,person_id,branch_id,proof,endpoint_hash,ciphertext,events,surface) VALUES(?,?,?,?,?,?,?,?,?)',
                       (device, username, admin['person_id'],str(admin.get('branch_id') or ''),self.identity.push_proof(admin),
                        digest,cipher,json.dumps(sorted(set(selected))), 'desktop' if desktop else 'office'))
            return device

    def disable(self, username, *, endpoint=None, device=None):
        with self.store.connect() as db:
            if device:
                db.execute('DELETE FROM office_push_devices WHERE username=? AND id=?',(username,device))
            elif endpoint:
                digest = hashlib.sha256(validate_endpoint(endpoint).encode()).hexdigest()
                db.execute('DELETE FROM office_push_devices WHERE username=? AND endpoint_hash=?',(username,digest))

    def targets(self, db, admin, event):
        choices = self.identity.saved_push_choices(db,admin['person_id'])
        return [r['id'] for r in self.identity.admin_push_devices(db,admin)
                if choices.get(event,event in json.loads(r['events']))]

    def recipient(self, db, device, branch, event):
        row = db.execute('SELECT * FROM office_push_devices WHERE id=?',(device,)).fetchone()
        if row:
            try:
                admin = self.actor(db, row['username'], desktop=row['surface']=='desktop')
            except (ValueError, PermissionError):
                return None
            choices = self.identity.saved_push_choices(db,admin['person_id'])
            if (self.identity.push_proof(admin)==row['proof'] and self.identity.event_selected(db,admin['person_id'],event)
                    and admin_receives_event(admin,branch,event) and choices.get(event,event in json.loads(row['events']))):
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
        desktop = row['surface']=='desktop'
        url = '/admin/notifications' if desktop else '/office/notifications'
        if re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',thread):
            url = ('/admin/client-messages?thread=' if desktop else '/office/messages?thread=')+thread
        result = self.pwa.transport(subscription,{'title':'GateBot · Кабинет','body':'В кабинете появилось новое событие.','url':url},
                                    key,origin.scheme+'://'+origin.netloc)
        if result.get('status')=='expired':
            self.disable(row['username'],device=device)
        return result
