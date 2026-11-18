"""People, branch administration, expiring invitations and verified identities.

No network or hardware on import. Every delivery is an injected callback, called
after its durable claim. The runtime supplies transports; tests supply fakes.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
import uuid

from admin_auth import AdminAuth
from link_manager import normalize_phone
from storage import config
from storage.product_store import ProductStore
from storage.sql_documents import setup_documents, read_document, write_document


CLIENT_EVENTS = ('access_granted', 'access_changed', 'access_revoked')
ADMIN_EVENTS = ('gate_requested', 'gate_denied', 'gate_error', 'admin_contact_requested',
                'invitation_accepted', 'access_changed', 'crm_error', 'crm_recovered',
                'modem_error', 'modem_recovered', 'delivery_failed', 'subscription_expiring',
                'subscription_expired', 'quota_warning', 'quota_exhausted', 'sms_received', 'call_received')
CHANNELS = ('email', 'telegram', 'max', 'chatpush', 'sms')
CLIENT_CHANNELS = ('email', 'telegram', 'max', 'messengers')
ADMIN_CHANNELS = ('email', 'telegram', 'max')
DEFAULT_ADMIN_EVENTS = frozenset(('subscription_expiring', 'subscription_expired', 'quota_warning', 'quota_exhausted'))


def default_admin_preferences():
    return {'events': {event: {channel: channel == 'telegram' and event in DEFAULT_ADMIN_EVENTS
                              for channel in ADMIN_CHANNELS} for event in ADMIN_EVENTS}}


def valid_phone(value):
    if not isinstance(value, str) or not re.fullmatch(r'[+0-9\s().-]{7,64}', value):
        raise ValueError('Введите номер телефона с кодом страны')
    value = normalize_phone(value)
    if not re.fullmatch(r'\+[1-9][0-9]{6,14}', value or ''):
        raise ValueError('Введите номер телефона с кодом страны')
    return value


def valid_email(value):
    if not value:
        return ''
    if not isinstance(value, str) or any(ord(char)<32 or ord(char)==127 for char in value):
        raise ValueError('Введите корректный email')
    value=value.strip().lower()
    if len(value)>254 or value.count('@')!=1:
        raise ValueError('Введите корректный email')
    local,domain=value.split('@')
    try:domain=domain.encode('idna').decode('ascii')
    except UnicodeError:raise ValueError('Введите корректный email') from None
    if not local or len(local)>64 or local.startswith('.') or local.endswith('.') or '..' in local or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+",local) or '.' not in domain or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?',part) for part in domain.split('.')):
        raise ValueError('Введите корректный email')
    normalized=local+'@'+domain
    if len(normalized)>254:raise ValueError('Введите корректный email')
    return normalized


class OtpRateLimit(ValueError):
    def __init__(self, retry_after, message='Повторный код пока недоступен'):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class IdentityService:
    def __init__(self, product=None, sender=None, clock=None):
        self.product = product or ProductStore()
        self.sender = sender
        self.event_sink = None
        self.welcome_sink = None
        self.delivery_status = None
        self.clock = clock or time.time
        with self.product.connect() as db:
            setup_documents(db)
            statements = [
                'CREATE TABLE IF NOT EXISTS persons(id TEXT PRIMARY KEY,name TEXT NOT NULL,phone TEXT,phone_verified INTEGER NOT NULL DEFAULT 0,email TEXT NOT NULL DEFAULT "",email_verified INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS branch_clients(id TEXT PRIMARY KEY,person_id TEXT NOT NULL REFERENCES persons(id),branch_id TEXT NOT NULL,legacy_token TEXT UNIQUE,created REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS web_accounts(username TEXT PRIMARY KEY,person_id TEXT NOT NULL REFERENCES persons(id))',
                'CREATE TABLE IF NOT EXISTS branch_memberships(person_id TEXT NOT NULL REFERENCES persons(id),branch_id TEXT NOT NULL,status TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(person_id,branch_id))',
                'CREATE TABLE IF NOT EXISTS system_roles(person_id TEXT PRIMARY KEY REFERENCES persons(id),role TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS bot_bindings(platform TEXT NOT NULL,user_id TEXT NOT NULL,person_id TEXT NOT NULL REFERENCES persons(id),assurance TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(platform,user_id),UNIQUE(platform,person_id))',
                'CREATE TABLE IF NOT EXISTS identity_invitations(id TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,branch_id TEXT NOT NULL,phone TEXT,email TEXT NOT NULL,client_token TEXT,inviter TEXT NOT NULL,expires REAL NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,delivery TEXT)',
                'CREATE TABLE IF NOT EXISTS identity_otp(id TEXT PRIMARY KEY,subject TEXT NOT NULL,phone TEXT NOT NULL,code_hash TEXT NOT NULL,expires REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,state TEXT NOT NULL,created REAL NOT NULL,ip_hash TEXT NOT NULL,delivery TEXT)',
                'CREATE TABLE IF NOT EXISTS identity_tickets(token_hash TEXT PRIMARY KEY,person_id TEXT NOT NULL,platform TEXT NOT NULL,purpose TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL DEFAULT "{}")',
                'CREATE TABLE IF NOT EXISTS notification_preferences(person_id TEXT NOT NULL,audience TEXT NOT NULL,branch_id TEXT NOT NULL DEFAULT "",payload TEXT NOT NULL,PRIMARY KEY(person_id,audience,branch_id))',
                'CREATE TABLE IF NOT EXISTS branch_registrations(id TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,branch_id TEXT NOT NULL,name TEXT NOT NULL,person_name TEXT NOT NULL,phone TEXT NOT NULL,expires REAL NOT NULL,state TEXT NOT NULL,created REAL NOT NULL)',
                'CREATE INDEX IF NOT EXISTS identity_otp_limits ON identity_otp(phone,created)',
                'CREATE INDEX IF NOT EXISTS branch_clients_person ON branch_clients(person_id,branch_id)',
                'CREATE TABLE IF NOT EXISTS identity_integrations(branch_id TEXT NOT NULL,kind TEXT NOT NULL,ciphertext TEXT NOT NULL,updated REAL NOT NULL,PRIMARY KEY(branch_id,kind))',
            ]
            for sql in statements:
                db.execute(sql)
            # Freeze the old effective choices once, before changing defaults for new accounts.
            db.execute('CREATE TABLE IF NOT EXISTS identity_migrations(name TEXT PRIMARY KEY,applied REAL NOT NULL)')
            if not db.execute('SELECT 1 FROM identity_migrations WHERE name=?', ('admin_notification_defaults_v2',)).fetchone():
                for account in db.execute('SELECT DISTINCT person_id FROM web_accounts').fetchall():
                    self._seed_admin_preferences(db, account['person_id'], legacy=True)
                db.execute('INSERT INTO identity_migrations(name,applied) VALUES(?,?)', ('admin_notification_defaults_v2', self.clock()))
            registration_columns={row['name'] for row in db.execute('PRAGMA table_info(branch_registrations)')}
            if 'yclients_enabled' not in registration_columns:
                db.execute('ALTER TABLE branch_registrations ADD COLUMN yclients_enabled INTEGER NOT NULL DEFAULT 1')
            if 'connection_mode' not in registration_columns:
                db.execute('ALTER TABLE branch_registrations ADD COLUMN connection_mode TEXT NOT NULL DEFAULT "direct"')

    @contextmanager
    def transaction(self, db=None):
        if db is not None:
            yield db
        else:
            with self.product.connect() as connection:
                yield connection

    @staticmethod
    def _hash(value):
        return hashlib.sha256(str(value).encode()).hexdigest()

    @staticmethod
    def _code_hash(challenge, code):
        return hmac.new(config.SECRET_KEY.encode(), (challenge+':'+code).encode(), hashlib.sha256).hexdigest()

    def _admin(self, db, username):
        admin = read_document(db, 'admins.json').get(username)
        if not admin or admin.get('disabled'):
            raise PermissionError('Требуется действующий администратор')
        account = db.execute('SELECT person_id FROM web_accounts WHERE username=?', (username,)).fetchone()
        person_id = account['person_id'] if account else admin.get('person_id')
        branches = [r['branch_id'] for r in db.execute('SELECT branch_id FROM branch_memberships WHERE person_id=? AND status="active"',(person_id,))] if person_id else []
        return self._resolved_admin(admin, username, person_id, branches)

    @staticmethod
    def _resolved_admin(admin, username, person_id, branches):
        if not admin or admin.get('disabled'):
            raise PermissionError('Требуется действующий администратор')
        if not admin.get('is_super'):
            branches=[branch for branch in branches if branch==str(admin.get('branch_id'))]
        if not person_id and admin.get('branch_id'):
            branches = [str(admin['branch_id'])]  # Existing password account remains usable after migration.
        if not admin.get('is_super') and not branches:
            raise PermissionError('Права администратора отозваны')
        return {**admin, 'username': username, 'person_id': person_id, 'branch_ids': branches}

    def web_admin(self, username):
        with self.product.connect() as db:
            return self._admin(db, username)

    def _scope(self, db, username, branch_id):
        admin = self._admin(db, username)
        return self._checked_scope(admin, branch_id, read_document(db, 'branches.json'))

    @staticmethod
    def _checked_scope(admin, branch_id, branches):
        branch_id = str(branch_id)
        if not branch_id.isascii() or not branch_id.isdigit() or int(branch_id) < 1:
            raise ValueError('Некорректный филиал')
        if not admin.get('is_super') and branch_id not in admin['branch_ids']:
            raise PermissionError('Нет доступа к этому филиалу')
        if branch_id not in branches:
            raise ValueError('Филиал не найден')
        return admin

    def _person(self, db, name, phone, *, verified=False):
        person_id = uuid.uuid4().hex
        db.execute('INSERT INTO persons(id,name,phone,phone_verified,created) VALUES(?,?,?,?,?)',
                   (person_id, str(name).strip()[:200], phone, int(verified), self.clock()))
        return person_id

    def _client(self, db, token, data, person_id=None):
        row = db.execute('SELECT person_id FROM branch_clients WHERE legacy_token=?',(token,)).fetchone()
        if row:
            data['person_id']=row['person_id']
            return row['person_id']
        person_id = person_id or self._person(db, data.get('user_name',''), normalize_phone(data.get('phone')) or None)
        email = data.get('email') or ''
        if email:
            try: email = valid_email(email)
            except ValueError: email = ''
            db.execute('UPDATE persons SET email=? WHERE id=? AND email=""', (email, person_id))
        db.execute('INSERT INTO branch_clients(id,person_id,branch_id,legacy_token,created) VALUES(?,?,?,?,?)',
                   (token, person_id, str(data['branch_id']), token, self.clock()))
        data['person_id']=person_id
        return person_id

    def import_legacy(self):
        counts = {'clients':0,'accounts':0,'unlinked_accounts':0,'repaired_admin_clients':0}
        with self.product.connect() as db:
            links = read_document(db,'links.json')
            for token,data in links.items():
                person_id=self._client(db,token,data);counts['clients']+=1
                # Preserve the former profile fallback as an explicit branch contact.
                # An existing empty value is an intentional empty contact, not missing data.
                if 'email' not in data:
                    person=db.execute('SELECT email FROM persons WHERE id=?',(person_id,)).fetchone()
                    data['email']=person['email'] if person else ''
            write_document(db,'links.json',links)
            admins=read_document(db,'admins.json')
            for username,admin in admins.items():
                if not admin.get('person_id'):
                    if admin.get('phone'):
                        admin['person_id']=self._person(db,admin.get('name',''),valid_phone(admin['phone']),verified=True)
                    else:
                        counts['unlinked_accounts']+=1;continue
                person_id=admin['person_id']
                if not db.execute('SELECT 1 FROM persons WHERE id=?',(person_id,)).fetchone():
                    raise ValueError('Администратор ссылается на отсутствующую личность')
                inserted=db.execute('INSERT OR IGNORE INTO web_accounts(username,person_id) VALUES(?,?)',(username,person_id)).rowcount
                if inserted:
                    self._membership(db,person_id,admin.get('branch_id'),bool(admin.get('is_super')),legacy=True)
                elif not admin.get('disabled') and not admin.get('is_super'):
                    branch_id=str(admin.get('branch_id'))
                    membership=db.execute('SELECT 1 FROM branch_memberships WHERE person_id=? AND branch_id=? AND status="active"',(person_id,branch_id)).fetchone()
                    if membership and branch_id in read_document(db,'branches.json'):
                        current=read_document(db,'links.json')
                        existing=any(client.get('person_id')==person_id and str(client.get('branch_id'))==branch_id for client in current.values())
                        self._admin_grant(db,person_id,branch_id)
                        if not existing:counts['repaired_admin_clients']+=1
                counts['accounts']+=1
            write_document(db,'admins.json',admins)
        return counts

    def _seed_admin_preferences(self, db, person_id, *, legacy=False):
        prefs = ({'events': {event: {channel: True for channel in ADMIN_CHANNELS}
                             for event in ADMIN_EVENTS}} if legacy else default_admin_preferences())
        db.execute('INSERT OR IGNORE INTO notification_preferences(person_id,audience,branch_id,payload) VALUES(?,"admin","",?)',
                   (person_id, json.dumps(prefs)))

    def _membership(self, db, person_id, branch_id, is_super=False, *, legacy=False):
        self._seed_admin_preferences(db, person_id, legacy=legacy)
        if is_super:
            db.execute('INSERT OR REPLACE INTO system_roles(person_id,role) VALUES(?,"superadmin")',(person_id,))
            # Global administration is a role, not a client record in every branch.
            links = read_document(db, 'links.json')
            own_branches = {str(client['branch_id']) for client in links.values()
                            if client.get('person_id') == person_id}
            branches = read_document(db, 'branches.json')
            if not own_branches and branches:
                own_branches.add(str(branch_id) if str(branch_id) in branches else next(iter(branches)))
            for own_branch in own_branches:
                self._admin_grant(db, person_id, own_branch)
        elif branch_id:
            if not legacy:self._require_single_branch(db,person_id,branch_id)
            db.execute('INSERT INTO branch_memberships(person_id,branch_id,status,created) VALUES(?, ?,"active",?) ON CONFLICT(person_id,branch_id) DO UPDATE SET status="active"',(person_id,str(branch_id),self.clock()))
            self._admin_grant(db,person_id,str(branch_id))

    def _require_single_branch(self,db,person_id,branch_id):
        accounts=read_document(db,'admins.json')
        for account in accounts.values():
            if account.get('person_id')==person_id and not account.get('is_super') and str(account.get('branch_id'))!=str(branch_id):
                raise ValueError('Администратор уже закреплён за другим филиалом. Обратитесь к суперадминистратору')

    def _admin_grant(self,db,person_id,branch_id):
        links=read_document(db,'links.json')
        person=db.execute('SELECT * FROM persons WHERE id=?',(person_id,)).fetchone()
        bindings={('tg_id' if row['platform']=='telegram' else 'max_id'):row['user_id'] for row in db.execute('SELECT platform,user_id FROM bot_bindings WHERE person_id=? AND assurance="admin_verified"',(person_id,))}
        for row in db.execute('SELECT legacy_token FROM branch_clients WHERE person_id=? AND branch_id=?',(person_id,branch_id)):
            token=row['legacy_token']
            if token in links:
                links[token].update(admin_role_active=True,admin_person_id=person_id)
                if not links[token].get('email') and not links[token].get('email_override'):
                    links[token]['email']=person['email']
                links[token].update(bindings)
                write_document(db,'links.json',links);return token
        token=secrets.token_urlsafe(24)
        links[token]={'branch_id':int(branch_id),'phone':person['phone'],'user_name':person['name'],'email':person['email'],
            'gate_access':False,'access_expires':None,'access_source':'admin_role','admin_person_id':person_id,'admin_role_active':True,'admin_blocked':False,
            'is_temporary':False,'mailing_enabled':False,'tg_id':None,'max_id':None,'generated_admin_client':True,'created':datetime.now(timezone.utc).isoformat()}
        links[token].update(bindings)
        self._client(db,token,links[token],person_id);write_document(db,'links.json',links)
        return token

    def attach_console_account(self, username, phone, *, client_token=None):
        phone=valid_phone(phone)
        with self.product.connect() as db:
            admins=read_document(db,'admins.json');admin=admins.get(username)
            if not admin:raise ValueError('Аккаунт не найден')
            if client_token:
                client=read_document(db,'links.json').get(client_token)
                if not client or normalize_phone(client.get('phone'))!=phone:raise ValueError('Телефон выбранного клиента не совпадает')
                person_id=self._client(db,client_token,client)
                db.execute('UPDATE persons SET phone_verified=1 WHERE id=?',(person_id,))
            else:
                existing=db.execute('SELECT person_id FROM web_accounts WHERE username=?',(username,)).fetchone()
                person_id=existing['person_id'] if existing else self._person(db,admin.get('name',''),phone,verified=True)
                old=db.execute('SELECT phone FROM persons WHERE id=?',(person_id,)).fetchone()
                if old and old['phone']!=phone:
                    db.execute('DELETE FROM bot_bindings WHERE person_id=?',(person_id,))
                    db.execute('UPDATE identity_tickets SET used=1 WHERE person_id=? AND purpose="admin_binding"',(person_id,))
                    links=read_document(db,'links.json')
                    for client in db.execute('SELECT legacy_token FROM branch_clients WHERE person_id=?',(person_id,)):
                        if client['legacy_token'] in links:
                            card=links[client['legacy_token']]
                            if normalize_phone(card.get('phone')) != phone:card['id_ycl']=None
                            card.update(phone=phone,tg_id=None,max_id=None)
                    write_document(db,'links.json',links)
                db.execute('UPDATE persons SET phone=?,phone_verified=1 WHERE id=?',(phone,person_id))
            admin['person_id']=person_id;admin['phone']=phone
            admin['session_version']=int(admin.get('session_version',0))+1
            write_document(db,'admins.json',admins)
            db.execute('INSERT OR REPLACE INTO web_accounts(username,person_id) VALUES(?,?)',(username,person_id))
            self._membership(db,person_id,admin.get('branch_id'),bool(admin.get('is_super')))
        return person_id

    def _send(self, channel, target, text, context):
        if self.sender is None:
            return {'status':'failed','code':'transport_not_configured'}
        try:
            result=self.sender(channel,target,text,context)
            return result if isinstance(result,dict) else {'status':'sent' if result else 'failed'}
        except Exception:
            return {'status':'unknown','code':'transport_result_unknown'}

    def invite(self, username, branch_id, *, phone=None,email='',client_token=None,channel='sms'):
        if channel not in CHANNELS:raise ValueError('Некорректный канал')
        raw=secrets.token_urlsafe(32);identifier=uuid.uuid4().hex;now=self.clock()
        with self.product.connect() as db:
            self._scope(db,username,branch_id)
            if channel=='chatpush' and read_document(db,'branches.json')[str(branch_id)].get('yclients_enabled',True) is False:
                raise PermissionError('ChatPush недоступен самостоятельному филиалу')
            client=read_document(db,'links.json').get(client_token) if client_token else None
            if client_token and (not client or str(client.get('branch_id'))!=str(branch_id)):raise PermissionError('Клиент не найден в филиале')
            if client:phone=client.get('phone');email=client.get('email') or email
            phone=valid_phone(phone) if phone else None;email=valid_email(email)
            if phone:
                for account in read_document(db,'admins.json').values():
                    if normalize_phone(account.get('phone'))==phone and not account.get('is_super') and str(account.get('branch_id'))!=str(branch_id):
                        raise ValueError('Администратор уже закреплён за другим филиалом')
            if not phone and not email:raise ValueError('Укажите телефон или email')
            target=email if channel=='email' else (str((client or {}).get('tg_id' if channel=='telegram' else 'max_id') or '') if channel in {'telegram','max'} else phone)
            if not target:raise ValueError('Этот канал у получателя не подключён')
            db.execute('UPDATE identity_invitations SET state="revoked" WHERE branch_id=? AND (phone=? OR (?<>"" AND email=?)) AND state="pending"',(str(branch_id),phone,email,email))
            db.execute('INSERT INTO identity_invitations(id,token_hash,branch_id,phone,email,client_token,inviter,expires,state,created) VALUES(?,?,?,?,?,?,?,?,"pending",?)',(identifier,self._hash(raw),str(branch_id),phone,email,client_token,username,now+3600,now))
        link=config.BASE_URL+'/invite/'+raw
        text='Вас приглашают стать администратором филиала GateBot. Ссылка действует один час: '+link
        delivery=self._send(channel,target,text,{'event':'admin_invitation','idempotency_key':identifier,'branch_id':str(branch_id),'purpose':'invitation'})
        with self.product.connect() as db:db.execute('UPDATE identity_invitations SET delivery=? WHERE id=?',(json.dumps(delivery),identifier))
        return {'invitation_id':identifier,'link':link,'text':text,'expires_at':now+3600,'delivery':delivery}

    def _invitation(self,db,raw):
        row=db.execute('SELECT * FROM identity_invitations WHERE token_hash=?',(self._hash(raw),)).fetchone()
        if not row or row['state']!='pending' or row['expires']<=self.clock():raise ValueError('Приглашение недействительно или истекло')
        self._scope(db,row['inviter'],row['branch_id'])
        return row

    def invitation_info(self,raw):
        with self.product.connect() as db:
            row=self._invitation(db,raw);branch=read_document(db,'branches.json')[row['branch_id']]
            return {'branch_name':branch.get('name',''),'expires_at':row['expires'],'phone_fixed':bool(row['phone'])}

    def request_otp(self,raw,phone=None,ip='',*,registration=False):
        from registration_sms import sms_recipient
        now=self.clock();challenge=uuid.uuid4().hex;code=f'{secrets.randbelow(1000000):06d}'
        with self.product.connect() as db:
            row=self._registration(db,raw) if registration else self._invitation(db,raw)
            phone=valid_phone(row['phone'] or phone)
            recipient=sms_recipient(phone)
            subject=('registration:' if registration else 'invitation:')+row['id'];ip_hash=self._hash(ip)
            recent=db.execute('SELECT created FROM identity_otp WHERE phone=? ORDER BY created DESC LIMIT 1',(phone,)).fetchone()
            if recent and recent['created']>now-60:
                raise OtpRateLimit(math.ceil(recent['created'] + 60 - now), 'Дождитесь окончания таймера повторной отправки')
            count=db.execute('SELECT count(*) FROM identity_otp WHERE created>? AND (phone=? OR ip_hash=?)',(now-3600,phone,ip_hash)).fetchone()[0]
            if count>=10:raise ValueError('Лимит отправки кодов исчерпан. Повторите позже')
            db.execute('UPDATE identity_otp SET state="superseded" WHERE subject=? AND state IN ("sending","ready")',(subject,))
            db.execute('INSERT INTO identity_otp(id,subject,phone,code_hash,expires,state,created,ip_hash) VALUES(?,?,?,?,?,"sending",?,?)',(challenge,subject,phone,self._code_hash(challenge,code),now+300,now,ip_hash))
        # Keep the code in ONE UCS2 segment. Waiting for a second segment can
        # hide the whole message on the phone even after the network accepted it.
        delivery=self._send('sms',recipient,'GateBot: '+code+'. Код действует 5 минут. Никому не сообщайте.',{'event':'verification_code','purpose':'otp','idempotency_key':challenge,'branch_id':row['branch_id']})
        delivery={key:value for key,value in delivery.items() if key!='_development_redirect'}
        # Queued means the concrete transport durably owns the send; unknown/failed
        # never activate the challenge. A real code remains necessary in all cases.
        ready=delivery.get('status') in {'sent','queued','success','network_accepted','accepted'} and delivery.get('delivery') not in {'simulated','dry_run','stub'}
        stored_delivery=dict(delivery)
        if recipient != phone:
            stored_delivery['_development_redirect']=self._code_hash('registration-sms-redirect:'+config.DEV_REGISTRATION_SMS_PREFIX+':'+phone,recipient)
        with self.product.connect() as db:db.execute('UPDATE identity_otp SET state=?,delivery=? WHERE id=? AND state="sending"',('ready' if ready else 'failed',json.dumps(stored_delivery),challenge))
        return {'challenge_id':challenge,'expires_at':now+300,'delivery':delivery,'ready':ready,
                'retry_after':60,'resend_at':now+60,'server_time':self.clock(),
                'phone_hint':'••• '+phone[-4:]}

    def otp_status(self, raw, *, registration=False):
        """Status is bound to the invitation; no phone, code or hash is returned."""
        now=self.clock()
        with self.product.connect() as db:
            row=self._registration(db,raw) if registration else self._invitation(db,raw)
            subject=('registration:' if registration else 'invitation:')+row['id']
            otp=db.execute('SELECT * FROM identity_otp WHERE subject=? ORDER BY created DESC LIMIT 1',(subject,)).fetchone()
            if not otp:return {'challenge_id':None,'retry_after':0,'server_time':now,'ready':False}
            delivery=json.loads(otp['delivery'] or '{}')
            if self.delivery_status:
                delivery=self.delivery_status('sms',otp['id']) or delivery
            terminal=delivery.get('delivery') or delivery.get('status')
            ready=otp['state']=='ready' and otp['expires']>now and self._otp_delivery_allowed(otp) and terminal not in {'failed','error','cancelled','expired','simulated'}
            return {'challenge_id':otp['id'],'retry_after':max(0,math.ceil(otp['created']+60-now)),
                    'resend_at':otp['created']+60,'server_time':now,'expires_at':otp['expires'],
                    'ready':ready,'delivery':{k:delivery.get(k) for k in ('status','delivery','code','confirmed')},
                    'phone_hint':'••• '+otp['phone'][-4:]}

    def _otp_delivery_allowed(self, row):
        """A development redirect never becomes real phone proof in production."""
        marker=json.loads(row['delivery'] or '{}').get('_development_redirect')
        if not marker:return True
        from registration_sms import sms_recipient
        try:recipient=sms_recipient(row['phone'])
        except ValueError:return False
        return recipient != row['phone'] and hmac.compare_digest(marker,self._code_hash('registration-sms-redirect:'+config.DEV_REGISTRATION_SMS_PREFIX+':'+row['phone'],recipient))

    def _check_otp(self,db,subject,challenge,code):
        row=db.execute('SELECT * FROM identity_otp WHERE id=? AND subject=?',(challenge,subject)).fetchone()
        if not row or row['state']!='ready' or row['expires']<=self.clock() or row['attempts']>=5 or not self._otp_delivery_allowed(row):return None
        db.execute('UPDATE identity_otp SET attempts=attempts+1 WHERE id=?',(challenge,))
        if not isinstance(code,str) or not re.fullmatch(r'[0-9]{6}',code) or not hmac.compare_digest(row['code_hash'],self._code_hash(challenge,code)):return None
        return row

    def accept_invitation(self,raw,challenge,code,name,username,password,existing_password=None):
        name=str(name or '').strip();username=str(username or '').strip()
        if not name or len(name)>200:raise ValueError('Укажите имя')
        AdminAuth.validate_new_password(password)
        success=None
        with self.product.connect() as db:
            invitation=self._invitation(db,raw)
            otp=self._check_otp(db,'invitation:'+invitation['id'],challenge,code)
            if otp:
                admins=read_document(db,'admins.json');username=AdminAuth.resolve_username(admins,username);existing=admins.get(username)
                if existing:
                    if existing.get('disabled') or not AdminAuth.verify_password(existing_password,existing.get('password_hash')):raise ValueError('Войдите в существующий аккаунт с его паролем')
                    person_id=existing.get('person_id')
                    person=db.execute('SELECT * FROM persons WHERE id=?',(person_id,)).fetchone()
                    if not person or person['phone']!=otp['phone']:raise ValueError('Телефон существующего аккаунта не совпадает')
                    if existing.get('is_super'):raise ValueError('Суперадминистратору не требуется приглашение')
                    self._require_single_branch(db,person_id,invitation['branch_id'])
                else:
                    username=valid_email(username)
                    if not username:raise ValueError('Укажите email — он будет логином')
                    verified=db.execute('SELECT id FROM persons WHERE phone=? AND phone_verified=1',(otp['phone'],)).fetchall()
                    if len(verified)>1:raise ValueError('Номер связан с несколькими профилями. Обратитесь к суперадминистратору')
                    person_id=verified[0]['id'] if verified else None
                    if person_id and db.execute('SELECT 1 FROM web_accounts WHERE person_id=?',(person_id,)).fetchone():raise ValueError('Для этого телефона уже есть аккаунт. Используйте существующий логин')
                    if not person_id and invitation['client_token']:
                        client=read_document(db,'links.json').get(invitation['client_token'])
                        if not client or normalize_phone(client.get('phone'))!=otp['phone']:raise ValueError('Данные клиента изменились; запросите новое приглашение')
                        person_id=self._client(db,invitation['client_token'],client)
                    person_id=person_id or self._person(db,name,otp['phone'],verified=True)
                    if db.execute('SELECT 1 FROM web_accounts WHERE person_id=?',(person_id,)).fetchone():raise ValueError('Для телефона уже есть аккаунт. Используйте вход или восстановление пароля')
                    self._require_single_branch(db,person_id,invitation['branch_id'])
                    db.execute('UPDATE persons SET phone_verified=1,name=?,email=?,email_verified=0 WHERE id=?',(name,username,person_id))
                    admins[username]={'name':name,'phone':otp['phone'],'person_id':person_id,'password_hash':AdminAuth.hash_password(password),'branch_id':int(invitation['branch_id']),'is_super':False,'disabled':False,'session_version':1}
                    write_document(db,'admins.json',admins)
                    db.execute('INSERT INTO web_accounts(username,person_id) VALUES(?,?)',(username,person_id))
                if invitation['client_token']:
                    links=read_document(db,'links.json');client=links.get(invitation['client_token'])
                    if not client or normalize_phone(client.get('phone'))!=otp['phone']:raise ValueError('Данные клиента изменились; запросите новое приглашение')
                    old_person=self._client(db,invitation['client_token'],client)
                    if old_person!=person_id:
                        if db.execute('SELECT 1 FROM web_accounts WHERE person_id=?',(old_person,)).fetchone():raise ValueError('Клиент связан с другим аккаунтом')
                        db.execute('UPDATE branch_clients SET person_id=? WHERE legacy_token=?',(person_id,invitation['client_token']))
                        client['person_id']=person_id
                        write_document(db,'links.json',links)
                self._membership(db,person_id,invitation['branch_id'])
                db.execute('UPDATE identity_otp SET state="used" WHERE id=?',(challenge,))
                db.execute('UPDATE identity_invitations SET state="accepted" WHERE id=?',(invitation['id'],))
                success={'username':username,'person_id':person_id,'branch_id':invitation['branch_id']}
                if not existing and self.welcome_sink:self.welcome_sink(person_id,invitation['branch_id'],username)
                if self.event_sink:
                    self.event_sink('invitation_accepted', invitation['branch_id'], {
                        'event_id': invitation['id'], 'client_name': name})
        if success is None:raise ValueError('Код неверен, истёк или исчерпан лимит попыток')
        return success

    def list_invitations(self,username,branch_id):
        with self.product.connect() as db:
            self._scope(db,username,branch_id)
            return [dict(r) for r in db.execute('SELECT id,phone,email,expires,state,created,delivery FROM identity_invitations WHERE branch_id=? ORDER BY created DESC LIMIT 100',(str(branch_id),))]

    def list_admins(self,username,branch_id):
        with self.product.connect() as db:
            self._scope(db,username,branch_id)
            active=self._active_branch_admins(db,branch_id)
            return [dict(r,status='inactive' if r['status']=='active' and r['person_id'] not in active else r['status']) for r in db.execute('SELECT p.id person_id,p.name,p.phone,m.status FROM branch_memberships m JOIN persons p ON p.id=m.person_id WHERE m.branch_id=? ORDER BY p.name',(str(branch_id),))]

    def _active_branch_admins(self,db,branch_id):
        return {admin['person_id'] for admin in self._active_admin_accounts(db).values()
                if not admin.get('is_super') and str(branch_id) in admin['branch_ids']}

    def _active_admin_accounts(self,db):
        """One transaction-local snapshot, using the single-account role rules."""
        admins=read_document(db,'admins.json')
        people={row['username']:row['person_id'] for row in db.execute('SELECT username,person_id FROM web_accounts')}
        memberships={}
        for row in db.execute('SELECT person_id,branch_id FROM branch_memberships WHERE status="active"'):
            memberships.setdefault(row['person_id'],[]).append(row['branch_id'])
        result={}
        for username,admin in admins.items():
            if not admin or admin.get('disabled'):continue
            person_id=people.get(username,admin.get('person_id'))
            try:result[username]=self._resolved_admin(admin,username,person_id,memberships.get(person_id,[]) if person_id else [])
            except PermissionError:continue
        return result

    def _client_table_snapshot(self,db,username,tokens):
        """Authoritative clients and account roles, scoped before rendering rows."""
        accounts=self._active_admin_accounts(db)
        actor=accounts.get(username)
        if not actor:raise PermissionError('Требуется действующий администратор')
        links=read_document(db,'links.json')
        clients={}
        for token in tokens:
            if not isinstance(token,str) or token not in links:raise ValueError('Клиент не найден')
            clients[token]=links[token]
        branches=read_document(db,'branches.json')
        for branch_id in {str(client['branch_id']) for client in clients.values()}:
            self._checked_scope(actor,branch_id,branches)
        return clients,accounts

    def revoke_invitation(self,username,identifier):
        with self.product.connect() as db:
            row=db.execute('SELECT * FROM identity_invitations WHERE id=?',(identifier,)).fetchone()
            if not row:return False
            self._scope(db,username,row['branch_id']);db.execute('UPDATE identity_invitations SET state="revoked" WHERE id=? AND state="pending"',(identifier,))
        return True

    def revoke_admin(self,username,person_id,branch_id):
        with self.product.connect() as db:
            actor=self._scope(db,username,branch_id)
            if not isinstance(person_id,str) or not person_id or not db.execute('SELECT 1 FROM branch_memberships WHERE person_id=? AND branch_id=?',(person_id,str(branch_id))).fetchone():
                raise ValueError('Администратор не найден в филиале')
            active=self._active_branch_admins(db,branch_id)
            if person_id in active and len(active)<=1:raise ValueError('Сначала назначьте другого администратора филиала')
            db.execute('UPDATE branch_memberships SET status="revoked" WHERE person_id=? AND branch_id=?',(person_id,str(branch_id)))
            links=read_document(db,'links.json')
            for data in links.values():
                if data.get('admin_person_id')==person_id and str(data.get('branch_id'))==str(branch_id):data['admin_role_active']=False
            write_document(db,'links.json',links)
            admins=read_document(db,'admins.json')
            for admin in admins.values():
                if admin.get('person_id')==person_id and not admin.get('is_super') and str(admin.get('branch_id'))==str(branch_id):admin['session_version']=int(admin.get('session_version',0))+1
            write_document(db,'admins.json',admins)

    def _client_admin_state(self,db,token):
        client=read_document(db,'links.json').get(token)
        if not client:raise ValueError('Клиент не найден')
        return self._client_admin_states(db,{token:client})[token]

    def _client_admin_states(self,db,clients,*,accounts=None):
        accounts=self._active_admin_accounts(db) if accounts is None else accounts
        active_by_branch={}
        supers=set()
        for admin in accounts.values():
            if admin.get('is_super'):supers.add(admin['person_id'])
            else:
                for branch_id in admin['branch_ids']:
                    active_by_branch.setdefault(branch_id,set()).add(admin['person_id'])
        indexed={(row['legacy_token'],row['branch_id']):row['person_id']
                 for row in db.execute('SELECT legacy_token,branch_id,person_id FROM branch_clients')}
        return {token:self._client_role_state(indexed.get((token,str(client['branch_id'])),client.get('person_id')),
                    active_by_branch.get(str(client['branch_id']),set()),supers) for token,client in clients.items()}

    @staticmethod
    def _client_role_state(person_id,active,supers):
        is_super=bool(person_id and person_id in supers)
        is_admin=bool(is_super or (person_id and person_id in active))
        last=bool(not is_super and person_id in active and len(active)<=1)
        reason=('Суперадминистратора нельзя удалить или лишить прав через карточку клиента' if is_super else
                'Это последний администратор филиала. Сначала назначьте другого администратора' if last else '')
        return {'is_admin':is_admin,'is_super':is_super,'person_id':person_id,'last_admin':last,
                'can_revoke':bool(is_admin and not is_super and not last),'can_delete':not (is_super or last),'reason':reason}

    def _active_account(self,db,username):
        try:return self._admin(db,username)
        except PermissionError:return None

    def client_admin_state(self,username,token):
        with self.product.connect() as db:
            client=read_document(db,'links.json').get(token)
            if not client:raise ValueError('Клиент не найден')
            self._scope(db,username,client['branch_id'])
            return self._client_admin_state(db,token)

    def client_admin_states(self,username,tokens):
        with self.product.connect() as db:
            clients,accounts=self._client_table_snapshot(db,username,tokens)
            return self._client_admin_states(db,clients,accounts=accounts)

    def revoke_client_admin(self,username,token):
        with self.product.connect() as db:
            state=self.client_admin_state(username,token)
            if not state['is_admin']:raise ValueError('У клиента нет действующих прав администратора')
            if not state['can_revoke']:raise ValueError(state['reason'])
            client=read_document(db,'links.json')[token]
            self.revoke_admin(username,state['person_id'],client['branch_id'])
            return self._client_admin_state(db,token)

    def delete_client(self,username,token,*,confirm_admin=False):
        with self.product.connect() as db:
            state=self.client_admin_state(username,token)
            if not state['can_delete']:raise ValueError(state['reason'])
            if state['is_admin']:
                if confirm_admin is not True:raise ValueError('Подтвердите удаление клиента и отзыв его прав администратора')
                client=read_document(db,'links.json')[token]
                self.revoke_admin(username,state['person_id'],client['branch_id'])
            links=read_document(db,'links.json')
            del links[token]
            write_document(db,'links.json',links)
            return {'deleted':True,'admin_revoked':state['is_admin']}

    def issue_bind_ticket(self,username,platform):
        if platform not in {'telegram','max'}:raise ValueError('Неизвестный бот')
        raw='adm_'+secrets.token_urlsafe(24)
        with self.product.connect() as db:
            admin=self._admin(db,username)
            if not admin['person_id']:raise ValueError('Сначала привяжите телефон аккаунта через консоль')
            person=db.execute('SELECT phone_verified FROM persons WHERE id=?',(admin['person_id'],)).fetchone()
            if not person or not person['phone_verified']:raise ValueError('Подтвердите изменённый телефон аккаунта через администратора')
            proof=self._hash(str(admin.get('password_hash'))+'|'+str(admin.get('session_version',0)))
            db.execute('INSERT INTO identity_tickets(token_hash,person_id,platform,purpose,expires,payload) VALUES(?,?,?,"admin_binding",?,?)',(self._hash(raw),admin['person_id'],platform,self.clock()+600,json.dumps({'auth_proof':proof})))
        return raw

    def bind_ticket(self,platform,user_id,ticket):
        if platform not in {'telegram','max'} or not str(user_id).isdigit() or int(user_id)<=0:raise ValueError('Некорректный аккаунт бота')
        with self.product.connect() as db:
            row=db.execute('SELECT * FROM identity_tickets WHERE token_hash=?',(self._hash(ticket),)).fetchone()
            if not row or row['used'] or row['expires']<=self.clock() or row['platform']!=platform or row['purpose']!='admin_binding':raise ValueError('Ссылка привязки недействительна или истекла')
            account=db.execute('SELECT username FROM web_accounts WHERE person_id=?',(row['person_id'],)).fetchone()
            if not account:raise ValueError('Аккаунт не найден')
            admin=self._admin(db,account['username'])
            proof=self._hash(str(admin.get('password_hash'))+'|'+str(admin.get('session_version',0)))
            if not hmac.compare_digest(json.loads(row['payload']).get('auth_proof',''),proof):raise ValueError('Аккаунт изменился. Получите новую ссылку привязки')
            conflicts=db.execute('SELECT * FROM bot_bindings WHERE platform=? AND (user_id=? OR person_id=?)',(platform,str(user_id),row['person_id'])).fetchall()
            if any(r['person_id']!=row['person_id'] or r['user_id']!=str(user_id) for r in conflicts):raise ValueError('Бот уже привязан; обратитесь к суперадминистратору')
            db.execute('INSERT INTO bot_bindings(platform,user_id,person_id,assurance) VALUES(?,?,?,"admin_verified") ON CONFLICT(platform,user_id) DO UPDATE SET assurance="admin_verified",revision=revision+1',(platform,str(user_id),row['person_id']))
            db.execute('UPDATE identity_tickets SET used=1 WHERE token_hash=?',(self._hash(ticket),))
            field='tg_id' if platform=='telegram' else 'max_id';links=read_document(db,'links.json')
            for client in db.execute('SELECT legacy_token FROM branch_clients WHERE person_id=?',(row['person_id'],)):
                if client['legacy_token'] in links:links[client['legacy_token']][field]=str(user_id)
            write_document(db,'links.json',links)
        return {'status':'success','message':'Бот подключён к аккаунту администратора'}

    def bot_admin(self,platform,user_id):
        with self.product.connect() as db:
            row=db.execute('SELECT a.username FROM bot_bindings b JOIN web_accounts a ON a.person_id=b.person_id WHERE b.platform=? AND b.user_id=? AND b.assurance="admin_verified"',(platform,str(user_id))).fetchone()
            if not row:return None
            try:admin=self._admin(db,row['username'])
            except PermissionError:return None
            return {key:admin.get(key) for key in ('username','is_super','branch_ids','person_id')}

    def preferences_link_for_token(self,token):
        raw='pref_'+secrets.token_urlsafe(24)
        with self.product.connect() as db:
            client=read_document(db,'links.json').get(token)
            if not client:raise ValueError('Клиент не найден')
            if client.get('is_temporary'):raise PermissionError('Настройки уведомлений недоступны для гостевого пропуска')
            person_id=self._client(db,token,client)
            db.execute('INSERT INTO identity_tickets(token_hash,person_id,platform,purpose,expires,payload) VALUES(?,?,"","preferences",?,?)',
                (self._hash(raw),person_id,self.clock()+10*366*86400,json.dumps({'client_token':token,'branch_id':str(client['branch_id'])})))
        return config.BASE_URL+'/preferences/'+raw

    def _preference_identity(self,db,token):
        ticket=db.execute('SELECT * FROM identity_tickets WHERE token_hash=? AND purpose="preferences" AND used=0 AND expires>?',(self._hash(token),self.clock())).fetchone()
        links=read_document(db,'links.json')
        legacy=json.loads(ticket['payload']).get('client_token') if ticket else token
        client=links.get(legacy)
        if not client:raise ValueError('Ссылка настроек недействительна')
        if client.get('is_temporary'):raise PermissionError('Настройки уведомлений недоступны для гостевого пропуска')
        person_id=ticket['person_id'] if ticket else self._client(db,legacy,client)
        if ticket:
            payload=json.loads(ticket['payload'])
            indexed=db.execute('SELECT 1 FROM branch_clients WHERE legacy_token=? AND person_id=? AND branch_id=?',(legacy,person_id,str(client['branch_id']))).fetchone()
            if not indexed or str(payload.get('branch_id'))!=str(client['branch_id']):
                raise ValueError('Ссылка настроек недействительна')
        return person_id,str(client['branch_id']),legacy,client

    def _client_prefs(self,db,person_id,branch_id,client):
        row=db.execute('SELECT payload FROM notification_preferences WHERE person_id=? AND audience="client" AND branch_id=?',(person_id,branch_id)).fetchone()
        defaults={channel: bool(client.get('mailing_enabled') and client.get({'telegram':'tg_id','max':'max_id'}.get(channel,''))) for channel in CLIENT_CHANNELS}
        prefs=json.loads(row['payload']) if row else {'channels':defaults}
        selected=prefs.get('channels',{})
        # Only a previous explicit ChatPush opt-in carries over to Messengers.
        # Removing SMS must not opt somebody into a different delivery route.
        channels={channel:bool(selected.get(channel,False)) for channel in CLIENT_CHANNELS}
        if 'messengers' not in selected:channels['messengers']=bool(selected.get('chatpush',False))
        return {**prefs,'enabled':bool(client.get('mailing_enabled')),'channels':channels}

    def _client_allowed_channels(self,db,branch_id):
        branch=read_document(db,'branches.json').get(str(branch_id))
        return tuple(channel for channel in CLIENT_CHANNELS
                     if channel!='messengers' or (branch and branch.get('yclients_enabled',True) is not False))

    def prefs_for_token(self,token):
        with self.product.connect() as db:
            person_id,branch_id,legacy,client=self._preference_identity(db,token)
            return self._prefs_for_client(db,person_id,branch_id,client)

    def _prefs_for_client(self,db,person_id,branch_id,client):
        """Shared preference rules for a resolved client, without rereading links."""
        person=dict(db.execute('SELECT * FROM persons WHERE id=?',(person_id,)).fetchone())
        prefs=self._client_prefs(db,person_id,branch_id,client)
        email=client.get('email',person['email'])
        try:email=valid_email(email)
        except ValueError:email=''
        verified=bool(email and (prefs.get('verified_email')==email or (person['email_verified'] and person['email']==email)))
        allowed=self._client_allowed_channels(db,branch_id)
        connected={'email':verified,'telegram':bool(client.get('tg_id')),'max':bool(client.get('max_id'))}
        messenger=self._client_messengers_state(db,branch_id,client) if 'messengers' in allowed else None
        if messenger is not None:connected['messengers']=bool(messenger.get('connected'))
        result={**prefs,'channels':{channel:prefs['channels'][channel] for channel in allowed},
            'person_id':person_id,'branch_id':branch_id,'name':person['name'],'phone':person['phone'],
            'email':email,'email_verified':verified,'connected':connected,
            'effective_channels':{channel:bool(prefs['enabled'] and prefs['channels'][channel] and connected[channel]) for channel in allowed},
            'allowed_channels':list(allowed),
            'bound':{'telegram':bool(client.get('tg_id')),'max':bool(client.get('max_id'))}}
        if messenger is not None:result['messengers']={'connected':connected['messengers'],'reason':messenger.get('reason','')}
        return result

    def _person_admins(self,db,person_id):
        result=[]
        for row in db.execute('SELECT username FROM web_accounts WHERE person_id=?',(person_id,)):
            try:admin=self._admin(db,row['username'])
            except PermissionError:continue
            result.append(admin)
        return result

    def _client_messengers_state(self,db,branch_id,client):
        from messenger_service import MessengerService
        return MessengerService(self).client_state(db,branch_id,client)

    def assert_client_email_change(self,username,token,email):
        """A branch client editor cannot redirect a super's global notices."""
        with self.product.connect() as db:
            row=db.execute('SELECT person_id FROM branch_clients WHERE legacy_token=?',(token,)).fetchone()
            client=read_document(db,'links.json').get(token)
            if not row or not client or email==client.get('email',''):return
            actor=self._scope(db,username,client['branch_id'])
            if not actor.get('is_super') and any(admin.get('is_super') for admin in self._person_admins(db,row['person_id'])):
                raise PermissionError('Email суперадминистратора может изменить только суперадминистратор')

    def save_prefs_for_token(self,token,payload,*,actor_username=None):
        channels=payload.get('channels',{});enabled=payload.get('enabled')
        if type(enabled) is not bool or not isinstance(channels,dict) or any(k not in CLIENT_CHANNELS or type(v) is not bool for k,v in channels.items()):raise ValueError('Некорректные настройки каналов')
        with self.product.connect() as db:
            person_id,branch_id,legacy,client=self._preference_identity(db,token)
            allowed=self._client_allowed_channels(db,branch_id)
            if enabled and any(selected and channel not in allowed for channel,selected in channels.items()):
                raise ValueError('Канал «Мессенджеры» недоступен для филиала без YCLIENTS')
            person=db.execute('SELECT email FROM persons WHERE id=?',(person_id,)).fetchone()
            email=valid_email(payload['email']) if 'email' in payload else client.get('email',person['email'])
            if email!=client.get('email',person['email']) and self._person_admins(db,person_id):
                actor=self._admin(db,actor_username) if actor_username else None
                if not actor or not (actor.get('is_super') or actor['person_id']==person_id):
                    raise PermissionError('Измените email административного профиля в своём кабинете')
            old_prefs=self._client_prefs(db,person_id,branch_id,client)
            prefs={'enabled':enabled,'channels':{channel:channels.get(channel,False) if channel in allowed else old_prefs['channels'][channel] for channel in CLIENT_CHANNELS}}
            if old_prefs.get('verified_email')==email:prefs['verified_email']=email
            db.execute('INSERT INTO notification_preferences(person_id,audience,branch_id,payload) VALUES(?,"client",?,?) ON CONFLICT(person_id,audience,branch_id) DO UPDATE SET payload=excluded.payload',(person_id,branch_id,json.dumps(prefs)))
            links=read_document(db,'links.json');links[legacy]['mailing_enabled']=enabled
            if 'email' in payload:links[legacy].update(email=email,email_override=True)
            write_document(db,'links.json',links)
        return self.prefs_for_token(token)

    def verify_email_request(self,token):
        with self.product.connect() as db:
            person_id,branch_id,legacy,client=self._preference_identity(db,token)
        return self._email_request(person_id,branch_id,client_token=legacy)

    def _admin_contact(self,db,admin):
        """Resolve the account's own client profile, never a phone/name match."""
        person=db.execute('SELECT * FROM persons WHERE id=?',(admin['person_id'],)).fetchone()
        links=read_document(db,'links.json')
        candidates=[]
        for row in db.execute('SELECT legacy_token,branch_id FROM branch_clients WHERE person_id=? ORDER BY created,id',(admin['person_id'],)):
            client=links.get(row['legacy_token'])
            if not client or str(client.get('branch_id'))!=row['branch_id'] or client.get('person_id',admin['person_id'])!=admin['person_id']:
                continue
            if not admin.get('is_super') and row['branch_id']!=str(admin.get('branch_id')):
                continue
            candidates.append((row,client))
        # A super has one canonical client; retain deterministic handling of old
        # additional memberships without copying their contact across branches.
        candidates.sort(key=lambda item:item[0]['branch_id']!=str(admin.get('branch_id')))
        token=None;branch_id=admin.get('branch_id');client=None
        raw_email=person['email'] if person else ''
        if candidates:
            row,client=candidates[0];token=row['legacy_token'];branch_id=row['branch_id']
            if client.get('email') or client.get('email_override'):
                raw_email=client.get('email','')
        try:email=valid_email(raw_email)
        except ValueError:email=''
        verified=bool(email and person and person['email_verified'] and person['email']==email)
        if client and email:
            verified=verified or self._client_prefs(db,admin['person_id'],str(branch_id),client).get('verified_email')==email
        return {'email':email,'email_verified':verified,'token':token,'branch_id':branch_id}

    def admin_email(self,username,email,verify=False):
        email=valid_email(email)
        with self.product.connect() as db:
            admin=self._admin(db,username);person_id=admin['person_id']
            if not person_id:raise ValueError('Сначала укажите телефон аккаунта через консоль')
            contact=self._admin_contact(db,admin)
            if contact['token']:
                links=read_document(db,'links.json')
                links[contact['token']].update(email=email,email_override=True)
                write_document(db,'links.json',links)
            else:
                # A legacy console account can exist before the first branch.
                db.execute('UPDATE persons SET email=?,email_verified=0 WHERE id=? AND email<>?',(email,person_id,email))
        return self._email_request(person_id,contact['branch_id'],client_token=contact['token']) if verify else {'status':'saved'}

    def _email_request(self,person_id,branch_id,*,client_token=None):
        raw='mail_'+secrets.token_urlsafe(24)
        with self.product.connect() as db:
            person=db.execute('SELECT * FROM persons WHERE id=?',(person_id,)).fetchone()
            client=read_document(db,'links.json').get(client_token) if client_token else None
            if client_token:
                indexed=db.execute('SELECT 1 FROM branch_clients WHERE legacy_token=? AND person_id=? AND branch_id=?',(client_token,person_id,str(branch_id))).fetchone()
                if not client or not indexed or str(client.get('branch_id'))!=str(branch_id):
                    raise ValueError('Клиент не найден в филиале')
            email=client.get('email',person['email']) if client_token else person['email']
            if not email:raise ValueError('Сначала укажите email')
            recent=db.execute('SELECT expires FROM identity_tickets WHERE person_id=? AND purpose="email" ORDER BY expires DESC LIMIT 1',(person_id,)).fetchone()
            if recent and recent['expires']>self.clock()+86400-60:raise ValueError('Повторите отправку через минуту')
            db.execute('INSERT INTO identity_tickets(token_hash,person_id,platform,purpose,expires,payload) VALUES(?,?,"","email",?,?)',(self._hash(raw),person_id,self.clock()+86400,json.dumps({'email':email,**({'client_token':client_token,'branch_id':str(branch_id)} if client_token else {})})))
        return self._send('email',email,'Подтвердите email для GateBot: '+config.BASE_URL+'/verify-email/'+raw,{'purpose':'email_verification','event':'email_verification','idempotency_key':self._hash(raw),'branch_id':branch_id})

    def verify_email(self,raw):
        with self.product.connect() as db:
            row=db.execute('SELECT * FROM identity_tickets WHERE token_hash=? AND purpose="email" AND used=0 AND expires>?',(self._hash(raw),self.clock())).fetchone()
            if not row:raise ValueError('Ссылка подтверждения недействительна')
            payload=json.loads(row['payload']);email=payload['email']
            if payload.get('client_token'):
                client=read_document(db,'links.json').get(payload['client_token'])
                person=db.execute('SELECT email FROM persons WHERE id=?',(row['person_id'],)).fetchone()
                indexed=db.execute('SELECT 1 FROM branch_clients WHERE legacy_token=? AND person_id=? AND branch_id=?',(payload['client_token'],row['person_id'],payload['branch_id'])).fetchone()
                if client and person and indexed and str(client.get('branch_id'))==payload['branch_id'] and client.get('email',person['email'])==email:
                    prefs=self._client_prefs(db,row['person_id'],payload['branch_id'],client)
                    prefs['verified_email']=email
                    db.execute('INSERT INTO notification_preferences(person_id,audience,branch_id,payload) VALUES(?,"client",?,?) ON CONFLICT(person_id,audience,branch_id) DO UPDATE SET payload=excluded.payload',(row['person_id'],payload['branch_id'],json.dumps(prefs)))
                else:
                    raise ValueError('Email или клиент изменился. Запросите новую ссылку подтверждения')
            else:
                db.execute('UPDATE persons SET email_verified=1 WHERE id=? AND email=?',(row['person_id'],email))
            db.execute('UPDATE identity_tickets SET used=1 WHERE token_hash=?',(self._hash(raw),))

    def client_bot_link(self,token,platform):
        from urllib.parse import quote
        if platform not in {'telegram','max'}:raise ValueError('Некорректный бот')
        with self.product.connect() as db:
            person_id,branch_id,legacy,client=self._preference_identity(db,token)
        username=config.BOT_USERNAME if platform=='telegram' else config.MAX_BOT_USERNAME
        if not username:raise ValueError('Бот не настроен')
        # A client link never creates an admin_verified binding.
        return ('https://t.me/' if platform=='telegram' else 'https://max.ru/')+quote(username.lstrip('@'))+'?start='+quote(legacy)

    def admin_preferences(self,username):
        with self.product.connect() as db:
            admin=self._admin(db,username)
            if not admin['person_id']:return {'requires_phone':True,'events':{},'channels':{},'person_id':None}
            contact=self._admin_contact(db,admin)
            row=db.execute('SELECT payload FROM notification_preferences WHERE person_id=? AND audience="admin" AND branch_id=""',(admin['person_id'],)).fetchone()
            prefs=json.loads(row['payload']) if row else default_admin_preferences()
            if not admin.get('is_super'):
                prefs['events']={e:c for e,c in prefs.get('events',{}).items() if e not in {'sms_received','call_received'}}
            bindings={r['platform']:True for r in db.execute('SELECT platform FROM bot_bindings WHERE person_id=? AND assurance="admin_verified"',(admin['person_id'],))}
            return {**prefs,'person_id':admin['person_id'],'requires_phone':False,'email':contact['email'],'email_verified':contact['email_verified'],
                'channels':{'email':bool(contact['email']),**{p:bindings.get(p,False) for p in ('telegram','max')}},'is_super':bool(admin.get('is_super'))}

    def save_admin_preferences(self,username,payload):
        events=payload.get('events')
        if not isinstance(events,dict) or any(event not in ADMIN_EVENTS or not isinstance(channels,dict) or any(c not in {'email','telegram','max'} or type(v) is not bool for c,v in channels.items()) for event,channels in events.items()):raise ValueError('Некорректная матрица уведомлений')
        with self.product.connect() as db:
            admin=self._admin(db,username)
            if not admin['person_id']:raise ValueError('Сначала укажите телефон аккаунта через консоль')
            if not admin.get('is_super') and any(any(events.get(e,{}).values()) for e in ('sms_received','call_received')):raise PermissionError('События SIM800 доступны только суперадминистратору')
            normalized={'events':{e:{c:bool(events.get(e,{}).get(c,False)) for c in ('email','telegram','max')} for e in ADMIN_EVENTS}}
            db.execute('INSERT INTO notification_preferences(person_id,audience,branch_id,payload) VALUES(?,"admin","",?) ON CONFLICT(person_id,audience,branch_id) DO UPDATE SET payload=excluded.payload',(admin['person_id'],json.dumps(normalized)))
        return self.admin_preferences(username)

    def contacts_for_admin_events(self,branch_id,event):
        if event not in ADMIN_EVENTS:return []
        result=[]
        with self.product.connect() as db:
            for account in db.execute('SELECT username,person_id FROM web_accounts').fetchall():
                try:admin=self._admin(db,account['username'])
                except PermissionError:continue
                if not admin.get('is_super') and (event in {'sms_received','call_received'} or str(branch_id) not in admin['branch_ids']):continue
                pref=db.execute('SELECT payload FROM notification_preferences WHERE person_id=? AND audience="admin" AND branch_id=""',(account['person_id'],)).fetchone()
                channels=json.loads(pref['payload']).get('events',{}).get(event,{}) if pref else default_admin_preferences()['events'][event]
                contact=self._admin_contact(db,admin)
                if channels.get('email') and contact['email']:result.append({'person_id':account['person_id'],'username':account['username'],'channel':'email','target':contact['email']})
                for binding in db.execute('SELECT * FROM bot_bindings WHERE person_id=? AND assurance="admin_verified"',(account['person_id'],)):
                    if channels.get(binding['platform']):result.append({'person_id':account['person_id'],'username':account['username'],'channel':binding['platform'],'target':binding['user_id']})
        return result

    def register_branch(self,branch_id,name,person_name,phone,*,yclients_enabled=True,connection_mode='direct'):
        from branch_manager import allocate_internal_branch_id
        phone=valid_phone(phone)
        if type(yclients_enabled) is not bool:raise ValueError('Выберите тип филиала')
        if connection_mode not in {'direct','marketplace'} or (connection_mode=='marketplace' and not yclients_enabled):raise ValueError('Некорректный способ подключения YCLIENTS')
        if not isinstance(name,str) or not name.strip() or len(name.strip())>200 or not isinstance(person_name,str) or not person_name.strip() or len(person_name.strip())>200:raise ValueError('Заполните название филиала и имя (до 200 символов), телефон')
        if yclients_enabled:
            branch_id=str(branch_id)
            if not branch_id.isascii() or not branch_id.isdigit() or int(branch_id)<1:raise ValueError('Укажите ID филиала YCLIENTS')
            branch_id=str(int(branch_id))
        elif branch_id not in (None,''):
            raise ValueError('Внутренний ID самостоятельного филиала назначается автоматически')
        raw='reg_'+secrets.token_urlsafe(24);identifier=uuid.uuid4().hex
        with self.product.connect() as db:
            branches=read_document(db,'branches.json')
            if yclients_enabled and branch_id in branches:raise ValueError('Филиал уже зарегистрирован. Запросите приглашение у его администратора')
            recent=db.execute('SELECT count(*) FROM branch_registrations WHERE phone=? AND created>?',(phone,self.clock()-3600)).fetchone()[0]
            if recent>=5:raise ValueError('Слишком много попыток регистрации')
            if not yclients_enabled:branch_id=str(allocate_internal_branch_id(db,branches))
            elif db.execute('SELECT 1 FROM branch_registrations WHERE branch_id=? AND yclients_enabled=0',(branch_id,)).fetchone():raise ValueError('Идентификатор уже зарезервирован')
            db.execute('INSERT INTO branch_registrations(id,token_hash,branch_id,name,person_name,phone,expires,state,created,yclients_enabled,connection_mode) VALUES(?,?,?,?,?,?,?,"pending",?,?,?)',(identifier,self._hash(raw),branch_id,name.strip()[:200],person_name.strip()[:200],phone,self.clock()+3600,self.clock(),int(yclients_enabled),connection_mode))
        return {'registration_token':raw,'expires_at':self.clock()+3600,'yclients_enabled':yclients_enabled}

    def _registration(self,db,raw):
        row=db.execute('SELECT * FROM branch_registrations WHERE token_hash=?',(self._hash(raw),)).fetchone()
        if not row or row['state']!='pending' or row['expires']<=self.clock():raise ValueError('Регистрация истекла или завершена')
        return row

    def complete_registration(self,raw,challenge,code,username,password,*,billing=None,details=None):
        from billing_service import BillingService
        username=valid_email(username)
        if not username:raise ValueError('Укажите email — он будет логином')
        AdminAuth.validate_new_password(password);result=None
        billing=billing or BillingService(self.product)
        with self.product.connect() as db:
            row=self._registration(db,raw)
            if details is not None:
                mode=details.get('yclients_enabled',True)
                mode=mode=='true' if isinstance(mode,str) else mode
                provided_id=str(details.get('branch_id'))
                provided_id=str(int(provided_id)) if provided_id.isascii() and provided_id.isdigit() else provided_id
                if mode!=bool(row['yclients_enabled']) or str(details.get('branch_name','')).strip()!=row['name'] or str(details.get('name','')).strip()!=row['person_name'] or valid_phone(details.get('phone'))!=row['phone'] or (mode and provided_id!=row['branch_id']):
                    raise ValueError('Данные регистрации изменились. Запросите новый SMS-код')
            otp=self._check_otp(db,'registration:'+row['id'],challenge,code)
            if otp:
                branches=read_document(db,'branches.json');admins=read_document(db,'admins.json')
                if row['branch_id'] in branches or AdminAuth.resolve_username(admins,username) in admins:raise ValueError('Филиал или email уже зарегистрирован')
                if db.execute('SELECT 1 FROM persons p JOIN web_accounts a ON a.person_id=p.id WHERE p.phone=?',(otp['phone'],)).fetchone():raise ValueError('У телефона уже есть аккаунт; используйте приглашение или обратитесь к суперадминистратору')
                person_id=self._person(db,row['person_name'],otp['phone'],verified=True)
                db.execute('UPDATE persons SET email=? WHERE id=?',(username,person_id))
                crm_enabled=bool(row['yclients_enabled']);status='pending_verification' if crm_enabled else 'active'
                branches[row['branch_id']]={'branch_id':int(row['branch_id']),'name':row['name'],'timezone':'Europe/Moscow','enabled':not crm_enabled,'yclients_enabled':crm_enabled,'id_origin':'yclients' if crm_enabled else 'internal','onboarding_status':status,'hardware_routes_verified':False,'buttons':[]}
                if crm_enabled:branches[row['branch_id']]['yclients_connection_mode']=row['connection_mode']
                admins[username]={'name':row['person_name'],'phone':otp['phone'],'person_id':person_id,'password_hash':AdminAuth.hash_password(password),'branch_id':int(row['branch_id']),'is_super':False,'disabled':False,'session_version':1}
                write_document(db,'branches.json',branches);write_document(db,'admins.json',admins)
                db.execute('INSERT INTO web_accounts(username,person_id) VALUES(?,?)',(username,person_id));self._membership(db,person_id,row['branch_id'])
                if not crm_enabled:
                    billing.activate_trial(row['branch_id'],timezone='Europe/Moscow',at=self.clock(),db=db)
                db.execute('UPDATE identity_otp SET state="used" WHERE id=?',(challenge,));db.execute('UPDATE branch_registrations SET state="completed" WHERE id=?',(row['id'],))
                result={'username':username,'branch_id':row['branch_id'],'person_id':person_id,'status':status,'yclients_enabled':crm_enabled}
                if self.welcome_sink:self.welcome_sink(person_id,row['branch_id'],username)
        if result is None:raise ValueError('Код неверен, истёк или исчерпан лимит попыток')
        return result

    def activate_branch(self,username,branch_id,billing):
        with self.product.connect() as db:
            admin=self._scope(db,username,branch_id)
            if not admin.get('is_super'):raise PermissionError('Активировать маршруты может только суперадминистратор')
            branches=read_document(db,'branches.json');branch=branches[str(branch_id)]
            if branch.get('onboarding_status')!='pending_verification':raise ValueError('Филиал не ожидает проверки')
            branch.update(enabled=True,onboarding_status='active');write_document(db,'branches.json',branches)
            billing.activate_trial(str(branch_id),timezone=branch.get('timezone','Europe/Moscow'),db=db)
        return {'status':'active'}

    def _fernet(self):
        from cryptography.fernet import Fernet
        path=Path(self.product.path).parent/'integration.key'
        if not path.exists():
            try:
                descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(descriptor,'wb') as stream:stream.write(Fernet.generate_key());stream.flush();os.fsync(stream.fileno())
            except FileExistsError:pass
        return Fernet(path.read_bytes())

    def save_integration(self,username,branch_id,kind,credentials,*,verified_direct=False,marketplace=False,account=None):
        credentials=dict(credentials) if isinstance(credentials,dict) else credentials
        allowed={'yclients':{'user_token','partner_token'},'chatpush':{'token'}}
        if kind not in allowed or not isinstance(credentials,dict) or set(credentials)-allowed[kind] or any(not isinstance(v,str) or not v.strip() or len(v)>4096 for v in credentials.values()):raise ValueError('Некорректные настройки интеграции')
        required={'yclients':{'user_token'},'chatpush':{'token'}}[kind]
        if not required.issubset(credentials):raise ValueError('Заполните обязательные поля подключения')
        if kind=='chatpush':
            from chatpush import validate_token
            credentials['token']=validate_token(credentials['token'])
        with self.product.connect() as db:
            self._integration_scope(db,username,branch_id)
            branch=read_document(db,'branches.json')[str(branch_id)]
            mode=branch.get('yclients_connection_mode') or branch.get('yclients_connection_source') or 'direct'
            if kind=='yclients' and mode=='marketplace' and not marketplace:
                raise PermissionError('Филиал подключается через маркетплейс YCLIENTS. Вход по логину и паролю для него недоступен')
            encrypted=self._fernet().encrypt(json.dumps(credentials).encode()).decode()
            db.execute('INSERT INTO identity_integrations(branch_id,kind,ciphertext,updated) VALUES(?,?,?,?) ON CONFLICT(branch_id,kind) DO UPDATE SET ciphertext=excluded.ciphertext,updated=excluded.updated',(str(branch_id),kind,encrypted,self.clock()))
            if kind=='yclients' and verified_direct:
                branches=read_document(db,'branches.json')
                branches[str(branch_id)]['yclients_connection_source']='direct'
                branches[str(branch_id)]['yclients_connection_mode']='direct'
                branches[str(branch_id)].pop('yclients_marketplace_state',None)
                write_document(db,'branches.json',branches)
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='marketplace_branches'").fetchone():
                    db.execute('INSERT INTO marketplace_branches VALUES(?,"direct",1,?) ON CONFLICT(branch_id) DO UPDATE SET state="direct",generation=generation+1,updated=excluded.updated',(str(branch_id),self.clock()))
            self._integration_checks_table(db)
            db.execute('DELETE FROM integration_checks WHERE branch_id=? AND kind=?',(str(branch_id),kind))
            if kind=='yclients':
                from yclients_connection import account_profile
                profile=account_profile(account)
                db.execute('CREATE TABLE IF NOT EXISTS integration_profiles(branch_id TEXT NOT NULL,kind TEXT NOT NULL,ciphertext TEXT NOT NULL,PRIMARY KEY(branch_id,kind))')
                db.execute('DELETE FROM integration_profiles WHERE branch_id=? AND kind=?',(str(branch_id),kind))
                if profile:
                    encrypted_profile=self._fernet().encrypt(json.dumps(profile).encode()).decode()
                    db.execute('INSERT INTO integration_profiles VALUES(?,?,?)',(str(branch_id),kind,encrypted_profile))
        return {'configured':True}

    def integration_credentials(self,branch_id,kind):
        with self.product.connect() as db:
            branch=read_document(db,'branches.json').get(str(branch_id))
            if not branch or branch.get('yclients_enabled',True) is False:return {}
            if kind=='yclients' and branch.get('yclients_marketplace_state') in {'frozen','uninstalled'}:return {}
            row=db.execute('SELECT ciphertext FROM identity_integrations WHERE branch_id=? AND kind=?',(str(branch_id),kind)).fetchone()
        credentials=json.loads(self._fernet().decrypt(row['ciphertext'].encode())) if row else {}
        # Ignore obsolete instance setup fields in already encrypted credentials.
        if kind=='chatpush':return {'token':credentials['token']} if credentials.get('token') else {}
        return credentials

    def integration_status(self,username,branch_id):
        with self.product.connect() as db:
            self._integration_scope(db,username,branch_id)
            self._integration_checks_table(db)
            result={r['kind']:{'configured':True,'updated':r['updated']} for r in db.execute('SELECT kind,updated FROM identity_integrations WHERE branch_id=?',(str(branch_id),))}
            for row in db.execute('SELECT kind,result,checked_at FROM integration_checks WHERE branch_id=?',(str(branch_id),)):
                if row['kind']=='chatpush':continue
                result.setdefault(row['kind'],{'configured':False}).update(check=json.loads(row['result']),checked_at=row['checked_at'])
            branch=read_document(db,'branches.json')[str(branch_id)]
            source=branch.get('yclients_connection_source')
            state=branch.get('yclients_marketplace_state')
            connection=result.setdefault('yclients',{'configured':False})
            mode=branch.get('yclients_connection_mode') or source or 'direct'
            connection['mode']=mode
            connection['manual_allowed']=mode!='marketplace'
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='integration_profiles'").fetchone():
                profile=db.execute('SELECT ciphertext FROM integration_profiles WHERE branch_id=? AND kind="yclients"',(str(branch_id),)).fetchone()
                if profile:connection['account']=json.loads(self._fernet().decrypt(profile['ciphertext'].encode()))
            if source in {'direct','marketplace'} or state in {'active','frozen','uninstalled'}:
                connection=result.setdefault('yclients',{'configured':False})
                connection['source']=source if source in {'direct','marketplace'} else 'marketplace'
                connection['blocked']=state in {'frozen','uninstalled'}
                if state in {'active','frozen','uninstalled'}:connection['marketplace_state']=state
            return result

    def _integration_scope(self,db,username,branch_id):
        admin=self._scope(db,username,branch_id)
        branch=read_document(db,'branches.json').get(str(branch_id),{})
        if branch.get('yclients_enabled',True) is False:raise PermissionError('Подключения YCLIENTS и ChatPush доступны только филиалам с YCLIENTS')
        return admin

    @staticmethod
    def _integration_checks_table(db):
        db.execute('CREATE TABLE IF NOT EXISTS integration_checks(branch_id TEXT NOT NULL,kind TEXT NOT NULL,result TEXT NOT NULL,checked_at REAL NOT NULL,PRIMARY KEY(branch_id,kind))')

    def record_integration_check(self,username,branch_id,kind,result,expected_credentials=None):
        if kind!='yclients' or not isinstance(result,dict):raise ValueError('Некорректная проверка подключения')
        # Only our fixed, non-sensitive diagnostic vocabulary is persisted.
        codes={'ready','verified','permission_missing','staff_restricted','permissions_missing','contacts_unconfirmed','credentials_missing','authorization_failed','access_denied','branch_unavailable','rate_limited','provider_unavailable','invalid_response','transport_error','provider_rejected','invalid_account','invalid_provider_response','duplicate_requires_reconciliation'}
        code=result.get('code') if result.get('code') in codes else ('ready' if result.get('connected') is True else 'provider_unavailable')
        safe={'connected':result.get('connected') is True,'code':code,'checks':[]}
        for item in result.get('checks',[]):
            if isinstance(item,dict) and item.get('key') in {'branch','records','contacts','client_directory','all_staff','records_api','account'} and item.get('state') in {'success','warning','error'}:
                safe['checks'].append({'key':item['key'],'state':item['state'],'code':item.get('code') if item.get('code') in codes else 'provider_unavailable'})
        with self.product.connect() as db:
            self._integration_scope(db,username,branch_id)
            if expected_credentials is not None:
                row=db.execute('SELECT ciphertext FROM identity_integrations WHERE branch_id=? AND kind=?',(str(branch_id),kind)).fetchone()
                current=json.loads(self._fernet().decrypt(row['ciphertext'].encode())) if row else {}
                if current!=expected_credentials:return False
            self._integration_checks_table(db)
            db.execute('INSERT INTO integration_checks VALUES(?,?,?,?) ON CONFLICT(branch_id,kind) DO UPDATE SET result=excluded.result,checked_at=excluded.checked_at',(str(branch_id),kind,json.dumps(safe),self.clock()))
        return True
