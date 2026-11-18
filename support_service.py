"""Private administrator support, shared by HTTP and both bot adapters.

Messages and their notification claims commit together. Remote delivery belongs
to the existing runtime worker; neither construction nor HTTP sends to a bot.
"""
import hashlib
import re
import time
import uuid

from storage import config
from storage.sql_documents import read_document


class SupportService:
    def __init__(self, identity, *, clock=time.time, sender=None, ready=None):
        self.identity, self.store, self.clock = identity, identity.product, clock
        self.sender, self.ready = sender, ready
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS support_threads(id INTEGER PRIMARY KEY AUTOINCREMENT,branch_id TEXT NOT NULL,owner_username TEXT NOT NULL,owner_subject TEXT NOT NULL,title TEXT NOT NULL,state TEXT NOT NULL DEFAULT "open",created REAL NOT NULL,updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS support_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,thread_id INTEGER NOT NULL REFERENCES support_threads(id),sender_username TEXT NOT NULL,sender_subject TEXT NOT NULL,sender_role TEXT NOT NULL,body TEXT NOT NULL,request_key TEXT NOT NULL,created REAL NOT NULL,UNIQUE(sender_subject,request_key))')
            db.execute('CREATE TABLE IF NOT EXISTS support_reads(thread_id INTEGER NOT NULL REFERENCES support_threads(id),reader_subject TEXT NOT NULL,last_message INTEGER NOT NULL,PRIMARY KEY(thread_id,reader_subject))')
            db.execute('CREATE TABLE IF NOT EXISTS support_outbox(id TEXT PRIMARY KEY,message_id INTEGER NOT NULL REFERENCES support_messages(id),audience TEXT NOT NULL,recipient_person TEXT NOT NULL,channel TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,retry_at REAL NOT NULL DEFAULT 0,code TEXT,UNIQUE(message_id,recipient_person,channel))')
            db.execute('CREATE INDEX IF NOT EXISTS support_threads_owner ON support_threads(owner_subject,branch_id,updated)')

    @staticmethod
    def _subject(admin):
        # A recreated legacy username must not acquire its predecessor's chats.
        return admin.get('person_id') or 'legacy:' + hashlib.sha256((admin['username'] + ':' + admin.get('password_hash','')).encode()).hexdigest()

    def _actor(self, db, username, branch=None, *, super_only=False):
        actor = self.identity._scope(db,username,branch) if branch is not None else self.identity._admin(db,username)
        if super_only and not actor.get('is_super'):
            raise PermissionError('Обращения других администраторов доступны только суперадминистратору')
        return actor

    def _thread(self, db, username, thread_id):
        if not str(thread_id).isascii() or not str(thread_id).isdigit():
            raise ValueError('Обращение не найдено')
        row=db.execute('SELECT * FROM support_threads WHERE id=?',(int(thread_id),)).fetchone()
        if not row:raise ValueError('Обращение не найдено')
        actor=self._actor(db,username,row['branch_id'])
        if not actor.get('is_super') and self._subject(actor)!=row['owner_subject']:
            raise PermissionError('Нет доступа к обращению')
        return row,actor

    def _owner_active(self, db, thread):
        try:owner=self._actor(db,thread['owner_username'],thread['branch_id'])
        except (ValueError,PermissionError):return False
        return self._subject(owner)==thread['owner_subject']

    def _public_thread(self, db, row, actor):
        branch=read_document(db,'branches.json').get(row['branch_id'],{})
        owner=read_document(db,'admins.json').get(row['owner_username'],{})
        last=db.execute('SELECT id,body,sender_role,created FROM support_messages WHERE thread_id=? ORDER BY id DESC LIMIT 1',(row['id'],)).fetchone()
        read=db.execute('SELECT last_message FROM support_reads WHERE thread_id=? AND reader_subject=?',(row['id'],self._subject(actor))).fetchone()
        last_read=read['last_message'] if read else 0
        unread=db.execute('SELECT count(*) FROM support_messages WHERE thread_id=? AND id>? AND sender_subject<>?',(row['id'],last_read,self._subject(actor))).fetchone()[0]
        return {'id':row['id'],'branch_id':row['branch_id'],'branch_name':branch.get('name','Филиал'),
            'owner_name':str(owner.get('name') or 'Администратор')[:120], 'title':row['title'],'state':row['state'],
            'created':row['created'],'updated':row['updated'],'unread':unread,
            'last_message':dict(last) if last else None,'can_reply':self._owner_active(db,row)}

    def list_threads(self, username, branch_id=None, *, before=None, limit=30):
        limit=min(50,max(1,int(limit)))
        with self.store.connect() as db:
            actor=self._actor(db,username,branch_id)
            clauses=[];params=[]
            if not actor.get('is_super'):
                clauses.append('owner_subject=?');params.append(self._subject(actor))
                allowed=[str(b) for b in actor['branch_ids']]
                clauses.append('branch_id IN ('+','.join('?' for _ in allowed)+')');params.extend(allowed)
            if branch_id is not None:clauses.append('branch_id=?');params.append(str(branch_id))
            if before is not None:
                if not str(before).isdigit():raise ValueError('Некорректная страница')
                cursor,_=self._thread(db,username,before)
                clauses.append('(updated<? OR (updated=? AND id<?))');params.extend((cursor['updated'],cursor['updated'],cursor['id']))
            rows=db.execute('SELECT * FROM support_threads'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY updated DESC,id DESC LIMIT ?',(*params,limit+1)).fetchall()
            return {'items':[self._public_thread(db,row,actor) for row in rows[:limit]],'next_before':rows[limit-1]['id'] if len(rows)>limit else None,'is_super':bool(actor.get('is_super'))}

    def messages(self, username, thread_id, *, after=0, before=None, limit=60):
        if not str(after).isdigit() or (before is not None and not str(before).isdigit()):raise ValueError('Некорректная страница')
        limit=min(100,max(1,int(limit)))
        with self.store.connect() as db:
            thread,actor=self._thread(db,username,thread_id)
            if before is not None:
                rows=db.execute('SELECT * FROM support_messages WHERE thread_id=? AND id<? ORDER BY id DESC LIMIT ?',(thread['id'],int(before),limit+1)).fetchall();rows=list(reversed(rows[:limit]));has_older=bool(rows and db.execute('SELECT 1 FROM support_messages WHERE thread_id=? AND id<?',(thread['id'],rows[0]['id'])).fetchone())
            elif int(after):
                rows=db.execute('SELECT * FROM support_messages WHERE thread_id=? AND id>? ORDER BY id LIMIT ?',(thread['id'],int(after),limit)).fetchall();has_older=False
            else:
                rows=list(reversed(db.execute('SELECT * FROM support_messages WHERE thread_id=? ORDER BY id DESC LIMIT ?',(thread['id'],limit)).fetchall()));has_older=bool(rows and db.execute('SELECT 1 FROM support_messages WHERE thread_id=? AND id<?',(thread['id'],rows[0]['id'])).fetchone())
            return {'thread':self._public_thread(db,thread,actor),'messages':[{'id':r['id'],'text':r['body'],'sender_role':r['sender_role'],'mine':r['sender_subject']==self._subject(actor),'created':r['created']} for r in rows],'has_older':has_older}

    def mark_read(self, username, thread_id, message_id):
        if not str(message_id).isdigit():raise ValueError('Некорректное сообщение')
        with self.store.connect() as db:
            thread,actor=self._thread(db,username,thread_id)
            if not db.execute('SELECT 1 FROM support_messages WHERE id=? AND thread_id=?',(int(message_id),thread['id'])).fetchone():raise ValueError('Сообщение не найдено')
            db.execute('INSERT INTO support_reads VALUES(?,?,?) ON CONFLICT(thread_id,reader_subject) DO UPDATE SET last_message=MAX(last_message,excluded.last_message)',(thread['id'],self._subject(actor),int(message_id)))

    def send(self, username, text, request_key, *, branch_id=None, thread_id=None, title=None):
        if not isinstance(text,str) or not text.strip() or len(text)>4000:raise ValueError('Введите сообщение до 4000 символов')
        if not isinstance(request_key,str) or not re.fullmatch(r'[A-Za-z0-9_:-]{8,160}',request_key):raise ValueError('Некорректный ключ сообщения')
        text=text.strip();now=self.clock()
        with self.store.connect() as db:
            if thread_id is not None:thread,actor=self._thread(db,username,thread_id)
            else:
                if branch_id is None:raise ValueError('Выберите филиал')
                actor=self._actor(db,username,branch_id);thread=None
            subject=self._subject(actor)
            previous=db.execute('SELECT m.id,m.thread_id,m.body,t.branch_id FROM support_messages m JOIN support_threads t ON t.id=m.thread_id WHERE m.sender_subject=? AND m.request_key=?',(subject,request_key)).fetchone()
            if previous:
                self._thread(db,username,previous['thread_id'])
                if previous['body']!=text or (thread_id is not None and int(thread_id)!=previous['thread_id']) or (branch_id is not None and str(branch_id)!=previous['branch_id']):raise ValueError('Этот ключ уже использован для другого сообщения')
                return {'thread_id':previous['thread_id'],'message_id':previous['id'],'duplicate':True}
            if db.execute('SELECT count(*) FROM support_messages WHERE sender_subject=? AND created>?',(subject,now-60)).fetchone()[0]>=20:raise ValueError('Слишком много сообщений. Подождите минуту')
            if thread and not self._owner_active(db,thread):raise PermissionError('Права автора обращения отозваны; ответ недоступен')
            if thread is None:
                heading=(str(title).strip() if title else text.splitlines()[0])[:100]
                cursor=db.execute('INSERT INTO support_threads(branch_id,owner_username,owner_subject,title,created,updated) VALUES(?,?,?,?,?,?)',(str(branch_id),username,subject,heading,now,now));thread=db.execute('SELECT * FROM support_threads WHERE id=?',(cursor.lastrowid,)).fetchone()
            role='superadmin' if actor.get('is_super') and subject!=thread['owner_subject'] else 'administrator'
            cursor=db.execute('INSERT INTO support_messages(thread_id,sender_username,sender_subject,sender_role,body,request_key,created) VALUES(?,?,?,?,?,?,?)',(thread['id'],username,subject,role,text,request_key,now));message_id=cursor.lastrowid
            db.execute('UPDATE support_threads SET updated=?,state="open" WHERE id=?',(now,thread['id']))
            if role=='administrator':
                admins=read_document(db,'admins.json')
                for name in admins:
                    try:recipient=self._actor(db,name,super_only=True)
                    except PermissionError:continue
                    if not recipient.get('person_id') or self._subject(recipient)==subject:continue
                    for binding in db.execute('SELECT platform FROM bot_bindings WHERE person_id=? AND assurance="admin_verified" AND platform IN ("telegram","max")',(recipient['person_id'],)):
                        db.execute('INSERT OR IGNORE INTO support_outbox VALUES(?,?,"superadmin",?,?,"pending",?,?,0,NULL)',(uuid.uuid4().hex,message_id,recipient['person_id'],binding['platform'],now,now))
            db.execute('INSERT INTO support_reads VALUES(?,?,?) ON CONFLICT(thread_id,reader_subject) DO UPDATE SET last_message=MAX(last_message,excluded.last_message)',(thread['id'],subject,message_id))
            return {'thread_id':thread['id'],'message_id':message_id,'duplicate':False}

    def set_state(self, username, thread_id, state):
        if state not in {'open','closed'}:raise ValueError('Некорректное состояние')
        with self.store.connect() as db:
            thread,_=self._thread(db,username,thread_id)
            db.execute('UPDATE support_threads SET state=?,updated=? WHERE id=?',(state,self.clock(),thread['id']))

    def bot_actor(self, platform, user_id):
        actor=self.identity.bot_admin(platform,str(user_id))
        if not actor or not actor.get('is_super'):raise PermissionError('Поддержка в боте доступна суперадминистратору')
        return actor

    def _recipient(self, db, row):
        if row['audience']!='superadmin':return None
        binding=db.execute('SELECT user_id FROM bot_bindings WHERE person_id=? AND platform=? AND assurance="admin_verified"',(row['recipient_person'],row['channel'])).fetchone()
        if not binding:return None
        actor=self.identity.bot_admin(row['channel'],binding['user_id'])
        return binding['user_id'] if actor and actor.get('is_super') and actor['person_id']==row['recipient_person'] else None

    def process_once(self, limit=5):
        if not config.OUTBOUND_ENABLED:return 0
        processed=0
        for _ in range(min(20,max(1,int(limit)))):
            with self.store.connect() as db:
                # A process lost after claiming a send cannot safely repeat it.
                db.execute('UPDATE support_outbox SET state="unknown",code="interrupted" WHERE state="sending" AND updated<?',(self.clock()-180,))
                candidates=db.execute('SELECT * FROM support_outbox WHERE state="pending" AND retry_at<=? ORDER BY created LIMIT 30',(self.clock(),)).fetchall()
                chosen=None
                for candidate in candidates:
                    module=None if self.sender else __import__('telegram_bot' if candidate['channel']=='telegram' else 'max_bot')
                    available=self.ready(candidate['channel']) if self.ready else (True if self.sender else module.status().get('ready'))
                    if available:chosen=candidate;break
                if chosen is None:break
                row=chosen
                message=db.execute('SELECT * FROM support_messages WHERE id=?',(row['message_id'],)).fetchone()
                thread=db.execute('SELECT * FROM support_threads WHERE id=?',(message['thread_id'],)).fetchone()
                recipient=self._recipient(db,row)
                if not recipient or not self._owner_active(db,thread):
                    db.execute('UPDATE support_outbox SET state="skipped",updated=?,code="role_changed" WHERE id=?',(self.clock(),row['id']));continue
                owner=read_document(db,'admins.json').get(thread['owner_username'],{})
                branch=read_document(db,'branches.json').get(thread['branch_id'],{})
                body='Поддержка · обращение №'+str(thread['id'])+'\n'+str(branch.get('name','Филиал'))[:120]+' · '+str(owner.get('name','Администратор'))[:100]+'\n\n'+message['body'][:2200]+'\n\nОткройте меню бота → Обращения в поддержку → №'+str(thread['id'])+', чтобы ответить.'
                db.execute('UPDATE support_outbox SET state="sending",updated=? WHERE id=?',(self.clock(),row['id']))
            try:
                sent=self.sender(row['channel'],recipient,body) if self.sender else __import__('telegram_bot' if row['channel']=='telegram' else 'max_bot').notify_text(recipient,body)
                state='sent' if sent is True else 'pending';code=None if sent is True else 'channel_not_ready'
            except Exception:state='unknown';code='delivery_result_unknown'
            with self.store.connect() as db:
                db.execute('UPDATE support_outbox SET state=?,updated=?,retry_at=?,code=? WHERE id=?',(state,self.clock(),self.clock()+15,code,row['id']))
            processed+=1
        return processed
