"""Explicit superadmin deletion of one branch, entirely inside local storage.

No CRM requests, global configuration changes or phone-based cross-branch deletes.
Immutable billing rows can only be removed by this confirmed whole-branch action;
their triggers are restored before the transaction commits.
"""
import hashlib
import json
from pathlib import Path
import re

from storage.sql_documents import read_document,write_document


class BranchLifecycleService:
    def __init__(self,identity):
        self.identity=identity
        self.store=identity.product

    @staticmethod
    def _tables(db):
        return {row['name'] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    def _context(self,db,username,branch_id):
        actor=self.identity._admin(db,username)
        if actor.get('is_super') is not True:raise PermissionError('Для удаления филиала обратитесь в поддержку GateBot')
        self.identity._scope(db,username,branch_id)
        branch_id=str(branch_id)
        branches=read_document(db,'branches.json');links=read_document(db,'links.json');admins=read_document(db,'admins.json')
        clients={token:client for token,client in links.items() if str(client.get('branch_id'))==branch_id}
        accounts={name:account for name,account in admins.items() if not account.get('is_super') and str(account.get('branch_id'))==branch_id}
        return branch_id,branches,links,admins,clients,accounts

    def _preview(self,db,username,branch_id):
        branch_id,branches,links,admins,clients,accounts=self._context(db,username,branch_id)
        tables=self._tables(db)
        busy=False
        if 'hardware_commands' in tables:
            busy=bool(db.execute('SELECT 1 FROM hardware_commands WHERE branch_id=? AND status IN ("queued","started") LIMIT 1',(branch_id,)).fetchone())
        if 'sms_billing_commands' in tables:
            busy=busy or bool(db.execute('SELECT 1 FROM sms_billing_commands WHERE branch_id=? AND finished IS NULL LIMIT 1',(branch_id,)).fetchone())
        if 'message_outbox' in tables:
            busy=busy or bool(db.execute('SELECT 1 FROM message_outbox WHERE branch_id=? AND state="sending" LIMIT 1',(branch_id,)).fetchone())
        if 'message_effects' in tables:
            # Explicit OTP/invitation effects predate branch metadata on this
            # table; conservatively wait for any running submission, including
            # SMS accepted into the modem queue but not yet physically sent.
            busy=busy or bool(db.execute('SELECT 1 FROM message_effects WHERE state IN ("sending","queued") LIMIT 1').fetchone())
        if {'support_outbox','support_messages','support_threads'}<=tables:
            busy=busy or bool(db.execute('SELECT 1 FROM support_outbox o JOIN support_messages m ON m.id=o.message_id JOIN support_threads t ON t.id=m.thread_id WHERE t.branch_id=? AND o.state="sending" LIMIT 1',(branch_id,)).fetchone())
        revision=hashlib.sha256(json.dumps([branches[branch_id],clients,accounts],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return {'branch_id':branch_id,'name':branches[branch_id].get('name',''),'clients':len(clients),'admins':len(accounts),
                'can_delete':not busy,'reason':'Дождитесь завершения команд ворот и отправки сообщений филиала' if busy else '',
                'revision':revision}

    def preview(self,username,branch_id):
        with self.store.connect() as db:return self._preview(db,username,branch_id)

    def delete(self,username,branch_id,*,confirmation,revision):
        logo=None
        with self.store.connect() as db:
            preview=self._preview(db,username,branch_id)
            if not isinstance(confirmation,str) or confirmation!=preview['branch_id']:
                raise ValueError('Для удаления введите ID филиала')
            if revision!=preview['revision']:raise ValueError('Данные филиала изменились. Откройте подтверждение заново')
            if not preview['can_delete']:raise ValueError(preview['reason'])
            branch_id,branches,links,admins,clients,accounts=self._context(db,username,branch_id)
            tables=self._tables(db);tokens=set(clients)
            people={row['person_id'] for row in db.execute('SELECT person_id FROM branch_clients WHERE branch_id=?',(branch_id,))}
            people.update(account['person_id'] for account in accounts.values() if account.get('person_id'))
            account_names=set(accounts)
            # The retained global superadmin identity must survive deletion of
            # their own client card in this branch.
            for name in account_names:
                admins.pop(name,None);db.execute('DELETE FROM web_accounts WHERE username=?',(name,))
            for token in tokens:links.pop(token,None)
            logo=branches[branch_id].get('logo')
            del branches[branch_id]
            write_document(db,'admins.json',admins)
            write_document(db,'links.json',links)
            write_document(db,'branches.json',branches)

            def remove(table,where,args=()):
                if table in tables:db.execute('DELETE FROM "'+table+'" WHERE '+where,args)

            # References without a branch_id must be removed before parents.
            if 'password_reset_requests' in tables:
                for name in account_names:
                    for row in db.execute('SELECT token_hash,challenge_id FROM password_reset_requests WHERE username=?',(name,)).fetchall():
                        remove('password_reset_sessions','request_hash=?',(row['token_hash'],))
                        remove('identity_otp','id=?',(row['challenge_id'],))
                    remove('password_reset_requests','username=?',(name,))
            for table,prefix in (('identity_invitations','invitation:'),('branch_registrations','registration:')):
                if table in tables:
                    for row in db.execute('SELECT id FROM '+table+' WHERE branch_id=?',(branch_id,)).fetchall():
                        remove('identity_otp','subject=?',(prefix+row['id'],))
            remove('identity_otp','subject LIKE ?',('gate_qr:'+branch_id+':%',))
            if 'support_threads' in tables:
                where='thread_id IN (SELECT id FROM support_threads WHERE branch_id=?)'
                remove('support_outbox','message_id IN (SELECT id FROM support_messages WHERE '+where+')',(branch_id,))
                remove('support_reads',where,(branch_id,))
                remove('support_messages',where,(branch_id,))
            if 'hardware_commands' in tables:
                remove('access_command_routes','command_id IN (SELECT id FROM hardware_commands WHERE branch_id=?)',(branch_id,))
            if 'gate_requests' in tables:
                remove('access_request_auth','request_id IN (SELECT id FROM gate_requests WHERE branch_id=?)',(branch_id,))
                remove('billing_cooldowns','request_id IN (SELECT id FROM gate_requests WHERE branch_id=?)',(branch_id,))
            if 'message_outbox' in tables:
                for row in db.execute('SELECT id,channel FROM message_outbox WHERE branch_id=?',(branch_id,)).fetchall():
                    effect=hashlib.sha256(json.dumps([row['channel'],row['id']]).encode()).hexdigest()
                    remove('message_effects','id=?',(effect,))
            if 'integration_test_requests' in tables:
                for row in db.execute('SELECT id,provider FROM integration_test_requests WHERE branch_id=?',(branch_id,)).fetchall():
                    effect=hashlib.sha256(json.dumps([row['provider'],'integration-test:'+row['id']]).encode()).hexdigest()
                    remove('message_effects','id=?',(effect,))
            for row in db.execute('SELECT token_hash,payload FROM identity_tickets').fetchall():
                payload=json.loads(row['payload'])
                if str(payload.get('branch_id'))==branch_id or payload.get('client_token') in tokens:
                    remove('identity_tickets','token_hash=?',(row['token_hash'],))
            if 'marketplace_intents' in tables:
                # A multi-branch hand-off is short lived; revoke the whole intent
                # when it contains this branch, preventing stale installation.
                for row in db.execute('SELECT id_hash,payload FROM marketplace_intents').fetchall():
                    payload=json.loads(self.identity._fernet().decrypt(row['payload'].encode()))
                    if branch_id in [str(value) for value in payload.get('branches',[])]:
                        remove('marketplace_attempts','intent_hash=?',(row['id_hash'],))
                        remove('marketplace_registrations','intent_hash=?',(row['id_hash'],))
                        remove('marketplace_intents','id_hash=?',(row['id_hash'],))

            triggers=[dict(row) for row in db.execute('SELECT name,sql FROM sqlite_master WHERE type="trigger" AND name IN ("quota_no_delete","sms_quota_no_delete","product_events_no_delete","payments_no_delete")')]
            for trigger in triggers:db.execute('DROP TRIGGER "'+trigger['name']+'"')
            if 'billing_periods' in tables:remove('quota_ledger','period_id IN (SELECT id FROM billing_periods WHERE branch_id=?)',(branch_id,))
            if 'billing_orders' in tables:remove('billing_payments','order_id IN (SELECT id FROM billing_orders WHERE branch_id=?)',(branch_id,))
            # All remaining direct branch references, child tables before the
            # account they reference. Only installation-owned table names enter SQL.
            ordered=['hardware_commands','billing_calendar_changes','billing_periods','billing_terms','billing_accounts']
            scoped=[]
            for table in tables:
                if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',table):continue
                if 'branch_id' in {row['name'] for row in db.execute('PRAGMA table_info("'+table+'")')}:
                    scoped.append(table)
            for table in sorted(set(scoped)-set(ordered))+ordered:
                remove(table,'branch_id=?',(branch_id,))
            for trigger in triggers:db.execute(trigger['sql'])

            for person in people:
                if db.execute('SELECT 1 FROM web_accounts WHERE person_id=?',(person,)).fetchone():continue
                remove('notification_preferences','person_id=? AND audience="admin"',(person,))
                remove('identity_tickets','person_id=? AND purpose="admin_binding"',(person,))
                if db.execute('SELECT 1 FROM branch_clients WHERE person_id=?',(person,)).fetchone():continue
                if db.execute('SELECT 1 FROM system_roles WHERE person_id=?',(person,)).fetchone():continue
                if db.execute('SELECT 1 FROM branch_memberships WHERE person_id=?',(person,)).fetchone():continue
                for table in ('notification_preferences','identity_tickets','bot_bindings'):
                    remove(table,'person_id=?',(person,))
                remove('persons','id=?',(person,))
        if isinstance(logo,str) and re.fullmatch(r'/media/branch-logos/[a-f0-9]{32}\.png',logo):
            path=Path(self.store.path).parent/'branch-logos'/Path(logo).name
            try:path.unlink(missing_ok=True)
            except OSError:pass  # A stale non-public upload must not undo a committed deletion.
        return {'deleted':True,'branch_id':preview['branch_id'],'clients':preview['clients'],'admins':preview['admins']}
