"""Private branch conversations, pass requests and expiring guest handoffs."""
import hashlib
import re
import secrets
from datetime import datetime, timezone

from link_manager import access_is_active, normalize_phone
from storage.sql_documents import read_document
from timeutil import pass_expiry
from identity_service import valid_phone
from storage import config


class BranchDesk:
    def __init__(self, identity, links, branches, notices):
        self.identity, self.links, self.branches, self.notices = identity, links, branches, notices
        self.store = identity.product
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS desk_threads(id INTEGER PRIMARY KEY AUTOINCREMENT,branch_id TEXT NOT NULL,token TEXT NOT NULL,phone TEXT NOT NULL,updated REAL NOT NULL,UNIQUE(branch_id,token,phone))')
            db.execute('CREATE TABLE IF NOT EXISTS desk_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,thread_id INTEGER NOT NULL REFERENCES desk_threads(id) ON DELETE CASCADE,sender TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,request_key TEXT NOT NULL,approved REAL,UNIQUE(thread_id,sender,request_key))')
            db.execute('CREATE INDEX IF NOT EXISTS desk_by_branch ON desk_threads(branch_id,updated)')
            db.execute('CREATE TABLE IF NOT EXISTS guest_issues(owner TEXT NOT NULL, nonce TEXT NOT NULL, guest TEXT NOT NULL, created REAL NOT NULL, delivery TEXT, PRIMARY KEY(owner,nonce))')
            db.execute('CREATE TABLE IF NOT EXISTS guest_phone_sequence(id INTEGER PRIMARY KEY CHECK(id=1), last INTEGER NOT NULL)')
            db.execute("CREATE INDEX IF NOT EXISTS desk_pending ON desk_messages(thread_id,id) WHERE kind='request' AND approved IS NULL")
            db.execute('CREATE TABLE IF NOT EXISTS desk_shares(id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,token TEXT NOT NULL,phone TEXT NOT NULL,revision INTEGER NOT NULL,gate_id TEXT NOT NULL,button_id TEXT NOT NULL,expires REAL NOT NULL,request_id TEXT UNIQUE)')

    def _client(self, db, token):
        data = read_document(db, 'links.json').get(token)
        if not data or str(data['branch_id']) not in read_document(db,'branches.json'):
            raise PermissionError('Пропуск не найден')
        if self.links.guest_pass_removed(token, data):
            raise PermissionError('Пропуск удален')
        return data

    def _thread(self, db, identifier, username):
        row = db.execute('SELECT * FROM desk_threads WHERE id=?',(identifier,)).fetchone()
        if not row:
            raise ValueError('Диалог не найден')
        self.identity._scope(db, username, row['branch_id'])
        data = self._client(db, row['token'])
        if str(data['branch_id']) != row['branch_id'] or (normalize_phone(data.get('phone')) or '') != row['phone']:
            raise PermissionError('Контакт клиента изменился')
        return row, data

    def _client_thread(self, db, token, *, create=False):
        data = self._client(db, token)
        key = (str(data['branch_id']), token, normalize_phone(data.get('phone')) or '')
        if create:
            db.execute('INSERT OR IGNORE INTO desk_threads(branch_id,token,phone,updated) VALUES(?,?,?,?)',(*key,self.identity.clock()))
        return db.execute('SELECT * FROM desk_threads WHERE branch_id=? AND token=? AND phone=?',key).fetchone(), data

    def _messages(self, db, row):
        if row is None:
            return []
        messages = db.execute('SELECT id,sender,kind,body,created,approved FROM desk_messages WHERE thread_id=? ORDER BY id DESC LIMIT 100',(row['id'],)).fetchall()
        return [{**dict(message),'body':self.notices._decrypt(message['body'])['text']} for message in reversed(messages)]

    def client_view(self, token):
        with self.store.connect() as db:
            row, data = self._client_thread(db, token)
            branch = self.branches.get_branch(data['branch_id'])
            return {'messages':self._messages(db,row),'active':access_is_active(data),'branch':branch.name,'branch_id':row['branch_id'] if row else str(data['branch_id']),
                    'share_delivery':bool(self._guest_channel(db, branch)),
                    'contacts':[{'phone':b.phone,'label':b.label} for b in branch.buttons if b.type=='admin' and normalize_phone(b.phone)],
                    'gates':[{'id':b.id,'label':b.label} for b in branch.buttons if b.type=='gate'] if branch.enabled and access_is_active(data) else []}

    def threads(self, username, branch=None):
        with self.store.connect() as db:
            actor = self.identity._admin(db, username)
            branch = str(branch or actor.get('branch_id') or '')
            if branch:
                self.identity._scope(db, username, branch)
            elif not actor.get('is_super'):
                raise PermissionError('Нет доступа к филиалу')
            links = read_document(db,'links.json')
            result = []
            query = """SELECT t.*, (SELECT max(m.id) FROM desk_messages m
                       WHERE m.thread_id=t.id AND m.kind='request' AND m.approved IS NULL) AS pending
                       FROM desk_threads t """
            if branch:
                query += 'WHERE t.branch_id=? '
            for row in db.execute(query+'ORDER BY updated DESC,id DESC',(branch,) if branch else ()):
                data = links.get(row['token'])
                if not data or str(data['branch_id'])!=row['branch_id'] or (normalize_phone(data.get('phone')) or '')!=row['phone']:
                    continue
                result.append({'id':row['id'],'name':data.get('user_name') or 'Клиент','branch_id':row['branch_id'],
                               'updated':row['updated'],'pending':row['pending']})
            return result

    def admin_view(self, username, identifier):
        with self.store.connect() as db:
            row, data = self._thread(db, identifier, username)
            return {'id':row['id'],'name':data.get('user_name') or 'Клиент','messages':self._messages(db,row)}

    def _insert(self, db, row, sender, kind, body, key):
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key):
            raise ValueError('Обновите страницу и повторите отправку')
        if not isinstance(body,str) or not 1<=len(body.strip())<=2000:
            raise ValueError('Введите сообщение от 1 до 2000 символов')
        existing = db.execute('SELECT id FROM desk_messages WHERE thread_id=? AND sender=? AND request_key=?',(row['id'],sender,key)).fetchone()
        if existing:
            return None
        stamp = self.identity.clock()
        if sender=='client' and db.execute('SELECT COUNT(*) FROM desk_messages WHERE thread_id=? AND sender=? AND created>?',(row['id'],sender,stamp-60)).fetchone()[0]>=8:
            raise ValueError('Слишком много сообщений. Повторите через минуту')
        message = db.execute('INSERT INTO desk_messages(thread_id,sender,kind,body,created,request_key) VALUES(?,?,?,?,?,?)',
                             (row['id'],sender,kind,self.notices._encrypt({'text':body.strip()}),stamp,key))
        db.execute('UPDATE desk_threads SET updated=? WHERE id=?',(stamp,row['id']))
        return message.lastrowid

    def client_send(self, token, kind, body, key):
        with self.links.store.lock, self.store.connect() as db:
            row, data = self._client_thread(db,token,create=True)
            event = 'client_message'
            if kind=='request':
                if db.execute('SELECT 1 FROM desk_messages WHERE thread_id=? AND sender="client" AND request_key=?',(row['id'],key)).fetchone():
                    return
                if access_is_active(data):
                    raise ValueError('Ваш пропуск уже действует')
                if db.execute('SELECT 1 FROM desk_messages WHERE thread_id=? AND kind="request" AND approved IS NULL',(row['id'],)).fetchone():
                    return
                body = 'Прошу предоставить пропуск'
                event = 'pass_requested'
            elif kind!='message':
                raise ValueError('Неизвестное действие')
            saved = self._insert(db,row,'client',kind,body,key)
            if saved:
                branch = self.branches.get_branch(row['branch_id'])
                if kind=='request' and branch.enabled and branch.auto_approve_pass_requests:
                    self.links.set_expiry_at(token,pass_expiry(branch))
                    db.execute('UPDATE desk_messages SET approved=? WHERE id=?',(self.identity.clock(),saved))
                    event = 'pass_request_auto_granted'
                self.notices.event(event,row['branch_id'],
                                   {'event_id':'desk:'+str(saved),'client_name':data.get('user_name'),'message':body.strip(),'thread_id':row['id'],'desk_message_id':saved})

    def reply(self, username, identifier, body, key, *, grant_revision=None):
        with self.store.connect() as db:
            row, data = self._thread(db,identifier,username)
            saved = self._insert(db,row,'admin','message',body,key)
            if saved:
                targets = [(channel,data[field]) for channel,field in [('telegram','tg_id'),('max','max_id')] if data.get(field)]
                if data.get('person_id') and not data.get('is_temporary'):
                    for device in self.notices.pwa.targets(db,row['token'],row['branch_id'],data['person_id']):
                        targets.append(('push',device))
                for channel,target in targets:
                    self.notices._enqueue(f'desk-reply:{saved}:{channel}:{target}','desk_reply','desk_reply',row['branch_id'],channel,
                        target,{'text':'Администратор: '+body.strip(),'token':row['token'],'phone':row['phone'],'revision':grant_revision,'thread_id':row['id']},db)

    def approve(self, username, identifier, message_id):
        # Same lock order as gate commands and ordinary pass mutations.
        with self.links.store.lock, self.store.connect() as db:
            row, data = self._thread(db,identifier,username)
            message = db.execute('SELECT approved FROM desk_messages WHERE thread_id=? AND id=? AND kind="request"',(row['id'],message_id)).fetchone()
            if message is None:
                raise ValueError('Запрос не найден')
            if message['approved'] is None:
                if not access_is_active(data):
                    branch = self.branches.get_branch(row['branch_id'])
                    self.links.set_expiry_at(row['token'],pass_expiry(branch))
                db.execute('UPDATE desk_messages SET approved=? WHERE id=?',(self.identity.clock(),message_id))
                granted=self._client(db,row['token'])
                self.reply(username,identifier,'Пропуск разрешён. Откройте страницу ворот.',f'approved-{int(message_id):08d}',
                           grant_revision=granted.get('access_revision',0))

    def _guest_channel(self, db, branch):
        from notification_policy import client_delivery_enabled
        if not client_delivery_enabled(db, branch.branch_id):
            return None
        if branch.yclients_enabled and self.identity.integration_credentials(branch.branch_id, 'chatpush').get('token'):
            return 'chatpush'
        return 'sms' if branch.sms_notifications_enabled else None

    def _guest_phone(self, db):
        used = {normalize_phone(card.get('phone')) for card in read_document(db,'links.json').values()}
        db.execute('INSERT OR IGNORE INTO guest_phone_sequence VALUES(1,0)')
        index = db.execute('SELECT last FROM guest_phone_sequence WHERE id=1').fetchone()[0]
        while index < 99_999_999:
            index += 1
            phone = '+790' + str(index).zfill(8)
            if phone not in used:
                db.execute('UPDATE guest_phone_sequence SET last=? WHERE id=1',(index,))
                return phone
        raise ValueError('Не удалось назначить номер гостю')

    def share(self, token, *, phone=None, no_phone=False, send=False, key):
        from message_templates import pass_link
        if type(no_phone) is not bool or type(send) is not bool or (no_phone and (send or phone)):
            raise ValueError('Проверьте номер гостя и способ отправки')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key):
            raise ValueError('Обновите форму гостевого пропуска')
        phone = None if no_phone else valid_phone(phone)
        with self.links.store.lock, self.store.connect() as db:
            data = self._client(db,token)
            branch = self.branches.get_branch(data['branch_id'])
            if not branch.enabled or not access_is_active(data):
                raise PermissionError('Для передачи ссылки нужен действующий пропуск')
            stamp = self.identity.clock()
            previous = db.execute('SELECT guest,delivery FROM guest_issues WHERE owner=? AND nonce=?',(token,key)).fetchone()
            if previous:
                self._client(db,previous['guest'])
                return {'path':'/opn/'+previous['guest'], 'delivery':previous['delivery']}
            db.execute('DELETE FROM guest_issues WHERE created<?',(stamp-8*86400,))
            if db.execute('SELECT count(*) FROM guest_issues WHERE owner=? AND created>?',(token,stamp-3600)).fetchone()[0]>=12:
                raise ValueError('Достигнут лимит гостевых ссылок за час')
            channel = self._guest_channel(db,branch) if send else None
            if send and not channel:
                raise ValueError('Отправка ссылки в этом филиале недоступна')
            if no_phone:
                phone = self._guest_phone(db)
            guest = self.links.create_guest(branch, phone, name='Гость от '+(data.get('user_name') or 'клиента'),
                                            system_phone=no_phone, at=datetime.fromtimestamp(stamp,timezone.utc))
            delivery = 'unknown' if send else 'not_requested'
            db.execute('INSERT INTO guest_issues VALUES(?,?,?,?,?)',(token,key,guest,stamp,delivery))
        path = '/opn/' + guest
        if send:
            try:
                result = self.notices.send(channel,phone,pass_link(config.BASE_URL.rstrip('/')+path),
                        {'branch_id':str(branch.branch_id),'client_token':guest,'event':'manual_pass',
                         'preference_channel':'sms' if channel=='sms' else 'share','idempotency_key':'issue:'+guest})
                delivery = result.get('status','unknown')
            except Exception:
                delivery = 'unknown'
            with self.store.connect() as db:
                db.execute('UPDATE guest_issues SET delivery=? WHERE owner=? AND nonce=?',(delivery,token,key))
        return {'path':path,'delivery':delivery}

    @staticmethod
    def _digest(value):
        return hashlib.sha256(str(value).encode()).hexdigest()

    @staticmethod
    def _matches(row, data, gate):
        return bool(row and data and gate and gate.stable_id==row['gate_id'] and gate.id==row['button_id']
                    and str(data['branch_id'])==row['branch_id'] and (normalize_phone(data.get('phone')) or '')==row['phone']
                    and data.get('access_revision',0)==row['revision'] and access_is_active(data))

    def authorizes_request(self, db, request_id, token, data, gate):
        row=db.execute('SELECT * FROM desk_shares WHERE request_id=? AND expires>?',(request_id,self.identity.clock())).fetchone()
        return bool(row and row['token']==token and self._matches(row,data,gate))

    def shared(self, raw, access=None):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM desk_shares WHERE id=? AND expires>? AND request_id IS NULL',(self._digest(raw),self.identity.clock())).fetchone()
            if not row:
                raise PermissionError('Гостевая ссылка истекла или уже использована')
            data=self._client(db,row['token'])
            branch=self.branches.get_branch(row['branch_id'])
            gate=next((b for b in branch.buttons if b.type=='gate' and b.stable_id==row['gate_id']),None) if branch and branch.enabled else None
            if not self._matches(row,data,gate):
                raise PermissionError('Доступ невозможен')
            if access is None:
                view={'branch':branch.name,'branch_id':row['branch_id'],'gate':gate.label}
                if branch.logo:
                    view['branch_logo']=branch.logo
                return view
            request_id='shared_'+secrets.token_hex(16)
            db.execute('UPDATE desk_shares SET request_id=? WHERE id=?',(request_id,row['id']))
        result=access.open_for_token(row['token'],gate.id,request_id=request_id,expected_gate_id=row['gate_id'],expected_phone=row['phone'] or None)
        # Only definitively denied requests may be attempted again.
        if result.get('status')=='error' and result.get('http_status') in (403,429):
            with self.store.connect() as db:
                db.execute('UPDATE desk_shares SET request_id=NULL WHERE id=? AND request_id=?',(row['id'],request_id))
        return result
