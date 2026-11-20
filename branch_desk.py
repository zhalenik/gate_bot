"""Private branch conversations, pass requests and expiring guest handoffs."""
import hashlib
import re
import secrets

from link_manager import access_is_active, normalize_phone
from storage.sql_documents import read_document
from timeutil import end_of_day, parse_datetime


class BranchDesk:
    def __init__(self, identity, links, branches, notices):
        self.identity, self.links, self.branches, self.notices = identity, links, branches, notices
        self.store = identity.product
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS desk_threads(id INTEGER PRIMARY KEY AUTOINCREMENT,branch_id TEXT NOT NULL,token TEXT NOT NULL,phone TEXT NOT NULL,updated REAL NOT NULL,UNIQUE(branch_id,token,phone))')
            db.execute('CREATE TABLE IF NOT EXISTS desk_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,thread_id INTEGER NOT NULL REFERENCES desk_threads(id) ON DELETE CASCADE,sender TEXT NOT NULL,kind TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,request_key TEXT NOT NULL,approved REAL,UNIQUE(thread_id,sender,request_key))')
            db.execute('CREATE INDEX IF NOT EXISTS desk_by_branch ON desk_threads(branch_id,updated)')
            db.execute("CREATE INDEX IF NOT EXISTS desk_pending ON desk_messages(thread_id,id) WHERE kind='request' AND approved IS NULL")
            db.execute('CREATE TABLE IF NOT EXISTS desk_shares(id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,token TEXT NOT NULL,phone TEXT NOT NULL,revision INTEGER NOT NULL,gate_id TEXT NOT NULL,button_id TEXT NOT NULL,expires REAL NOT NULL,request_id TEXT UNIQUE)')

    def _client(self, db, token):
        data = read_document(db, 'links.json').get(token)
        if not data or str(data['branch_id']) not in read_document(db,'branches.json'):
            raise PermissionError('Пропуск не найден')
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
        with self.store.connect() as db:
            row, data = self._client_thread(db,token,create=True)
            if kind=='request':
                if access_is_active(data):
                    raise ValueError('Ваш пропуск уже действует')
                if db.execute('SELECT 1 FROM desk_messages WHERE thread_id=? AND kind="request" AND approved IS NULL',(row['id'],)).fetchone():
                    return
                body = 'Прошу предоставить пропуск'
            elif kind!='message':
                raise ValueError('Неизвестное действие')
            saved = self._insert(db,row,'client',kind,body,key)
            if saved:
                self.notices.event('pass_requested' if kind=='request' else 'client_message',row['branch_id'],
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
                    self.links.set_expiry_at(row['token'],end_of_day(tz=branch.timezone))
                db.execute('UPDATE desk_messages SET approved=? WHERE id=?',(self.identity.clock(),message_id))
                granted=self._client(db,row['token'])
                self.reply(username,identifier,'Пропуск разрешён. Откройте страницу ворот.',f'approved-{int(message_id):08d}',
                           grant_revision=granted.get('access_revision',0))

    def share(self, token, gate_id):
        with self.store.connect() as db:
            data = self._client(db,token)
            branch = self.branches.get_branch(data['branch_id'])
            gate = next((b for b in branch.buttons if b.type=='gate' and b.id==gate_id),None)
            if not gate or not branch.enabled or not access_is_active(data):
                raise PermissionError('Для передачи ссылки нужен действующий пропуск')
            stamp = self.identity.clock()
            expires = stamp + 3600
            if data.get('access_expires') and not data.get('admin_role_active'):
                expires = min(expires, parse_datetime(data['access_expires']).timestamp())
            db.execute('DELETE FROM desk_shares WHERE expires<=?',(stamp,))
            if db.execute('SELECT count(*) FROM desk_shares WHERE token=?',(token,)).fetchone()[0]>=12:
                raise ValueError('Достигнут лимит гостевых ссылок за час')
            raw = secrets.token_urlsafe(24)
            db.execute('INSERT INTO desk_shares VALUES(?,?,?,?,?,?,?,?,NULL)',(self._digest(raw),str(data['branch_id']),token,
                       normalize_phone(data.get('phone')) or '',data.get('access_revision',0),gate.stable_id,gate.id,expires))
            return '/shared/'+raw

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
                return {'branch':branch.name,'gate':gate.label}
            request_id='shared_'+secrets.token_hex(16)
            db.execute('UPDATE desk_shares SET request_id=? WHERE id=?',(request_id,row['id']))
        result=access.open_for_token(row['token'],gate.id,request_id=request_id,expected_gate_id=row['gate_id'],expected_phone=row['phone'] or None)
        # Only definitively denied requests may be attempted again.
        if result.get('status')=='error' and result.get('http_status') in (403,429):
            with self.store.connect() as db:
                db.execute('UPDATE desk_shares SET request_id=NULL WHERE id=? AND request_id=?',(row['id'],request_id))
        return result
