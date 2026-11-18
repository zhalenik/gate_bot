"""Catalog-backed terms, consent and orders; payment execution remains unavailable."""
from datetime import datetime, timedelta
import hashlib
import json
import math
import uuid

from billing_catalog import BillingCatalog, integer
from billing_utils import epoch, identifier, iso, month_at

UNSET = object()


class BillingV2Mixin:
    def _init_commercial(self):
        self.catalog = BillingCatalog(self.store)
        from billing_sms import SmsBilling, ensure_schema
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS billing_term_details(term_id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,snapshot TEXT NOT NULL,sms_quota INTEGER,period_policy TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS billing_renewals(branch_id TEXT PRIMARY KEY,enabled INTEGER NOT NULL DEFAULT 0,access_cancelled INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 0,consented_at REAL,cancelled_at REAL,offer_version TEXT,plan_snapshot TEXT,actor TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS billing_consents(id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,actor TEXT NOT NULL,kind TEXT NOT NULL,offer_version TEXT NOT NULL,order_id TEXT,accepted_at REAL NOT NULL,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS billing_package_grants(id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,term_id TEXT NOT NULL,period_id TEXT NOT NULL,package_id TEXT NOT NULL,resource TEXT NOT NULL,quantity INTEGER NOT NULL,starts REAL NOT NULL,ends REAL NOT NULL,source TEXT NOT NULL,price_kopecks INTEGER NOT NULL,snapshot TEXT NOT NULL,order_id TEXT UNIQUE)')
            db.execute('CREATE TABLE IF NOT EXISTS billing_request_details(request_id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,phone TEXT,client_name TEXT,client_token TEXT,gate_name TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS billing_renewal_intents(id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,term_id TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,due REAL NOT NULL,created REAL NOT NULL,snapshot TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS billing_test_activations(branch_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,term_id TEXT NOT NULL,request TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(branch_id,idempotency_key))')
            cancellation_table_exists = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='billing_cancelled_terms'").fetchone())
            db.execute('CREATE TABLE IF NOT EXISTS billing_cancelled_terms(branch_id TEXT NOT NULL,term_id TEXT PRIMARY KEY,cancelled_at REAL NOT NULL)')
            # Preserve an old installation's cancellation, while allowing a
            # subsequently purchased/granted term to reconnect at its start.
            if not cancellation_table_exists:
                db.execute('INSERT OR IGNORE INTO billing_cancelled_terms SELECT t.branch_id,t.id,COALESCE(r.cancelled_at,t.created) FROM billing_terms t JOIN billing_renewals r ON r.branch_id=t.branch_id WHERE r.access_cancelled=1')
            ensure_schema(db)
            # Keep historical periods and consumption intact. A trial becomes
            # identifiable in the catalog without restarting or extending it.
            trial = self.catalog.get('plan', 'trial', db=db)
            for row in db.execute('SELECT * FROM billing_terms WHERE source="trial" AND id NOT IN (SELECT term_id FROM billing_term_details)').fetchall():
                snapshot = {**trial, 'duration_value': 2, 'duration_unit': 'months', 'quota': row['quota'], 'sms_quota': None}
                db.execute('INSERT INTO billing_term_details VALUES(?,?,?,?,?)',(row['id'],row['branch_id'],json.dumps(snapshot,ensure_ascii=False),None,'legacy'))
                db.execute('UPDATE billing_terms SET plan_id="trial" WHERE id=?',(row['id'],))
                db.execute('UPDATE billing_periods SET plan_id="trial" WHERE term_id=?',(row['id'],))
        self.sms = SmsBilling(self)

    @staticmethod
    def _controls(db, branch):
        row = db.execute('SELECT * FROM billing_renewals WHERE branch_id=?',(branch,)).fetchone()
        return dict(row) if row else {'branch_id':branch,'enabled':0,'access_cancelled':0,'revision':0,'consented_at':None,'cancelled_at':None,'offer_version':None,'plan_snapshot':None,'actor':None}

    @staticmethod
    def _set_controls(db, branch, **changes):
        state = BillingV2Mixin._controls(db, branch)
        state.update(changes)
        db.execute('INSERT INTO billing_renewals VALUES(:branch_id,:enabled,:access_cancelled,:revision,:consented_at,:cancelled_at,:offer_version,:plan_snapshot,:actor) ON CONFLICT(branch_id) DO UPDATE SET enabled=excluded.enabled,access_cancelled=excluded.access_cancelled,revision=excluded.revision,consented_at=excluded.consented_at,cancelled_at=excluded.cancelled_at,offer_version=excluded.offer_version,plan_snapshot=excluded.plan_snapshot,actor=excluded.actor',state)
        return state

    def _period(self, db, account, term, at):
        details = db.execute('SELECT * FROM billing_term_details WHERE term_id=?',(term['id'],)).fetchone()
        if not details or details['period_policy'] != 'term':
            return self._legacy_period(db, account, term, at)
        existing = db.execute('SELECT * FROM billing_periods WHERE term_id=? ORDER BY starts LIMIT 1',(term['id'],)).fetchone()
        if existing: return existing
        starts = term['starts']
        # Legacy schema has UNIQUE(branch,starts). Two explicit changes can
        # share a clock instant; retain both immutable consumption periods.
        while db.execute('SELECT 1 FROM billing_periods WHERE branch_id=? AND starts=?',(account['branch_id'],starts)).fetchone():
            starts = math.nextafter(starts, -math.inf)
        key = uuid.uuid4().hex
        db.execute('INSERT INTO billing_periods VALUES(?,?,?,?,?,?,?,?,?)',(key,account['branch_id'],starts,term['ends'],term['id'],term['plan_id'],term['source'],term['quota'],term['price_kopecks']))
        return db.execute('SELECT * FROM billing_periods WHERE id=?',(key,)).fetchone()

    def _view(self, db, branch, at, zone='Europe/Moscow'):
        view = self._legacy_view(db, branch, at, zone)
        term = self._term(db, branch, at)
        detail = db.execute('SELECT * FROM billing_term_details WHERE term_id=?',(view['term_id'],)).fetchone() if view['term_id'] else None
        plan = json.loads(detail['snapshot']) if detail else {'id':view['plan_id'],'name': 'Сохранённый доступ' if view['source']=='legacy' else 'Ранее предоставленный тариф','quota':view['quota'],'sms_quota':0,'logo':'','kind':'legacy'}
        if detail and detail['period_policy']=='term':
            view['period_starts_at'], view['period_ends_at'] = iso(term['starts']), iso(term['ends'])
        sms_base = detail['sms_quota'] if detail else None if view['source']=='trial' else 0
        usage = self.sms.usage(db, view['period_id']) if view['period_id'] else {'spent':0,'reserved':0,'credits':0}
        sms_limit = None if sms_base is None else sms_base + usage['credits']
        controls = self._controls(db, branch)
        term_cancelled = bool(term and db.execute('SELECT 1 FROM billing_cancelled_terms WHERE term_id=?',(term['id'],)).fetchone())
        if controls['access_cancelled'] and term is not None and not term_cancelled:
            controls = self._set_controls(db,branch,access_cancelled=0)
        if term_cancelled or controls['access_cancelled']:
            view.update(status='cancelled',available=False,reason='subscription_cancelled')
        if view['status'] in {'expired','cancelled'}:
            view['remaining'] = 0
        packages = []
        if view['period_id']:
            for row in db.execute('SELECT * FROM billing_package_grants WHERE period_id=? ORDER BY starts,id',(view['period_id'],)):
                snapshot = json.loads(row['snapshot'])
                packages.append({**dict(row), 'name':snapshot['name'],'logo':snapshot.get('logo',''),'starts_at':iso(row['starts']),'ends_at':iso(row['ends'])})
        scheduled = []
        for row in db.execute('SELECT t.*,d.snapshot FROM billing_terms t LEFT JOIN billing_term_details d ON d.term_id=t.id WHERE t.branch_id=? AND t.starts>? AND NOT EXISTS(SELECT 1 FROM billing_cancelled_terms c WHERE c.term_id=t.id) ORDER BY t.starts',(branch,at)):
            data = dict(row); data.update(starts_at=iso(row['starts']),ends_at=iso(row['ends']),plan=json.loads(row['snapshot']) if row['snapshot'] else None)
            scheduled.append(data)
        pending = db.execute('SELECT * FROM billing_orders WHERE branch_id=? AND status="awaiting_payment" AND starts>created ORDER BY created DESC LIMIT 1',(branch,)).fetchone()
        accepted = db.execute('SELECT offer_version FROM billing_consents WHERE branch_id=? AND kind="offer" ORDER BY accepted_at DESC,rowid DESC LIMIT 1',(branch,)).fetchone()
        has_current_tariff = view['status'] in {'active','trial','suspended'} and bool(view['ends_at'] and epoch(view['ends_at']) > at)
        view.update(plan=plan, has_current_tariff=has_current_tariff, current_plan=plan if has_current_tariff else None,
            calls={key:view[key] for key in ('quota','spent','reserved','credits','remaining','unlimited')},
            sms={'quota':sms_limit,**usage,'remaining':0 if view['status'] in {'expired','cancelled'} else None if sms_limit is None else max(0,sms_limit-usage['spent']-usage['reserved']),'unlimited':sms_limit is None},
            renewal={'enabled':bool(controls['enabled']),'consented_at':iso(controls['consented_at']),'cancelled_at':iso(controls['cancelled_at']),
                     'offer_version':controls['offer_version'],'next_charge_at':iso(max([epoch(view['ends_at'])]+[row['ends'] for row in scheduled if row['ends']])) if controls['enabled'] and view['ends_at'] else None,
                     'payment_available':False,'status':'payment_unavailable' if controls['enabled'] else 'disabled','revision':controls['revision']},
            packages=packages,scheduled_terms=scheduled,scheduled_change=dict(pending) if pending else None,
            offer=self.catalog.offer(db=db), accepted_offer=self.catalog.offer(accepted['offer_version'],db=db) if accepted else None,
            payment_available=False,
            packages_available=bool(detail and detail['period_policy']=='term' and view['status'] in {'active','trial'}),
            packages_unavailable_reason=('Дополнительные пакеты доступны после подключения тарифа из нового каталога' if not detail or detail['period_policy']!='term' else 'Пакет доступен только в рамках действующего тарифа' if view['status'] not in {'active','trial'} else None))
        return view

    @staticmethod
    def _duration_end(plan, start, zone):
        return month_at(start,plan['duration_value'],zone) if plan['duration_unit']=='months' else start+plan['duration_value']*86400

    def _activate_term(self, db, branch, plan, start, end, source, actor, now, reason, *, price=None):
        key = uuid.uuid4().hex
        db.execute('INSERT INTO billing_terms VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,branch,source,plan.get('id'),start,end,plan['quota'],plan['price_kopecks'] if price is None else price,now,actor,reason))
        db.execute('INSERT INTO billing_term_details VALUES(?,?,?,?,?)',(key,branch,json.dumps(plan,ensure_ascii=False),plan['sms_quota'],'term'))
        if start <= now:
            db.execute('UPDATE billing_periods SET ends=? WHERE branch_id=? AND starts<=? AND ends>? AND term_id<>?',(start,branch,start,start,key))
            for row in db.execute('SELECT id FROM hardware_commands WHERE branch_id=? AND status="queued"',(branch,)).fetchall():
                self.finalize(row['id'],'cancelled','tariff_changed',at=now,db=db)
            self._set_controls(db,branch,access_cancelled=0)
        self._event(db,'tariff_activated',branch,key,now,source=source,plan_id=plan.get('id'),name=plan['name'],starts_at=iso(start),ends_at=iso(end),actor=actor)
        return key

    def activate_trial(self, branch_id, timezone='Europe/Moscow', *, at=None, db=None):
        branch, now = identifier(branch_id,'филиал'), epoch(at)
        with self._transaction(db) as tx:
            account = self._account(tx,branch,now,timezone)
            if account['trial_used']: return self._view(tx,branch,now)
            if tx.execute('SELECT 1 FROM gate_requests WHERE branch_id=? LIMIT 1',(branch,)).fetchone() or tx.execute('SELECT 1 FROM billing_terms WHERE branch_id=? AND source NOT IN ("legacy","trial") LIMIT 1',(branch,)).fetchone():
                raise ValueError('Пробный тариф доступен только новому филиалу')
            plan = self.catalog.get('plan','trial',db=tx)
            end = self._duration_end(plan,now,timezone)
            tx.execute('UPDATE billing_accounts SET trial_used=1,timezone=?,anchor=? WHERE branch_id=?',(timezone,now,branch))
            key = self._activate_term(tx,branch,plan,now,end,'trial',None,now,'Пробный тариф',price=0)
            self._event(tx,'trial_activated',branch,key,now,ends_at=iso(end),unlimited=plan['quota'] is None)
            return self._view(tx,branch,now)

    def quote(self, branch_id, plan_id, *, mode='period_end', at=None, db=None):
        if mode not in ('now','period_end'): raise ValueError('Выберите начало сейчас или после текущего периода')
        now, branch = epoch(at), identifier(branch_id,'филиал')
        with self._transaction(db) as tx:
            plan = self.catalog.get('plan',plan_id,include_hidden=False,db=tx)
            return self._plan_quote(tx,branch,plan,mode,now)

    def _plan_quote(self, tx, branch, plan, mode, now):
        if plan['kind']=='trial': raise ValueError('Пробный тариф нельзя оплатить или продлить')
        view = self._view(tx,branch,now)
        starts = self._scheduled_end(tx,branch,now) if mode=='period_end' else now
        if starts==now and self._has_future_term(tx,branch,now):
            raise ValueError('У филиала уже предоставлен следующий период. Выберите смену после его окончания')
        account = self._account(tx,branch,now)
        ends = self._duration_end(plan,starts,self._calendar(tx,account,starts)['timezone'])
        return {'schema_version':2,'plan':plan,'kind':'tariff','mode':mode,'amount_kopecks':plan['price_kopecks'],'currency':'RUB',
                'starts_at':iso(starts),'ends_at':iso(ends),'period_id':None,'current_term_id':view['term_id'],
                'control_revision':self._controls(tx,branch)['revision'],'current_usage':view['spent'],'payment_available':False,
                'burn_required':bool(mode=='now' and view['has_current_tariff']),'burn':{'calls_remaining':view['remaining'],'sms_remaining':view['sms']['remaining'],'ends_at':view['ends_at']},
                'offer':self.catalog.offer(db=tx)}

    @staticmethod
    def _has_future_term(db, branch, now):
        return bool(db.execute('SELECT 1 FROM billing_terms t WHERE branch_id=? AND starts>? AND NOT EXISTS(SELECT 1 FROM billing_cancelled_terms c WHERE c.term_id=t.id)',(branch,now)).fetchone())

    def _accept_offer(self, db, branch, actor, version, accepted, order_id, now):
        offer = self.catalog.offer(db=db)
        if not offer: raise ValueError('Оферта пока не опубликована. Обратитесь к поддержке')
        if version != offer['id']: raise ValueError('Условия оферты изменились. Откройте актуальную версию')
        existing = db.execute('SELECT 1 FROM billing_consents WHERE branch_id=? AND kind="offer" AND offer_version=?',(branch,version)).fetchone()
        if accepted is not True and not existing: raise ValueError('Необходимо принять оферту')
        if accepted is True:
            db.execute('INSERT INTO billing_consents VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,branch,str(actor),'offer',version,order_id,now,json.dumps({'sha256':offer['sha256'],'url':offer['url']})))
            self._event(db,'offer_accepted',branch,order_id,now,actor=actor,offer_version=version)
        return offer

    def _create_v2_order(self, tx, branch, quote, request, actor, key, now):
        previous = tx.execute('SELECT * FROM billing_orders WHERE branch_id=? AND idempotency_key=?',(branch,key)).fetchone()
        if previous:
            if json.loads(previous['snapshot']).get('request') != request: raise ValueError('Ключ заказа уже использован для других условий')
            return dict(previous)
        order_id = uuid.uuid4().hex
        self._accept_offer(tx,branch,actor,request['offer_version'],request['accept_offer'],order_id,now)
        if quote.get('burn_required') and request.get('confirm_burn') is not True: raise ValueError('Подтвердите сгорание остатка действующего тарифа и пакетов')
        if request.get('auto_renew'):
            tx.execute('INSERT INTO billing_consents VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,branch,str(actor),'renewal',request['offer_version'],order_id,now,json.dumps({'plan':quote['plan'],'separate_opt_in':True},ensure_ascii=False)))
            self._event(tx,'renewal_consent_accepted',branch,order_id,now,actor=actor,offer_version=request['offer_version'])
        snapshot = {**quote,'request':request,'actor':str(actor)}
        item_id = quote['plan']['id'] if quote['kind']=='tariff' else 'package:'+quote['package']['id']
        expiry = min(now+3600,epoch(quote['ends_at']))
        tx.execute('INSERT INTO billing_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(order_id,branch,item_id,quote['amount_kopecks'],'RUB',quote['kind'],'awaiting_payment',quote.get('period_id'),epoch(quote['starts_at']),epoch(quote['ends_at']),now,expiry,str(actor),json.dumps(snapshot,ensure_ascii=False),key))
        self._event(tx,'tariff_order_created' if quote['kind']=='tariff' else 'package_order_created',branch,order_id,now,amount_kopecks=quote['amount_kopecks'],item_id=item_id,actor=actor)
        return dict(tx.execute('SELECT * FROM billing_orders WHERE id=?',(order_id,)).fetchone())

    def create_order(self, branch_id, plan_id, *, actor, idempotency_key, mode='period_end', offer_version=None, accept_offer=False, auto_renew=False, confirm_burn=False, at=None, db=None):
        if any(type(value) is not bool for value in (accept_offer,auto_renew,confirm_burn)): raise ValueError('Некорректное согласие')
        now, branch = epoch(at), identifier(branch_id,'филиал')
        request = {'plan_id':plan_id,'mode':mode,'offer_version':offer_version,'accept_offer':accept_offer,'auto_renew':auto_renew,'confirm_burn':confirm_burn}
        with self._transaction(db) as tx:
            self.catalog.get('plan',plan_id,include_hidden=False,db=tx)
            # Idempotency is checked before reading a potentially changed price.
            key = identifier(idempotency_key,'ключ заказа')
            previous = tx.execute('SELECT * FROM billing_orders WHERE branch_id=? AND idempotency_key=?',(branch,key)).fetchone()
            if previous:
                if json.loads(previous['snapshot']).get('request') != request: raise ValueError('Ключ заказа уже использован для других условий')
                return dict(previous)
            return self._create_v2_order(tx,branch,self.quote(branch,plan_id,mode=mode,at=now,db=tx),request,actor,key,now)

    def package_quote(self, branch_id, package_id, *, at=None, db=None):
        now, branch = epoch(at), identifier(branch_id,'филиал')
        with self._transaction(db) as tx:
            package = self.catalog.get('package',package_id,db=tx)
            view = self._view(tx,branch,now)
            if view['status'] not in {'active','trial'} or not view['ends_at'] or epoch(view['ends_at'])<=now:
                raise ValueError('Пакет доступен только в рамках действующего тарифа')
            if view[package['resource']]['quota'] is None: raise ValueError('Для этого ресурса уже включён безлимит')
            detail = tx.execute('SELECT period_policy FROM billing_term_details WHERE term_id=?',(view['term_id'],)).fetchone()
            if detail is None or detail['period_policy']!='term':
                raise ValueError('Дополнительные пакеты доступны после подключения тарифа из нового каталога')
            return {'schema_version':2,'kind':'package','package':package,'amount_kopecks':package['price_kopecks'],'currency':'RUB',
                    'starts_at':iso(now),'ends_at':view['ends_at'],'period_id':view['period_id'],'current_term_id':view['term_id'],
                    'control_revision':self._controls(tx,branch)['revision'],'payment_available':False,'offer':self.catalog.offer(db=tx)}

    def create_package_order(self, branch_id, package_id, *, actor, idempotency_key, offer_version=None, accept_offer=False, at=None, db=None):
        if type(accept_offer) is not bool: raise ValueError('Некорректное согласие')
        now, branch = epoch(at), identifier(branch_id,'филиал')
        request = {'package_id':package_id,'offer_version':offer_version,'accept_offer':accept_offer}
        with self._transaction(db) as tx:
            key = identifier(idempotency_key,'ключ заказа')
            previous = tx.execute('SELECT * FROM billing_orders WHERE branch_id=? AND idempotency_key=?',(branch,key)).fetchone()
            if previous:
                if json.loads(previous['snapshot']).get('request') != request: raise ValueError('Ключ заказа уже использован для других условий')
                return dict(previous)
            return self._create_v2_order(tx,branch,self.package_quote(branch,package_id,at=now,db=tx),request,actor,key,now)

    def _grant_package(self, db, branch, package, view, now, source, actor, reason, order_id=None):
        key = uuid.uuid4().hex
        db.execute('INSERT INTO billing_package_grants VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,branch,view['term_id'],view['period_id'],package['id'],package['resource'],package['quantity'],now,epoch(view['ends_at']),source,package['price_kopecks'] if source=='paid' else 0,json.dumps(package,ensure_ascii=False),order_id))
        if package['resource']=='calls':
            db.execute('INSERT INTO quota_ledger VALUES(?,?,?,?,?,?,?,?,?,?)',(key,view['period_id'],None,'credit',0,0,package['quantity'],now,actor,reason))
        else:
            self.sms.add_credit(db,branch,view['period_id'],package['quantity'],actor=actor,reason=reason,at=now,credit_id=key)
        self._event(db,'package_activated',branch,key,now,name=package['name'],resource=package['resource'],quantity=package['quantity'],ends_at=view['ends_at'],source=source,actor=actor)
        return key

    def apply_payment(self, order_id, event_id, amount_kopecks, currency='RUB', *, at=None, db=None):
        """Only a future verified gateway adapter may call this; no public route."""
        now, event_id = epoch(at), identifier(event_id,'событие оплаты')
        integer(amount_kopecks,'Сумма',maximum=1000000000)
        with self._transaction(db) as tx:
            old = tx.execute('SELECT * FROM billing_payments WHERE event_id=?',(event_id,)).fetchone()
            if old:
                if (old['order_id'],old['amount_kopecks'],old['currency'])!=(order_id,amount_kopecks,currency): raise ValueError('Событие оплаты уже использовано')
                return {'applied':True,'duplicate':True,'order_id':order_id}
            order = tx.execute('SELECT * FROM billing_orders WHERE id=?',(order_id,)).fetchone()
            if not order or order['status']!='awaiting_payment': raise ValueError('Заказ не найден или уже обработан')
            if (amount_kopecks,currency)!=(order['amount_kopecks'],order['currency']): raise ValueError('Сумма или валюта не соответствует заказу')
            if now>=order['expires']: raise ValueError('Срок заказа истёк; требуется сверка платежа')
            snapshot = json.loads(order['snapshot']); branch = order['branch_id']
            if snapshot.get('schema_version') != 2: raise ValueError('Заказ создан по прежним условиям; требуется сверка платежа')
            controls = self._controls(tx,branch)
            if snapshot['control_revision'] != controls['revision']: raise ValueError('Подписка или согласие изменились; требуется сверка платежа')
            view = self._view(tx,branch,now)
            if view['term_id'] != snapshot['current_term_id']: raise ValueError('Тариф изменился; требуется сверка платежа')
            if order['kind']=='package':
                if view['period_id']!=order['period_id'] or view['status'] not in {'active','trial'} or now>=epoch(view['ends_at']): raise ValueError('Период пакета завершился; требуется сверка платежа')
                entity = self._grant_package(tx,branch,snapshot['package'],view,now,'paid',order['actor'],'Оплата пакета',order_id)
            else:
                plan = snapshot['plan']
                if snapshot['mode']=='period_end' and order['starts']>order['created'] and abs(self._scheduled_end(tx,branch,now)-order['starts'])>0.001: raise ValueError('Период подписки изменился; требуется сверка платежа')
                if order['starts']<=order['created'] and self._has_future_term(tx,branch,now): raise ValueError('Будущая подписка изменилась; требуется сверка платежа')
                start = max(now,order['starts'])
                account = self._account(tx,branch,now)
                end = self._duration_end(plan,start,self._calendar(tx,account,start)['timezone'])
                entity = self._activate_term(tx,branch,plan,start,end,'paid',order['actor'],now,'Оплата заказа '+order_id)
                enabled = bool(snapshot['request'].get('auto_renew'))
                self._set_controls(tx,branch,enabled=int(enabled),revision=controls['revision']+1,consented_at=now if enabled else controls['consented_at'],
                    cancelled_at=None if enabled else now,offer_version=snapshot['request']['offer_version'],plan_snapshot=json.dumps(plan,ensure_ascii=False),actor=order['actor'])
            tx.execute('INSERT INTO billing_payments VALUES(?,?,?,?,?)',(event_id,order_id,amount_kopecks,currency,now))
            tx.execute('UPDATE billing_orders SET status="paid" WHERE id=?',(order_id,))
            self._event(tx,'payment_applied',branch,order_id,now,event_id=event_id,amount_kopecks=amount_kopecks,entity_id=entity)
            return {'applied':True,'duplicate':False,'order_id':order_id,'term_id':entity if order['kind']=='tariff' else None,'package_id':entity if order['kind']=='package' else None}

    def set_renewal(self, branch_id, enabled, *, actor, consent=False, offer_version=None, accept_offer=False, at=None, db=None):
        if any(type(value) is not bool for value in (enabled,consent,accept_offer)): raise ValueError('Некорректное согласие')
        branch, now = identifier(branch_id,'филиал'), epoch(at)
        with self._transaction(db) as tx:
            controls = self._controls(tx,branch)
            view = self._view(tx,branch,now)
            if enabled:
                if view['status']!='active' or view['source']!='paid': raise ValueError('Автопродление доступно для оплаченного действующего тарифа')
                if consent is not True: raise ValueError('Необходимо отдельное явное согласие на автоматические списания')
                self._accept_offer(tx,branch,actor,offer_version,accept_offer,None,now)
                tx.execute('INSERT INTO billing_consents VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,branch,str(actor),'renewal',offer_version,None,now,json.dumps({'plan':view['plan'],'separate_opt_in':True},ensure_ascii=False)))
            else:
                tx.execute('UPDATE billing_renewal_intents SET status="cancelled" WHERE branch_id=? AND status="payment_unavailable"',(branch,))
                self.cancel_scheduled(branch,actor=actor,at=now,db=tx)
                for row in tx.execute('SELECT id,snapshot FROM billing_orders WHERE branch_id=? AND status="awaiting_payment"',(branch,)).fetchall():
                    if json.loads(row['snapshot']).get('request',{}).get('auto_renew'): tx.execute('UPDATE billing_orders SET status="cancelled" WHERE id=?',(row['id'],))
            self._set_controls(tx,branch,enabled=int(enabled),revision=controls['revision']+1,consented_at=now if enabled else controls['consented_at'],
                cancelled_at=None if enabled else now,offer_version=offer_version if enabled else controls['offer_version'],plan_snapshot=json.dumps(view['plan'],ensure_ascii=False) if enabled else controls['plan_snapshot'],actor=str(actor))
            self._event(tx,'subscription_renewal_enabled' if enabled else 'subscription_renewal_cancelled',branch,view['term_id'],now,actor=actor,offer_version=offer_version if enabled else controls['offer_version'])
            return self._view(tx,branch,now)

    def cancel_scheduled(self, branch_id, *, actor, at=None, db=None):
        branch, now = identifier(branch_id,'филиал'), epoch(at)
        with self._transaction(db) as tx:
            count = tx.execute('UPDATE billing_orders SET status="cancelled" WHERE branch_id=? AND status="awaiting_payment" AND starts>created',(branch,)).rowcount
            if count: self._event(tx,'scheduled_change_cancelled',branch,None,now,count=count,actor=actor)
            return {'cancelled':count}

    def cancel_subscription(self, branch_id, *, actor, immediate=False, confirm_burn=False, at=None, db=None):
        if type(immediate) is not bool or type(confirm_burn) is not bool: raise ValueError('Некорректный режим отключения')
        branch, now = identifier(branch_id,'филиал'), epoch(at)
        with self._transaction(db) as tx:
            if immediate and not confirm_burn: raise ValueError('Подтвердите немедленное отключение и сгорание остатков')
            view = self.set_renewal(branch,False,actor=actor,at=now,db=tx)
            if immediate:
                tx.execute('INSERT OR IGNORE INTO billing_cancelled_terms SELECT branch_id,id,? FROM billing_terms WHERE branch_id=?',(now,branch))
                self._set_controls(tx,branch,access_cancelled=1)
                tx.execute('UPDATE billing_orders SET status="cancelled" WHERE branch_id=? AND status="awaiting_payment"',(branch,))
                for row in tx.execute('SELECT id FROM hardware_commands WHERE branch_id=? AND status="queued"',(branch,)).fetchall(): self.finalize(row['id'],'cancelled','subscription_cancelled',at=now,db=tx)
            self._event(tx,'subscription_cancelled',branch,view['term_id'],now,actor=actor,immediate=immediate,access_until=iso(now) if immediate else view['ends_at'])
            return self._view(tx,branch,now)

    def grant_plan_gift(self, branch_id, plan_id=None, *, actor, mode='extend', reason='', confirm_burn=False, quota=UNSET, sms_quota=UNSET, days=None, at=None, db=None, preview=False):
        if mode not in ('extend','replace'): raise ValueError('Некорректный режим подарка')
        branch, now = identifier(branch_id,'филиал'), epoch(at)
        with self._transaction(db) as tx:
            plan = self.catalog.get('plan',plan_id,db=tx) if plan_id else {'id':None,'name':'Подарочный тариф','kind':'gift','price_kopecks':0,'duration_value':days or 30,'duration_unit':'days','quota':0,'sms_quota':0,'logo':'','revision':1}
            if plan.get('kind')=='trial': raise ValueError('Пробный тариф нельзя повторно предоставить или продлить')
            if quota is not UNSET: plan['quota'] = integer(quota,'Квота звонков',nullable=True)
            if sms_quota is not UNSET: plan['sms_quota'] = integer(sms_quota,'Квота SMS',nullable=True)
            if days is not None: plan.update(duration_value=integer(days,'Срок',minimum=1,maximum=36500),duration_unit='days')
            view = self._view(tx,branch,now)
            start = self._scheduled_end(tx,branch,now) if mode=='extend' else now
            end = self._duration_end(plan,start,view['billing_timezone'])
            burn = bool(mode=='replace' and view['ends_at'] and epoch(view['ends_at'])>now and view['status']!='cancelled')
            result = {'plan':plan,'starts_at':iso(start),'ends_at':iso(end),'quota':plan['quota'],'sms_quota':plan['sms_quota'],'burn_required':burn,'mode':mode,'current_spent':view['spent'],'current_reserved':view['reserved']}
            if preview: return result
            if burn and confirm_burn is not True: raise ValueError('Подтвердите замену действующего тарифа и сгорание остатков')
            if mode=='replace' and self._has_future_term(tx,branch,now): raise ValueError('У филиала уже есть будущий предоставленный тариф; используйте продление')
            key = self._activate_term(tx,branch,plan,start,end,'gift',str(actor),now,str(reason)[:1000],price=0)
            self._event(tx,'gift_granted',branch,key,now,actor=actor,reason=str(reason)[:1000],plan_id=plan_id,quota=plan['quota'],sms_quota=plan['sms_quota'],starts_at=iso(start),ends_at=iso(end))
            return {'gift_id':key,**result}

    def activate_test_tariff(self, branch_id, plan_id, *, actor, idempotency_key, mode='now', confirm_burn=False, at=None, db=None):
        """Internal development operation; the web boundary requires super + dev mode.

        Grants a real quota period explicitly labelled as a test, without a
        payment, offer acceptance or recurring charge authorization.
        """
        if mode not in ('now','period_end'): raise ValueError('Выберите начало сейчас или после текущего периода')
        if type(confirm_burn) is not bool: raise ValueError('Некорректное подтверждение замены тарифа')
        branch, now = identifier(branch_id,'филиал'), epoch(at)
        key = identifier(idempotency_key,'ключ тестового подключения')
        request = {'plan_id':plan_id,'mode':mode,'confirm_burn':confirm_burn,'actor':str(actor)}
        with self._transaction(db) as tx:
            previous = tx.execute('SELECT * FROM billing_test_activations WHERE branch_id=? AND idempotency_key=?',(branch,key)).fetchone()
            if previous:
                if json.loads(previous['request']) != request: raise ValueError('Ключ тестового подключения уже использован для других условий')
                term = tx.execute('SELECT * FROM billing_terms WHERE id=?',(previous['term_id'],)).fetchone()
                return {'term_id':term['id'],'source':'test','duplicate':True,'starts_at':iso(term['starts']),'ends_at':iso(term['ends']),'subscription':self._view(tx,branch,now)}
            plan = self.catalog.get('plan',plan_id,db=tx)
            quote = self._plan_quote(tx,branch,plan,mode,now)
            if quote['burn_required'] and not confirm_burn:
                raise ValueError('Подтвердите сгорание остатка действующего тарифа и пакетов')
            self.set_renewal(branch,False,actor=actor,at=now,db=tx)
            tx.execute('UPDATE billing_orders SET status="cancelled" WHERE branch_id=? AND status="awaiting_payment"',(branch,))
            term_id = self._activate_term(tx,branch,plan,epoch(quote['starts_at']),epoch(quote['ends_at']),'test',str(actor),now,'Тестовое подключение без оплаты',price=0)
            tx.execute('INSERT INTO billing_test_activations VALUES(?,?,?,?,?)',(branch,key,term_id,json.dumps(request,ensure_ascii=False),now))
            self._event(tx,'tariff_test_activated',branch,term_id,now,actor=str(actor),plan_id=plan_id,name=plan['name'],source='test',payment=False,
                starts_at=quote['starts_at'],ends_at=quote['ends_at'],quota=plan['quota'],sms_quota=plan['sms_quota'])
            return {'term_id':term_id,'source':'test','duplicate':False,'starts_at':quote['starts_at'],'ends_at':quote['ends_at'],'subscription':self._view(tx,branch,now)}

    def gift_package(self, branch_id, package_id, *, actor, reason='', at=None, db=None):
        branch, now = identifier(branch_id,'филиал'), epoch(at)
        with self._transaction(db) as tx:
            quote = self.package_quote(branch,package_id,at=now,db=tx)
            view = self._view(tx,branch,now)
            key = self._grant_package(tx,branch,quote['package'],view,now,'gift',str(actor),str(reason)[:1000])
            return {'package_grant_id':key,'subscription':self._view(tx,branch,now)}

    def prepare_renewals(self, *, at=None, db=None):
        now = epoch(at)
        with self._transaction(db) as tx:
            for control in tx.execute('SELECT * FROM billing_renewals WHERE enabled=1 AND access_cancelled=0').fetchall():
                term = self._term(tx,control['branch_id'],now)
                if not term or term['source']!='paid' or term['ends'] is None or term['ends']>now+3*86400: continue
                if tx.execute('SELECT 1 FROM billing_cancelled_terms WHERE term_id=?',(term['id'],)).fetchone(): continue
                if tx.execute('SELECT 1 FROM billing_terms t WHERE branch_id=? AND starts>? AND starts<=? AND ends>? AND NOT EXISTS(SELECT 1 FROM billing_cancelled_terms c WHERE c.term_id=t.id)',(control['branch_id'],now,term['ends'],term['ends'])).fetchone(): continue
                key = hashlib.sha256(json.dumps([control['branch_id'],term['id'],control['revision']]).encode()).hexdigest()
                inserted = tx.execute('INSERT OR IGNORE INTO billing_renewal_intents VALUES(?,?,?,?,?,?,?,?)',(key,control['branch_id'],term['id'],control['revision'],'payment_unavailable',term['ends'],now,control['plan_snapshot'] or '{}')).rowcount
                if inserted: self._event(tx,'subscription_renewal_pending',control['branch_id'],key,now,status='payment_unavailable',due_at=iso(term['ends']))
            return [dict(row) for row in tx.execute('SELECT * FROM billing_renewal_intents WHERE status="payment_unavailable" ORDER BY due')]

    def due_notices(self, *, at=None, db=None):
        with self._transaction(db) as tx:
            self.prepare_renewals(at=at,db=tx)
            return self._legacy_due_notices(at=at,db=tx)

    def history(self, branch_id):
        with self.store.connect() as db:
            result = self._legacy_history(branch_id)
            for row in result['orders']:
                snapshot = json.loads(row['snapshot'])
                row['item_name'] = (snapshot.get('plan') or snapshot.get('package') or {}).get('name',row['plan_id'])
            result['operations'] = [dict(row,payload=json.loads(row['payload'])) for row in db.execute('SELECT * FROM product_events WHERE branch_id=? AND event_type NOT LIKE "gate_%" AND event_type NOT LIKE "command_%" AND event_type NOT LIKE "request_%" ORDER BY happened_at DESC LIMIT 200',(str(branch_id),))]
            return result

    def _record_request_details(self, db, request_id, branch, gate, client):
        phone = name = token = gate_name = None
        if db.execute('SELECT 1 FROM sqlite_master WHERE name="documents"').fetchone():
            from storage.sql_documents import read_document
            links = read_document(db,'links.json')
            for candidate, data in links.items():
                if str(data.get('branch_id'))==branch and (candidate==client or data.get('branch_client_id')==client):
                    name, token = data.get('user_name'), candidate; break
            data = read_document(db,'branches.json').get(branch,{})
            for index, button in enumerate(data.get('buttons',[])):
                stable = button.get('stable_id') or hashlib.sha256((branch+':gate:'+str(index)).encode()).hexdigest()[:24]
                if stable==gate: phone,gate_name=button.get('phone'),button.get('label'); break
        db.execute('INSERT OR IGNORE INTO billing_request_details VALUES(?,?,?,?,?,?)',(request_id,branch,phone,name,token,gate_name))

    def usage_details(self, branch_ids, resource='calls', from_at=None, to_at=None, page=1, page_size=50):
        if resource=='sms': return self.sms.details(branch_ids,from_at,to_at,page=page,page_size=page_size)
        if resource!='calls': raise ValueError('Выберите звонки или SMS')
        if isinstance(branch_ids,(str,int)): branch_ids=[str(branch_ids)]
        if not isinstance(branch_ids,(list,tuple,set)) or len(branch_ids)>1000: raise ValueError('Некорректные филиалы')
        branches=[identifier(value,'филиал') for value in branch_ids]
        integer(page,'Страница',minimum=1,maximum=1000000);integer(page_size,'Размер страницы',minimum=1,maximum=200)
        if not branches: return {'items':[],'total':0,'page':page,'page_size':page_size}
        where='h.branch_id IN ('+','.join('?' for _ in branches)+')';params=list(branches)
        if from_at is not None: where+=' AND h.created>=?';params.append(epoch(from_at))
        if to_at is not None: where+=' AND h.created<?';params.append(epoch(to_at))
        if from_at is not None and to_at is not None and epoch(from_at)>=epoch(to_at): raise ValueError('Начало периода должно быть раньше окончания')
        with self.store.connect() as db:
            total=db.execute('SELECT COUNT(*) FROM hardware_commands h WHERE '+where,params).fetchone()[0]
            rows=db.execute('SELECT h.* FROM hardware_commands h WHERE '+where+' ORDER BY h.created DESC,h.id LIMIT ? OFFSET ?',params+[page_size,(page-1)*page_size]).fetchall()
            items=[]
            for row in rows:
                details=[dict(detail) for detail in db.execute('SELECT d.*,g.source,g.pass_type,g.coalesced FROM gate_requests g LEFT JOIN billing_request_details d ON d.request_id=g.id WHERE g.command_id=? ORDER BY g.happened_at,g.id',(row['id'],))]
                first=details[0] if details else {}
                items.append({**dict(row),'created':iso(row['created']),'started':iso(row['started']),'finished':iso(row['finished']),'at':iso(row['created']),'resource':'calls','quantity':int(bool(row['charged'])),'units':int(bool(row['charged'])),'phone':first.get('phone'),'client_name':first.get('client_name'),'client_token':first.get('client_token'),'gate_name':first.get('gate_name'),'clients':details})
            return {'items':items,'total':total,'page':page,'page_size':page_size}
