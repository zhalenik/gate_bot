"""Phone proof grants a short password-reset capability, never an admin login.

Uses the shared OTP limits and injected hardware dispatcher. Registration-only
development phone redirects deliberately do not participate in this flow.
"""
import json
import math
import re
import secrets
import uuid

from admin_auth import AdminAuth
from identity_service import OtpRateLimit, valid_phone
from link_manager import normalize_phone
from storage.sql_documents import read_document, write_document


class PasswordRecovery:
    def __init__(self, identity):
        self.identity=identity
        self.store=identity.product
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS password_reset_requests(token_hash TEXT PRIMARY KEY,username TEXT,person_id TEXT,phone TEXT NOT NULL,browser_hash TEXT NOT NULL,auth_proof TEXT,challenge_id TEXT NOT NULL,expires REAL NOT NULL,state TEXT NOT NULL,created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS password_reset_sessions(token_hash TEXT PRIMARY KEY,request_hash TEXT NOT NULL,browser_hash TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL DEFAULT 0)')

    def _proof(self,account):
        return self.identity._hash(json.dumps([account.get(key) for key in
            ('password_hash','session_version','branch_id','person_id','is_super','disabled')]))

    def _account(self,db,username,phone,proof=None):
        try:account=self.identity._admin(db,username)
        except PermissionError:return None
        if account.get('is_super') or account.get('disabled') or not account.get('person_id'):return None
        person=db.execute('SELECT phone,phone_verified FROM persons WHERE id=?',(account['person_id'],)).fetchone()
        if not person or not person['phone_verified'] or person['phone']!=phone:return None
        if proof is not None and not secrets.compare_digest(proof,self._proof(account)):return None
        return account

    def request_code(self,phone,browser,ip=''):
        phone=valid_phone(phone)
        if not isinstance(browser,str) or len(browser)<32:raise ValueError('Обновите страницу восстановления')
        now=self.identity.clock();challenge=uuid.uuid4().hex;raw=secrets.token_urlsafe(32)
        code=f'{secrets.randbelow(1000000):06d}';identifier=self.identity._hash(raw)
        with self.store.connect() as db:
            recent=db.execute('SELECT created FROM identity_otp WHERE phone=? ORDER BY created DESC LIMIT 1',(phone,)).fetchone()
            if recent and recent['created']>now-60:raise OtpRateLimit(math.ceil(recent['created']+60-now))
            ip_hash=self.identity._hash(ip)
            count=db.execute('SELECT count(*) FROM identity_otp WHERE created>? AND (phone=? OR ip_hash=?)',(now-3600,phone,ip_hash)).fetchone()[0]
            if count>=10:raise OtpRateLimit(3600,'Лимит кодов исчерпан. Попробуйте позже')
            matches=[]
            for username,account in read_document(db,'admins.json').items():
                if normalize_phone(account.get('phone'))==phone:
                    current=self._account(db,username,phone)
                    if current:matches.append(current)
            account=matches[0] if len(matches)==1 else None
            db.execute('UPDATE password_reset_requests SET state="superseded" WHERE phone=? AND state="pending"',(phone,))
            db.execute('INSERT INTO password_reset_requests VALUES(?,?,?,?,?,?,?,?,"pending",?)',
                (identifier,account['username'] if account else None,account['person_id'] if account else None,phone,self.identity._hash(browser),self._proof(account) if account else None,challenge,now+300,now))
            db.execute('INSERT INTO identity_otp(id,subject,phone,code_hash,expires,state,created,ip_hash) VALUES(?,?,?,?,?,"sending",?,?)',
                (challenge,'password_reset:'+identifier,phone,self.identity._code_hash(challenge,code),now+300,now,ip_hash))
        delivery={'status':'failed','code':'not_eligible'}
        if account:
            delivery=self.identity._send('sms',phone,'GateBot: '+code+'. Код сброса пароля. Никому не сообщайте.',
                {'purpose':'otp','event':'password_reset','idempotency_key':challenge,'branch_id':str(account['branch_id'])})
        ready=delivery.get('status') in {'sent','queued','success','network_accepted','accepted'} and delivery.get('delivery') not in {'simulated','dry_run','stub'}
        with self.store.connect() as db:
            db.execute('UPDATE identity_otp SET state=?,delivery=? WHERE id=?',('ready' if ready else 'failed',json.dumps(delivery),challenge))
        # An unknown number, a console superadmin and a valid account have the
        # same public response. No account is modified before proof succeeds.
        return {'request_token':raw,'retry_after':60,'expires_at':now+300,
                'message':'Если номер связан с аккаунтом администратора филиала, на него придёт SMS-код.'}

    def verify_code(self,raw,code,browser):
        reset=None;now=self.identity.clock()
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM password_reset_requests WHERE token_hash=?',(self.identity._hash(raw),)).fetchone()
            if row and row['state']=='pending' and row['expires']>now and secrets.compare_digest(row['browser_hash'],self.identity._hash(browser)):
                otp=self.identity._check_otp(db,'password_reset:'+row['token_hash'],row['challenge_id'],code)
                account=self._account(db,row['username'],row['phone'],row['auth_proof']) if otp and row['username'] else None
                if account:
                    reset=secrets.token_urlsafe(32)
                    db.execute('UPDATE identity_otp SET state="used" WHERE id=?',(row['challenge_id'],))
                    db.execute('UPDATE password_reset_requests SET state="verified" WHERE token_hash=?',(row['token_hash'],))
                    db.execute('INSERT INTO password_reset_sessions VALUES(?,?,?,?,0)',(self.identity._hash(reset),row['token_hash'],row['browser_hash'],now+600))
        if reset is None:raise ValueError('Код неверен, истёк или исчерпан лимит попыток')
        return reset

    def complete(self,raw,browser,password,confirmation):
        AdminAuth.validate_new_password(password)
        if password!=confirmation:raise ValueError('Пароли не совпадают')
        with self.store.connect() as db:
            ticket=db.execute('SELECT * FROM password_reset_sessions WHERE token_hash=?',(self.identity._hash(raw),)).fetchone()
            if not ticket or ticket['used'] or ticket['expires']<=self.identity.clock() or not secrets.compare_digest(ticket['browser_hash'],self.identity._hash(browser)):
                raise ValueError('Время восстановления истекло. Запросите новый SMS-код')
            row=db.execute('SELECT * FROM password_reset_requests WHERE token_hash=?',(ticket['request_hash'],)).fetchone()
            account=self._account(db,row['username'],row['phone'],row['auth_proof']) if row and row['state']=='verified' else None
            if not account:raise ValueError('Аккаунт изменился. Начните восстановление заново')
            admins=read_document(db,'admins.json');admin=admins[row['username']]
            admin['password_hash']=AdminAuth.hash_password(password)
            admin['session_version']=int(admin.get('session_version',0))+1
            write_document(db,'admins.json',admins)
            db.execute('UPDATE password_reset_sessions SET used=1 WHERE request_hash IN (SELECT token_hash FROM password_reset_requests WHERE username=?)',(row['username'],))
            db.execute('UPDATE password_reset_requests SET state="completed" WHERE username=?',(row['username'],))
            db.execute('UPDATE identity_tickets SET used=1 WHERE person_id=? AND purpose="admin_binding"',(account['person_id'],))
            db.execute('UPDATE bot_bindings SET assurance="client",revision=revision+1 WHERE person_id=? AND assurance="admin_verified"',(account['person_id'],))
        return {'username':row['username'],'status':'success'}
