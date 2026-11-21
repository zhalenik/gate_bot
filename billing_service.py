"""Branch subscriptions and an immutable quota ledger. No payment/network effects."""
from calendar import monthrange
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import math
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from storage.product_store import ProductStore
from billing_utils import epoch, iso, month_at, identifier

TERMINAL = {'command_sent', 'failed', 'unknown', 'expired', 'cancelled', 'simulated'}










SCHEMA = '''
CREATE TABLE IF NOT EXISTS billing_accounts (
 branch_id TEXT PRIMARY KEY, timezone TEXT NOT NULL, anchor REAL NOT NULL,
 trial_used INTEGER NOT NULL DEFAULT 0, suspended INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS billing_terms (
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES billing_accounts(branch_id),
 source TEXT NOT NULL, plan_id TEXT, starts REAL NOT NULL, ends REAL,
 quota INTEGER, price_kopecks INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
 actor TEXT, reason TEXT, CHECK(quota IS NULL OR quota>=0));
CREATE INDEX IF NOT EXISTS billing_term_branch ON billing_terms(branch_id,starts,ends);
CREATE TABLE IF NOT EXISTS billing_periods (
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL REFERENCES billing_accounts(branch_id),
 starts REAL NOT NULL, ends REAL NOT NULL, term_id TEXT NOT NULL,
 plan_id TEXT, source TEXT NOT NULL, quota INTEGER, price_kopecks INTEGER NOT NULL,
 UNIQUE(branch_id,starts));
CREATE TABLE IF NOT EXISTS billing_calendar_changes (
 branch_id TEXT NOT NULL REFERENCES billing_accounts(branch_id), effective_at REAL NOT NULL,
 timezone TEXT NOT NULL, anchor REAL NOT NULL, first_end REAL NOT NULL, created REAL NOT NULL,
 PRIMARY KEY(branch_id,effective_at));
CREATE TABLE IF NOT EXISTS quota_ledger (
 id TEXT PRIMARY KEY, period_id TEXT NOT NULL REFERENCES billing_periods(id),
 command_id TEXT, entry_type TEXT NOT NULL, reserved_delta INTEGER NOT NULL DEFAULT 0,
 spent_delta INTEGER NOT NULL DEFAULT 0, credit_delta INTEGER NOT NULL DEFAULT 0,
 happened_at REAL NOT NULL, actor TEXT, reason TEXT,
 UNIQUE(command_id,entry_type));
CREATE TRIGGER IF NOT EXISTS quota_no_update BEFORE UPDATE ON quota_ledger BEGIN SELECT RAISE(ABORT,'immutable quota ledger'); END;
CREATE TRIGGER IF NOT EXISTS quota_no_delete BEFORE DELETE ON quota_ledger BEGIN SELECT RAISE(ABORT,'immutable quota ledger'); END;
CREATE TABLE IF NOT EXISTS hardware_commands (
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, gate_id TEXT NOT NULL,
 period_id TEXT NOT NULL REFERENCES billing_periods(id), status TEXT NOT NULL,
 created REAL NOT NULL, deadline REAL NOT NULL, started REAL, finished REAL,
 error_code TEXT, charged INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS command_coalescing ON hardware_commands(branch_id,gate_id,period_id,status);
CREATE TABLE IF NOT EXISTS gate_requests (
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, person_id TEXT NOT NULL,
 branch_client_id TEXT, gate_id TEXT NOT NULL, source TEXT NOT NULL, pass_type TEXT NOT NULL,
 happened_at REAL NOT NULL, status TEXT NOT NULL, reason TEXT, command_id TEXT,
 coalesced INTEGER NOT NULL DEFAULT 0, cooldown_key TEXT, client_role TEXT NOT NULL DEFAULT 'client');
CREATE INDEX IF NOT EXISTS request_branch_time ON gate_requests(branch_id,happened_at);
CREATE INDEX IF NOT EXISTS request_person_time ON gate_requests(person_id,happened_at);
CREATE INDEX IF NOT EXISTS request_gate_time ON gate_requests(gate_id,happened_at);
CREATE TABLE IF NOT EXISTS billing_cooldowns (
 key TEXT PRIMARY KEY, expires REAL NOT NULL, request_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS product_events (
 id TEXT PRIMARY KEY, event_type TEXT NOT NULL, branch_id TEXT, entity_id TEXT,
 happened_at REAL NOT NULL, payload TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS product_events_no_update BEFORE UPDATE ON product_events BEGIN SELECT RAISE(ABORT,'immutable product events'); END;
CREATE TRIGGER IF NOT EXISTS product_events_no_delete BEFORE DELETE ON product_events BEGIN SELECT RAISE(ABORT,'immutable product events'); END;
CREATE TABLE IF NOT EXISTS billing_orders (
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, plan_id TEXT NOT NULL,
 amount_kopecks INTEGER NOT NULL, currency TEXT NOT NULL DEFAULT 'RUB',
 kind TEXT NOT NULL, status TEXT NOT NULL, period_id TEXT, starts REAL, ends REAL,
 created REAL NOT NULL, expires REAL NOT NULL, actor TEXT, snapshot TEXT NOT NULL,
 idempotency_key TEXT NOT NULL, UNIQUE(branch_id,idempotency_key));
CREATE TABLE IF NOT EXISTS billing_payments (
 event_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES billing_orders(id),
 amount_kopecks INTEGER NOT NULL, currency TEXT NOT NULL, happened_at REAL NOT NULL);
CREATE TRIGGER IF NOT EXISTS payments_no_update BEFORE UPDATE ON billing_payments BEGIN SELECT RAISE(ABORT,'immutable payment ledger'); END;
CREATE TRIGGER IF NOT EXISTS payments_no_delete BEFORE DELETE ON billing_payments BEGIN SELECT RAISE(ABORT,'immutable payment ledger'); END;
CREATE TABLE IF NOT EXISTS billing_notice_claims (
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, event_type TEXT NOT NULL,
 payload TEXT NOT NULL, created REAL NOT NULL, delivered INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS client_branch_events (
 branch_client_id TEXT NOT NULL, branch_id TEXT NOT NULL, person_id TEXT NOT NULL,
 created REAL NOT NULL, source TEXT NOT NULL DEFAULT 'local', client_role TEXT NOT NULL DEFAULT 'unknown',
 PRIMARY KEY(branch_id,branch_client_id));
'''


from billing_v2 import BillingV2Mixin


class BillingService(BillingV2Mixin):
    def __init__(self, store=None):
        self.store = store if isinstance(store, ProductStore) else ProductStore(store)
        with self.store.connect() as db:
            statement = ''
            for line in SCHEMA.splitlines():
                statement += line + '\n'
                if sqlite3.complete_statement(statement):
                    db.execute(statement)
                    statement = ''
            if 'client_role' not in {r['name'] for r in db.execute('PRAGMA table_info(gate_requests)')}:
                db.execute("ALTER TABLE gate_requests ADD COLUMN client_role TEXT NOT NULL DEFAULT 'client'")
                db.execute("UPDATE gate_requests SET client_role='admin' WHERE pass_type='admin_role'")
                db.execute("UPDATE gate_requests SET client_role='guest' WHERE pass_type='guest'")
            if 'client_role' not in {r['name'] for r in db.execute('PRAGMA table_info(client_branch_events)')}:
                db.execute("ALTER TABLE client_branch_events ADD COLUMN client_role TEXT NOT NULL DEFAULT 'unknown'")
        self._init_commercial()

    def _transaction(self, db):
        return nullcontext(db) if db is not None else self.store.connect()

    @staticmethod
    def _event(db, kind, branch, entity, at, **payload):
        db.execute('INSERT INTO product_events VALUES(?,?,?,?,?,?)',
            (uuid.uuid4().hex, kind, str(branch) if branch is not None else None, entity, at, json.dumps(payload, ensure_ascii=False)))

    @staticmethod
    def _usage(db, period):
        row = db.execute('SELECT COALESCE(SUM(reserved_delta),0) reserved, COALESCE(SUM(spent_delta),0) spent, COALESCE(SUM(credit_delta),0) credits FROM quota_ledger WHERE period_id=?', (period,)).fetchone()
        return dict(row)

    def _account(self, db, branch, at, zone='Europe/Moscow'):
        row = db.execute('SELECT * FROM billing_accounts WHERE branch_id=?', (branch,)).fetchone()
        if row is None:
            ZoneInfo(zone)
            db.execute('INSERT INTO billing_accounts(branch_id,timezone,anchor,created) VALUES(?,?,?,?)', (branch, zone, at, at))
            # Existing trusted branches stay operational until explicitly enrolled.
            db.execute('INSERT INTO billing_terms VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,branch,'legacy',None,at,None,None,0,at,'migration','Сохранение доступа существующего филиала'))
            row = db.execute('SELECT * FROM billing_accounts WHERE branch_id=?', (branch,)).fetchone()
        return row

    def _term(self, db, branch, at):
        # An expired explicit term must never fall back to the legacy unlimited grant.
        # Cancelled future grants must not supersede a later reconnection.
        # Keep an expired explicit grant instead of reviving legacy access.
        return db.execute('SELECT t.* FROM billing_terms t WHERE t.branch_id=? AND t.starts<=? ORDER BY EXISTS(SELECT 1 FROM billing_cancelled_terms c WHERE c.term_id=t.id),t.starts DESC,t.created DESC,t.rowid DESC LIMIT 1', (branch,at)).fetchone()

    def _legacy_period(self, db, account, term, at):
        existing = db.execute('SELECT * FROM billing_periods WHERE branch_id=? AND starts<=? AND ends>? ORDER BY starts DESC LIMIT 1', (account['branch_id'],at,at)).fetchone()
        if existing:
            if existing['term_id'] != term['id']:
                usage=self._usage(db,existing['id'])
                limit=None if term['quota'] is None else max(term['quota'],usage['reserved']+usage['spent'])
                db.execute('UPDATE billing_periods SET term_id=?,source=?,plan_id=?,quota=?,price_kopecks=? WHERE id=?',(term['id'],term['source'],term['plan_id'],limit,term['price_kopecks'],existing['id']))
                existing=db.execute('SELECT * FROM billing_periods WHERE id=?',(existing['id'],)).fetchone()
            return existing
        calendar=self._calendar(db,account,at)
        anchor, zone = calendar['anchor'], calendar['timezone']
        local, origin = datetime.fromtimestamp(at,ZoneInfo(zone)),datetime.fromtimestamp(anchor,ZoneInfo(zone))
        n = (local.year-origin.year)*12 + local.month-origin.month
        while month_at(anchor,n,zone)>at:n-=1
        while month_at(anchor,n+1,zone)<=at:n+=1
        starts,ends=month_at(anchor,n,zone),month_at(anchor,n+1,zone)
        if calendar.get('effective_at') is not None and calendar['effective_at']<=at<calendar['first_end']:
            starts,ends=calendar['effective_at'],calendar['first_end']
        key=uuid.uuid4().hex
        db.execute('INSERT INTO billing_periods VALUES(?,?,?,?,?,?,?,?,?)',
            (key,account['branch_id'],starts,ends,term['id'],term['plan_id'],term['source'],term['quota'],term['price_kopecks']))
        return db.execute('SELECT * FROM billing_periods WHERE id=?',(key,)).fetchone()

    @staticmethod
    def _calendar(db,account,at):
        row=db.execute('SELECT * FROM billing_calendar_changes WHERE branch_id=? AND effective_at<=? ORDER BY effective_at DESC LIMIT 1',(account['branch_id'],at)).fetchone()
        return dict(row) if row else {'anchor':account['anchor'],'timezone':account['timezone']}

    def schedule_timezone(self, branch_id, timezone, *, actor=None, at=None, db=None):
        """Change only future boundaries, retaining the original local billing day/time."""
        zone=ZoneInfo(timezone);now=epoch(at);branch=identifier(branch_id,'филиал')
        with self._transaction(db) as tx:
            view=self._view(tx,branch,now);account=self._account(tx,branch,now)
            current=self._calendar(tx,account,now)
            effective=tx.execute('SELECT MAX(ends) n FROM billing_periods WHERE branch_id=? AND ends>?',(branch,now)).fetchone()['n']
            effective=effective or epoch(view['period_ends_at'])
            if current['timezone']==timezone:
                tx.execute('DELETE FROM billing_calendar_changes WHERE branch_id=? AND effective_at>?',(branch,now))
                self._event(tx,'billing_timezone_cancelled',branch,None,now,timezone=timezone,actor=actor)
                return {'timezone':timezone,'effective_at':None}
            original=datetime.fromtimestamp(account['anchor'],ZoneInfo(account['timezone']))
            anchor=original.replace(tzinfo=zone).timestamp()
            boundary=datetime.fromtimestamp(effective,ZoneInfo(current['timezone']))
            n=(boundary.year-original.year)*12+boundary.month-original.month
            first_end=month_at(anchor,n+1,timezone)
            if first_end<=effective:raise ValueError('Не удалось определить следующий расчётный период')
            tx.execute('DELETE FROM billing_calendar_changes WHERE branch_id=? AND effective_at>?',(branch,now))
            tx.execute('INSERT INTO billing_calendar_changes VALUES(?,?,?,?,?,?)',(branch,effective,timezone,anchor,first_end,now))
            self._event(tx,'billing_timezone_scheduled',branch,None,now,timezone=timezone,effective_at=iso(effective),first_end=iso(first_end),actor=actor)
            return {'timezone':timezone,'effective_at':iso(effective),'first_period_ends_at':iso(first_end)}

    def _legacy_view(self, db, branch, at, zone='Europe/Moscow'):
        account=self._account(db,branch,at,zone);term=self._term(db,branch,at)
        if term is None:
            return {'branch_id':branch,'status':'pending','available':False,'reason':'subscription_pending',
                    'source':None,'plan_id':None,'term_id':None,'ends_at':None,'period_id':None,
                    'period_starts_at':None,'period_ends_at':None,'quota':0,'unlimited':False,
                    'spent':0,'reserved':0,'credits':0,'remaining':0,'price_kopecks':0,
                    'billing_timezone':self._calendar(db,account,at)['timezone'],'timezone_change':None}
        period=self._period(db,account,term,at);usage=self._usage(db,period['id'])
        limit=period['quota']
        quota=None if limit is None else limit+usage['credits']
        remaining=None if quota is None else max(0,quota-usage['reserved']-usage['spent'])
        expired=term['ends'] is not None and at>=term['ends']
        status='suspended' if account['suspended'] else 'expired' if expired else 'trial' if term['source']=='trial' else 'legacy' if term['source']=='legacy' else 'active'
        available=status not in {'expired','suspended'} and (remaining is None or remaining>0)
        calendar=self._calendar(db,account,at)
        pending=db.execute('SELECT timezone,effective_at FROM billing_calendar_changes WHERE branch_id=? AND effective_at>? ORDER BY effective_at LIMIT 1',(branch,at)).fetchone()
        return {'branch_id':branch,'status':status,'source':term['source'],'plan_id':period['plan_id'],
            'term_id':term['id'],'ends_at':iso(term['ends']),'period_id':period['id'],
            'period_starts_at':iso(period['starts']),'period_ends_at':iso(period['ends']),
            'quota':quota,'unlimited':quota is None,**usage,'remaining':remaining,
            'price_kopecks':period['price_kopecks'],'available':available,
            'billing_timezone':calendar['timezone'],'timezone_change':{'timezone':pending['timezone'],'effective_at':iso(pending['effective_at'])} if pending else None,
            'reason':None if available else 'subscription_'+status if status in {'expired','suspended'} else 'quota_exhausted'}

    def subscription_view(self, branch_id, *, at=None, timezone='Europe/Moscow', db=None):
        with self._transaction(db) as tx:
            return self._view(tx,identifier(branch_id,'филиал'),epoch(at),timezone)

    def cooldown_remaining(self, branch_id, person_id, *, at=None, db=None):
        with self._transaction(db) as tx:
            key=json.dumps([str(branch_id),str(person_id)],ensure_ascii=True)
            row=tx.execute('SELECT expires FROM billing_cooldowns WHERE key=?',(key,)).fetchone()
            return max(0,math.ceil(row['expires']-epoch(at))) if row else 0

    def record_denied(self, request_id, branch_id, person_id, gate_id, reason, source='web', *, branch_client_id=None, pass_type='none', client_role=None, at=None, db=None):
        """Persist AccessService denials without admitting or creating a subscription."""
        client_role = client_role or {'admin_role':'admin','guest':'guest'}.get(pass_type,'client')
        request_id=identifier(request_id,'запрос');branch=identifier(branch_id,'филиал')
        person=identifier(person_id,'клиент');gate=identifier(gate_id,'ворота');now=epoch(at)
        with self._transaction(db) as tx:
            old=tx.execute('SELECT * FROM gate_requests WHERE id=?',(request_id,)).fetchone()
            if old:
                if (old['branch_id'],old['person_id'],old['gate_id'])!=(branch,person,gate):raise ValueError('Ключ запроса уже использован')
                return False
            tx.execute('INSERT INTO gate_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(request_id,branch,person,branch_client_id,gate,source,pass_type,now,'denied',reason,None,0,None,client_role))
            self._event(tx,'gate_denied',branch,request_id,now,reason=reason,person_id=person,gate_id=gate)
            return True


    def admit(self, request_id, branch_id, person_id, gate_id, source='web', *,
              branch_client_id=None, pass_type='manual', client_role=None, command_id=None,
              coalesce=True, simulated=False, cooldown_seconds=20, ttl_seconds=40,
              operational=True, at=None, db=None):
        client_role = client_role or {'admin_role':'admin','guest':'guest'}.get(pass_type,'client')
        request_id=identifier(request_id,'запрос');branch=identifier(branch_id,'филиал');person=identifier(person_id,'клиент');gate=identifier(gate_id,'ворота')
        now=epoch(at)
        if not math.isfinite(cooldown_seconds) or not 0<=cooldown_seconds<=300 or not 0<ttl_seconds<=3600:
            raise ValueError('Некорректные интервалы команды')
        with self._transaction(db) as tx:
            existing=tx.execute('SELECT * FROM gate_requests WHERE id=?',(request_id,)).fetchone()
            if existing:
                if (existing['branch_id'],existing['person_id'],existing['gate_id'])!=(branch,person,gate):
                    raise ValueError('Ключ запроса уже использован для другой команды')
                return {'admitted':existing['status']!='denied','duplicate':True,'command_id':existing['command_id'],'reason':existing['reason'],'coalesced':bool(existing['coalesced']),'request_id':request_id,'delivery':existing['status']}
            view=self._view(tx,branch,now)
            cooldown_key=json.dumps([branch,person],ensure_ascii=True)
            cooling=tx.execute('SELECT expires FROM billing_cooldowns WHERE key=?',(cooldown_key,)).fetchone()
            reason='branch_disabled' if not operational else 'cooldown' if cooling and cooling['expires']>now else view['reason'] if view['status'] in {'expired','suspended','pending','cancelled'} else None
            shared=None
            if not reason and coalesce and not simulated:
                shared=tx.execute('SELECT * FROM hardware_commands WHERE branch_id=? AND gate_id=? AND period_id=? AND status="queued" AND deadline>? ORDER BY created LIMIT 1',(branch,gate,view['period_id'],now)).fetchone()
            if not reason and not shared and not simulated and view['remaining']==0:reason='quota_exhausted'
            if reason:
                tx.execute('INSERT INTO gate_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(request_id,branch,person,branch_client_id,gate,source,pass_type,now,'denied',reason,None,0,cooldown_key,client_role))
                self._event(tx,'gate_denied',branch,request_id,now,reason=reason,person_id=person,gate_id=gate)
                return {'admitted':False,'reason':reason,'request_id':request_id,'retry_after':max(1,math.ceil(cooling['expires']-now)) if reason=='cooldown' else 0}
            command=shared['id'] if shared else identifier(command_id or uuid.uuid4().hex,'команда')
            status='simulated' if simulated else 'queued'
            if not shared:
                tx.execute('INSERT INTO hardware_commands(id,branch_id,gate_id,period_id,status,created,deadline,finished) VALUES(?,?,?,?,?,?,?,?)',(command,branch,gate,view['period_id'],status,now,now+ttl_seconds,now if simulated else None))
                if not simulated:
                    tx.execute('INSERT INTO quota_ledger VALUES(?,?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,view['period_id'],command,'reserve',1,0,0,now,None,None))
            tx.execute('INSERT INTO gate_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(request_id,branch,person,branch_client_id,gate,source,pass_type,now,status,None,command,int(shared is not None),cooldown_key,client_role))
            self._record_request_details(tx,request_id,branch,gate,branch_client_id)
            tx.execute('INSERT INTO billing_cooldowns VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET expires=excluded.expires,request_id=excluded.request_id',(cooldown_key,now+cooldown_seconds,request_id))
            self._event(tx,'gate_admitted',branch,request_id,now,command_id=command,coalesced=shared is not None,simulated=simulated,person_id=person,branch_client_id=branch_client_id,gate_id=gate,source=source,pass_type=pass_type,period_id=view['period_id'])
            return {'admitted':True,'duplicate':False,'request_id':request_id,'command_id':command,'reservation_id':command,'coalesced':shared is not None,'delivery':status,'cooldown_until':iso(now+cooldown_seconds)}

    def authorize_command(self, command_id, *, at=None, db=None):
        now=epoch(at)
        with self._transaction(db) as tx:
            command=tx.execute('SELECT * FROM hardware_commands WHERE id=?',(command_id,)).fetchone()
            if not command:return {'authorized':False,'reason':'command_missing','requests':[]}
            rows=[dict(r) for r in tx.execute('SELECT * FROM gate_requests WHERE command_id=? AND status="queued"',(command_id,))]
            reason='already_started' if command['status']!='queued' else 'queue_expired' if now>=command['deadline'] else 'no_authorized_requests' if not rows else None
            view=self._view(tx,command['branch_id'],now)
            if not reason and view['status'] in {'expired','suspended','pending','cancelled'}:reason=view['reason']
            reserve=tx.execute('SELECT COALESCE(SUM(reserved_delta),0) n FROM quota_ledger WHERE command_id=?',(command_id,)).fetchone()['n']
            if not reason and reserve!=1:reason='reservation_missing'
            if reason and command['status']=='queued':self.finalize(command_id,'expired' if reason=='queue_expired' else 'cancelled',error_code=reason,at=now,db=tx)
            return {'authorized':reason is None,'reason':reason,'requests':rows,'command':dict(command)}

    def finalize(self, command_id, status, error_code=None, *, at=None, db=None):
        if status=='processing':status='started'
        if status not in TERMINAL|{'started'}:raise ValueError('Неизвестный статус команды')
        now=epoch(at)
        with self._transaction(db) as tx:
            command=tx.execute('SELECT * FROM hardware_commands WHERE id=?',(command_id,)).fetchone()
            if not command:return False
            if command['status'] in TERMINAL:return command['status']==status
            reserved=tx.execute('SELECT COALESCE(SUM(reserved_delta),0) n FROM quota_ledger WHERE command_id=?',(command_id,)).fetchone()['n']
            charge=status in {'started','command_sent','unknown'} and not command['charged']
            if reserved and (charge or status in TERMINAL):
                tx.execute('INSERT INTO quota_ledger VALUES(?,?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,command['period_id'],command_id,'charge' if charge else 'release',-1,1 if charge else 0,0,now,None,error_code))
            tx.execute('UPDATE hardware_commands SET status=?,charged=MAX(charged,?),started=COALESCE(started,?),finished=?,error_code=? WHERE id=?',(status,int(charge),now if charge else None,now if status in TERMINAL else None,error_code,command_id))
            tx.execute('UPDATE gate_requests SET status=?,reason=? WHERE command_id=? AND status NOT IN ("cancelled","denied")',(status,error_code,command_id))
            if not charge and not command['charged'] and status in TERMINAL:
                tx.execute('DELETE FROM billing_cooldowns WHERE request_id IN (SELECT id FROM gate_requests WHERE command_id=?)',(command_id,))
            self._event(tx,'command_'+status,command['branch_id'],command_id,now,error_code=error_code)
            return True

    def cancel_request(self, request_id, reason='access_revoked', *, at=None, db=None):
        now=epoch(at)
        with self._transaction(db) as tx:
            row=tx.execute('SELECT * FROM gate_requests WHERE id=?',(request_id,)).fetchone()
            if not row or row['status']!='queued':return False
            tx.execute('UPDATE gate_requests SET status="cancelled",reason=? WHERE id=?',(reason,request_id))
            tx.execute('DELETE FROM billing_cooldowns WHERE request_id=?',(request_id,))
            self._event(tx,'request_cancelled',row['branch_id'],request_id,now,reason=reason)
            if not tx.execute('SELECT 1 FROM gate_requests WHERE command_id=? AND status="queued"',(row['command_id'],)).fetchone():
                self.finalize(row['command_id'],'cancelled',reason,at=now,db=tx)
            return True

    def recover(self, *, at=None, db=None):
        now=epoch(at)
        with self._transaction(db) as tx:
            for row in tx.execute('SELECT id,status FROM hardware_commands WHERE status IN (?,?)',('queued','started')).fetchall():
                self.finalize(row['id'],'unknown' if row['status']=='started' else 'cancelled','runtime_interrupted',at=now,db=tx)

    def grant_gift(self, branch_id, quota, *, actor, ends_at=None, days=None, starts_at=None, mode='extend', reason='', at=None, db=None, preview=False, confirm_shorten=False):
        if type(quota) is not int or not 0<=quota<=10000000:raise ValueError('Квота должна быть целым неотрицательным числом')
        if mode not in {'extend','replace'}:raise ValueError('Некорректный режим подарка')
        now=epoch(at);branch=identifier(branch_id,'филиал')
        with self._transaction(db) as tx:
            account=self._account(tx,branch,now);term=self._term(tx,branch,now)
            scheduled_end=self._scheduled_end(tx,branch,now)
            start=epoch(starts_at) if starts_at is not None else scheduled_end if mode=='extend' else now
            if start<now:raise ValueError('Подарок нельзя применить задним числом')
            if ends_at is not None:end=epoch(ends_at)
            elif type(days) is int and 1<=days<=36500:end=start+days*86400
            else:raise ValueError('Укажите дату окончания или срок в днях')
            if end<=start:raise ValueError('Окончание должно быть позже начала')
            if mode=='extend' and start<scheduled_end:raise ValueError('Продление должно начинаться после уже предоставленного срока')
            if mode=='replace' and tx.execute('SELECT 1 FROM billing_terms t WHERE branch_id=? AND starts>? AND starts<? AND NOT EXISTS(SELECT 1 FROM billing_cancelled_terms c WHERE c.term_id=t.id)',(branch,now,end)).fetchone():
                raise ValueError('Подарок пересекается с будущей подпиской; используйте продление')
            current=self._view(tx,branch,now)
            current_limit=max(quota,current['spent']+current['reserved']) if start<=now else quota
            paid=term['source']=='paid' and term['ends'] and term['ends']>now
            shortens=bool(paid and start<term['ends'] and end<term['ends'])
            details={'starts_at':iso(start),'ends_at':iso(end),'quota':quota,'current_period_quota':current_limit,
                'current_spent':current['spent'],'current_reserved':current['reserved'],'previous_ends_at':current['ends_at'],'shortens_paid_term':shortens,'mode':mode}
            if preview:return details
            if shortens and confirm_shorten is not True:raise ValueError('Подтвердите сокращение уже оплаченного срока в предпросмотре')
            key=uuid.uuid4().hex
            tx.execute('INSERT INTO billing_terms VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,branch,'gift',None,start,end,quota,0,now,actor,reason))
            if start<=now:
                tx.execute('UPDATE billing_periods SET quota=?,source="gift",plan_id=NULL,price_kopecks=0,term_id=? WHERE id=?',(current_limit,key,current['period_id']))
            self._event(tx,'gift_granted',branch,key,now,quota=quota,starts_at=iso(start),ends_at=iso(end),actor=actor,reason=reason,mode=mode)
            return {'gift_id':key,**details}

    @staticmethod
    def _scheduled_end(db, branch, now):
        end=db.execute('SELECT MAX(ends) n FROM billing_terms t WHERE branch_id=? AND ends>? AND NOT EXISTS(SELECT 1 FROM billing_cancelled_terms c WHERE c.term_id=t.id)',(branch,now)).fetchone()['n']
        return max(now,end or now)

    def add_quota(self, branch_id, amount, *, actor, reason, at=None, db=None):
        if type(amount) is not int or not 1<=amount<=10000000:raise ValueError('Количество должно быть положительным целым')
        if not str(reason).strip():raise ValueError('Укажите основание корректировки')
        now=epoch(at)
        with self._transaction(db) as tx:
            view=self._view(tx,str(branch_id),now)
            tx.execute('INSERT INTO quota_ledger VALUES(?,?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,view['period_id'],None,'credit',0,0,amount,now,actor,reason))
            self._event(tx,'quota_gift',branch_id,view['period_id'],now,amount=amount,actor=actor,reason=reason)
            return self._view(tx,str(branch_id),now)

    def suspend(self, branch_id, suspended, *, actor, reason='', at=None, db=None):
        if type(suspended) is not bool:raise ValueError('Некорректный статус')
        now=epoch(at)
        with self._transaction(db) as tx:
            self._account(tx,str(branch_id),now)
            tx.execute('UPDATE billing_accounts SET suspended=? WHERE branch_id=?',(int(suspended),str(branch_id)))
            self._event(tx,'subscription_suspended' if suspended else 'subscription_resumed',branch_id,None,now,actor=actor,reason=reason)




    def checkout(self, order_id, *, branch_id, at=None):
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM billing_orders WHERE id=? AND branch_id=?',(order_id,str(branch_id))).fetchone()
            if row is None:raise ValueError('Заказ не найден')
            return {'status':'payment_unavailable','order_id':order_id,'message':'Приём платежей ещё не подключён. Заказ сохранён, подписка не изменена.','amount_kopecks':row['amount_kopecks'],'expired':epoch(at)>=row['expires']}

    def _legacy_history(self, branch_id):
        with self.store.connect() as db:
            return {'orders':[dict(r) for r in db.execute('SELECT * FROM billing_orders WHERE branch_id=? ORDER BY created DESC LIMIT 100',(str(branch_id),))],
                'terms':[dict(r) for r in db.execute('SELECT * FROM billing_terms WHERE branch_id=? ORDER BY starts DESC',(str(branch_id),))],
                'payments':[dict(r) for r in db.execute('SELECT p.* FROM billing_payments p JOIN billing_orders o ON o.id=p.order_id WHERE o.branch_id=? ORDER BY p.happened_at DESC',(str(branch_id),))]}

    def _legacy_due_notices(self, *, at=None, db=None):
        """Durable domain notices; adapter fans out ONLY to current branch admins."""
        now=epoch(at)
        with self._transaction(db) as tx:
            for account in tx.execute('SELECT * FROM billing_accounts').fetchall():
                view=self._view(tx,account['branch_id'],now);events=[]
                if view['ends_at'] and view['status'] != 'cancelled':
                    days=math.ceil((epoch(view['ends_at'])-now)/86400)
                    successor=tx.execute('SELECT 1 FROM billing_terms t WHERE branch_id=? AND starts>? AND starts<=? AND ends>? AND NOT EXISTS(SELECT 1 FROM billing_cancelled_terms c WHERE c.term_id=t.id)',(account['branch_id'],now,epoch(view['ends_at']),epoch(view['ends_at']))).fetchone()
                    if not successor and days in {14,7,3,1,0,-3,-7}:events.append(('subscription_expired' if days<=0 else 'subscription_expiring',view['term_id']+':'+str(days),{'term_id':view['term_id'],'days_remaining':days,'ends_at':view['ends_at']}))
                if view['status'] != 'cancelled' and view['quota'] is not None and view['quota']>0:
                    for threshold in (80,95,100):
                        if view['spent']*100>=view['quota']*threshold:events.append(('quota_exhausted' if threshold==100 else 'quota_warning',view['period_id']+':'+str(threshold),{'period_id':view['period_id'],'threshold':threshold,'spent':view['spent'],'quota':view['quota'],'resource':'calls'}))
                sms=view['sms']
                if view['status'] != 'cancelled' and sms['quota'] is not None and sms['quota']>0:
                    for threshold in (80,95,100):
                        if sms['spent']*100>=sms['quota']*threshold:events.append(('quota_exhausted' if threshold==100 else 'quota_warning',view['period_id']+':sms:'+str(threshold),{'period_id':view['period_id'],'threshold':threshold,'spent':sms['spent'],'quota':sms['quota'],'resource':'sms'}))
                valid_ids={account['branch_id']+':'+kind+':'+key for kind,key,_ in events}
                # Never deliver an old expiry/usage warning after renewal or a gift.
                for pending in tx.execute('SELECT id FROM billing_notice_claims WHERE branch_id=? AND delivered=0',(account['branch_id'],)).fetchall():
                    if pending['id'] not in valid_ids:tx.execute('UPDATE billing_notice_claims SET delivered=1 WHERE id=?',(pending['id'],))
                for kind,key,payload in events:
                    notice_id=account['branch_id']+':'+kind+':'+key
                    tx.execute('INSERT OR IGNORE INTO billing_notice_claims(id,branch_id,event_type,payload,created) VALUES(?,?,?,?,?)',(notice_id,account['branch_id'],kind,json.dumps(payload),now))
            return [dict(r, payload=json.loads(r['payload'])) for r in tx.execute('SELECT * FROM billing_notice_claims WHERE delivered=0 ORDER BY created')]

    def acknowledge_notice(self, notice_id, *, db=None):
        with self._transaction(db) as tx:tx.execute('UPDATE billing_notice_claims SET delivered=1 WHERE id=?',(notice_id,))
