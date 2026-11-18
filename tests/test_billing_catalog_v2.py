"""Focused catalog, full-term balances and revocable consent regressions."""
from datetime import datetime, timedelta, timezone
import json

import pytest

from billing_service import BillingService

AT = datetime(2026, 1, 31, 12, tzinfo=timezone.utc)


@pytest.fixture
def service(tmp_path):
    billing = BillingService(tmp_path/'application.sqlite3')
    billing.catalog.publish_offer({'filename':'offer.pdf','url':'/admin/billing-assets/'+'a'*32+'.pdf','sha256':'b'*64},actor='super',at=AT)
    return billing


def order(service, plan='start', key='order', *, at=AT, **options):
    return service.create_order('12',plan,actor='admin',idempotency_key=key,
        offer_version=service.catalog.offer()['id'],accept_offer=True,at=at,**options)


def pay(service, item, *, at=AT):
    return service.apply_payment(item['id'],'payment-'+item['id'],item['amount_kopecks'],at=at)


def package(service, resource='calls', quantity=5):
    return service.catalog.save('package',{'name':'Дополнение','resource':resource,'quantity':quantity,
        'price_kopecks':10000,'logo':'','active':True},actor='super')


def test_catalog_revision_archive_and_trial_rules(service):
    original = service.catalog.get('plan','start')
    updated = service.catalog.save('plan',{**original,'name':'Новый старт','sms_quota':10},actor='super')
    assert updated['revision']==original['revision']+1
    with pytest.raises(ValueError): service.catalog.save('plan',original,actor='super')
    archived = service.catalog.save('plan',{**updated,'active':False},actor='super')
    assert archived not in service.catalog.list()['plans']
    assert archived in service.catalog.list(True)['plans']
    with pytest.raises(ValueError): order(service)
    with pytest.raises(ValueError): order(service,'trial')
    with pytest.raises(ValueError): service.catalog.save('plan',{**service.catalog.get('plan','trial'),'price_kopecks':1},actor='super')


def test_trial_uses_catalog_snapshot_once_and_whole_duration(service):
    trial = service.catalog.get('plan','trial')
    service.catalog.save('plan',{**trial,'duration_value':45,'duration_unit':'days','quota':5,'sms_quota':9},actor='super')
    first = service.activate_trial('12',at=AT)
    assert first['plan_id']=='trial' and first['quota']==5 and first['sms']['quota']==9
    assert datetime.fromisoformat(first['ends_at'])==AT+timedelta(days=45)
    command = service.admit('r','12','p','g',at=AT)
    service.finalize(command['command_id'],'command_sent',at=AT)
    later = service.subscription_view('12',at=AT+timedelta(days=32))
    assert later['period_id']==first['period_id'] and later['spent']==1
    assert service.activate_trial('12',at=AT+timedelta(days=10))['ends_at']==first['ends_at']
    trial = service.catalog.get('plan','trial')
    service.catalog.save('plan',{**trial,'quota':100},actor='super')
    assert service.subscription_view('12',at=AT)['quota']==5


def test_offer_consent_and_stub_do_not_grant_or_enable_renewal(service):
    with pytest.raises(ValueError,match='оферт'):
        service.create_order('12','start',actor='admin',idempotency_key='no-consent',at=AT)
    item = order(service)
    assert service.checkout(item['id'],branch_id='12',at=AT)['status']=='payment_unavailable'
    assert service.subscription_view('12',at=AT)['source']=='legacy'
    assert not service.subscription_view('12',at=AT)['renewal']['enabled']
    assert item['id']==order(service)['id']
    with pytest.raises(ValueError): order(service,plan='business')
    with service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM billing_consents WHERE kind="offer"').fetchone()[0]==1


def test_paid_term_snapshot_immediate_switch_burns_balance_without_moving_ledger(service):
    plan = service.catalog.get('plan','start')
    service.catalog.save('plan',{**plan,'duration_value':2,'sms_quota':10},actor='super')
    initial = order(service);pay(service,initial)
    before = service.subscription_view('12',at=AT)
    command = service.admit('r','12','p','g',at=AT)
    service.finalize(command['command_id'],'command_sent',at=AT)
    assert service.subscription_view('12',at=AT+timedelta(days=32))['spent']==1
    with pytest.raises(ValueError,match='сгорание'): order(service,'business','switch',mode='now')
    replacement = order(service,'business','switch',mode='now',confirm_burn=True)
    assert replacement['amount_kopecks']==100000
    pay(service,replacement)
    after = service.subscription_view('12',at=AT)
    assert after['period_id']!=before['period_id'] and after['spent']==0 and after['quota']==500
    with service.store.connect() as db:
        assert service._usage(db,before['period_id'])['spent']==1
    assert pay(service,replacement)['duplicate']


@pytest.mark.parametrize('resource', ['calls','sms'])
def test_packages_need_active_tariff_and_expire_with_its_term(service, resource):
    extra = package(service,resource)
    with pytest.raises(ValueError): service.package_quote('12',extra['id'],at=AT)
    plan = service.catalog.get('plan','start')
    service.catalog.save('plan',{**plan,'sms_quota':10},actor='super')
    pay(service,order(service))
    before = service.subscription_view('12',at=AT)
    purchase = service.create_package_order('12',extra['id'],actor='admin',idempotency_key='extra',
        offer_version=service.catalog.offer()['id'],accept_offer=False,at=AT)
    pay(service,purchase)
    active = service.subscription_view('12',at=AT)
    assert active[resource]['quota']==before[resource]['quota']+5
    assert active['packages'][0]['ends_at']==before['ends_at']
    assert pay(service,purchase)['duplicate']
    renewal = order(service,key='next',mode='period_end');pay(service,renewal)
    next_view = service.subscription_view('12',at=before['ends_at'])
    assert next_view[resource]['credits']==0 and not next_view['packages']


def test_planned_paid_change_waits_for_period_end_and_preserves_existing_usage(service):
    pay(service,order(service))
    before=service.subscription_view('12',at=AT)
    pending=order(service,'business','planned',mode='period_end')
    assert service.subscription_view('12',at=AT)['scheduled_change']['id']==pending['id']
    assert service.subscription_view('12',at=AT)['plan_id']=='start'
    pay(service,pending)
    assert service.subscription_view('12',at=AT)['period_id']==before['period_id']
    assert service.subscription_view('12',at=before['ends_at'])['plan_id']=='business'


def test_cancel_revokes_renewal_and_blocks_late_callback_until_fresh_order(service):
    pay(service,order(service,auto_renew=True))
    before=service.subscription_view('12',at=AT)
    assert before['renewal']['enabled']
    pending=order(service,'business','next',mode='period_end',auto_renew=True)
    after=service.cancel_subscription('12',actor='admin',at=AT)
    assert not after['renewal']['enabled'] and after['available'] and after['ends_at']==before['ends_at']
    with pytest.raises(ValueError): pay(service,pending)
    with pytest.raises(ValueError): service.set_renewal('12',True,actor='admin',at=AT)
    enabled=service.set_renewal('12',True,actor='admin',consent=True,offer_version=service.catalog.offer()['id'],at=AT)
    assert enabled['renewal']['enabled']
    with pytest.raises(ValueError): service.cancel_subscription('12',actor='admin',immediate=True,at=AT)
    cancelled=service.cancel_subscription('12',actor='admin',immediate=True,confirm_burn=True,at=AT)
    assert cancelled['status']=='cancelled' and not cancelled['available']
    assert not service.admit('after','12','p','g',at=AT)['admitted']


def test_renewal_intents_are_durable_idempotent_and_never_simulate_payment(service):
    pay(service,order(service,auto_renew=True))
    view=service.subscription_view('12',at=AT)
    due=datetime.fromisoformat(view['ends_at'])-timedelta(days=3)
    assert len(service.prepare_renewals(at=due))==1
    assert len(service.prepare_renewals(at=due))==1
    assert len(service.history('12')['payments'])==1
    assert service.subscription_view('12',at=view['ends_at'])['status']=='expired'
    service.set_renewal('12',False,actor='admin',at=due)
    assert service.prepare_renewals(at=due)==[]


def test_gift_uses_plan_snapshot_and_package_gift_has_no_payment(service):
    gift=service.grant_plan_gift('12','start',actor='super',mode='replace',quota=7,sms_quota=12,days=60,at=AT)
    assert not service.history('12')['payments']
    view=service.subscription_view('12',at=AT)
    assert view['source']=='gift' and view['quota']==7 and view['sms']['quota']==12
    assert view['ends_at']==gift['ends_at']
    extra=package(service)
    service.gift_package('12',extra['id'],actor='super',at=AT)
    assert service.subscription_view('12',at=AT)['calls']['credits']==5
    assert service.subscription_view('12',at=AT+timedelta(days=32))['period_id']==view['period_id']
    with pytest.raises(ValueError): service.grant_plan_gift('12','trial',actor='super',at=AT)


def test_legacy_monthly_gift_keeps_its_boundaries_and_rejects_new_packages(service):
    service.grant_gift('12',10,actor='super',days=90,mode='replace',at=AT)
    before=service.subscription_view('12',at=AT)
    extra=package(service)
    with pytest.raises(ValueError,match='нового каталога'): service.package_quote('12',extra['id'],at=AT)
    after=service.subscription_view('12',at=AT)
    assert before['period_ends_at']==after['period_ends_at'] and before['quota']==after['quota']==10
    assert not after['packages_available']


def test_immediate_plan_switch_releases_only_queued_old_reservations(service):
    pay(service,order(service))
    queued=service.admit('queued','12','p','g',at=AT)
    old=service.subscription_view('12',at=AT)
    assert old['reserved']==1
    pay(service,order(service,'business','replace',mode='now',confirm_burn=True))
    assert not service.authorize_command(queued['command_id'],at=AT)['authorized']
    with service.store.connect() as db:
        assert service._usage(db,old['period_id'])['reserved']==0
        assert service._usage(db,old['period_id'])['spent']==0
    assert service.subscription_view('12',at=AT)['spent']==0


def test_call_detail_keeps_destination_and_client_snapshot_when_config_changes(service, monkeypatch):
    documents={'branches.json':{'12':{'buttons':[{'stable_id':'gate-stable-id','phone':'+15550000001','label':'Север'}]}},
               'links.json':{'test-token':{'branch_id':12,'branch_client_id':'client-1','user_name':'Synthetic client'}}}
    with service.store.connect() as db: db.execute('CREATE TABLE documents(dummy TEXT)')
    monkeypatch.setattr('storage.sql_documents.read_document',lambda db,name:documents[name])
    command=service.admit('r','12','person-1','gate-stable-id',branch_client_id='client-1',at=AT)
    service.finalize(command['command_id'],'command_sent',at=AT)
    documents['branches.json']['12']['buttons'][0]['phone']='+15550000002'
    documents['links.json']['test-token']['user_name']='Renamed'
    details=service.usage_details(['12'],from_at=AT,to_at=AT+timedelta(days=1))
    assert details['total']==1
    assert details['items'][0]['phone']=='+15550000001'
    assert details['items'][0]['client_name']=='Synthetic client'
    assert details['items'][0]['quantity']==1
    assert service.usage_details(['123'])['total']==0


def test_sms_balance_is_independent_of_calls_and_has_its_own_warning(service):
    service.grant_plan_gift('12','start',actor='super',mode='replace',quota=0,sms_quota=10,at=AT)
    assert not service.subscription_view('12',at=AT)['available']
    assert service.sms.reserve_sms('12','sms-1',8,phone='+15550000001',at=AT)['allowed']
    service.sms.finish_sms('sms-1','network_accepted',at=AT)
    view=service.subscription_view('12',at=AT)
    assert view['sms']['remaining']==2 and view['sms']['spent']==8 and view['spent']==0
    notices=service.due_notices(at=AT)
    assert any(row['event_type']=='quota_warning' and row['payload'].get('resource')=='sms' for row in notices)


@pytest.mark.parametrize('reconnect', ['paid','gift','test'])
def test_reconnection_ignores_cancelled_current_and_future_terms_without_legacy_fallback(service, reconnect):
    pay(service,order(service))
    old=service.subscription_view('12',at=AT)
    pay(service,order(service,'business','planned-before-cancel',mode='period_end'))
    cancelled_at=AT+timedelta(days=1)
    cancelled=service.cancel_subscription('12',actor='admin',immediate=True,confirm_burn=True,at=cancelled_at)
    assert cancelled['status']=='cancelled' and not cancelled['has_current_tariff'] and cancelled['current_plan'] is None
    assert not cancelled['scheduled_terms']
    reconnect_at=AT+timedelta(days=2)
    if reconnect=='paid':
        pay(service,order(service,'pro','reconnect',mode='period_end',at=reconnect_at),at=reconnect_at)
    elif reconnect=='gift':
        service.grant_plan_gift('12','pro',actor='super',mode='extend',at=reconnect_at)
    else:
        service.activate_test_tariff('12','pro',actor='super',idempotency_key='reconnect',mode='period_end',at=reconnect_at)
    fresh=service.subscription_view('12',at=reconnect_at)
    assert fresh['has_current_tariff'] and fresh['current_plan']['id']=='pro'
    assert datetime.fromisoformat(fresh['period_starts_at'])==reconnect_at
    service=BillingService(service.store)
    assert service.subscription_view('12',at=cancelled_at)['status']=='cancelled'
    # The revoked future Business plan must not supersede the reconnection.
    restored=service.subscription_view('12',at=old['ends_at'])
    assert restored['available'] and restored['status']=='active' and restored['term_id']==fresh['term_id']
    assert service.subscription_view('12',at=fresh['ends_at'])['status']=='expired'


def test_catalog_visibility_price_order_archive_restore_and_existing_subscriber(service):
    original=service.catalog.get('plan','start')
    # An older saved catalog without visibility remains compatible.
    with service.store.connect() as db:
        payload={key:value for key,value in original.items() if key!='visible'}
        db.execute('UPDATE billing_catalog SET payload=? WHERE kind="plan" AND id="start"',(json.dumps(payload),))
    assert service.catalog.get('plan','start')['visible']
    plans=service.catalog.list()['plans']
    assert [plan['price_kopecks'] for plan in plans]==sorted(plan['price_kopecks'] for plan in plans)
    assert 'trial' not in {plan['id'] for plan in plans}
    hidden=service.catalog.save('plan',{**original,'visible':False},actor='super')
    assert hidden not in service.catalog.list()['plans']
    assert hidden in service.catalog.list(include_hidden=True)['plans']
    with pytest.raises(ValueError,match='самостоятельного'): service.quote('12','start',at=AT)
    with pytest.raises(ValueError,match='самостоятельного'): order(service)
    service.grant_plan_gift('12','start',actor='super',mode='replace',at=AT)
    assert service.subscription_view('12',at=AT)['current_plan']['id']=='start'
    archived=service.catalog.save('plan',{**hidden,'active':False},actor='super')
    with pytest.raises(ValueError,match='архив'): service.grant_plan_gift('other','start',actor='super',at=AT)
    restored=service.catalog.save('plan',{**archived,'active':True},actor='super')
    assert restored['visible'] is False and restored['active']
    assert service.subscription_view('12',at=AT)['quota']==100
    trial=service.catalog.get('plan','trial')
    visible_trial=service.catalog.save('plan',{**trial,'visible':True},actor='super')
    assert service.catalog.list()['plans'][0]==visible_trial
    with pytest.raises(ValueError,match='нельзя оплатить'): order(service,'trial')


def test_test_activation_is_idempotent_quota_grant_without_financial_records(service):
    plan=service.catalog.get('plan','start')
    service.catalog.save('plan',{**plan,'visible':False,'quota':2,'sms_quota':3},actor='super')
    first=service.activate_test_tariff('any-branch','start',actor='super',idempotency_key='first',at=AT)
    view=first['subscription']
    assert first['source']=='test' and view['source']=='test' and view['quota']==2 and view['sms']['quota']==3
    assert not view['renewal']['enabled'] and view['accepted_offer'] is None
    command=service.admit('test-call','any-branch','client','gate',at=AT)
    service.finalize(command['command_id'],'command_sent',at=AT)
    assert service.subscription_view('any-branch',at=AT)['remaining']==1
    assert service.sms.reserve_sms('any-branch','test-sms',2,phone='+15550000001',at=AT)['allowed']
    service.sms.finish_sms('test-sms','network_accepted',at=AT)
    assert service.subscription_view('any-branch',at=AT)['sms']['remaining']==1
    repeated=service.activate_test_tariff('any-branch','start',actor='super',idempotency_key='first',at=AT)
    assert repeated['duplicate'] and repeated['term_id']==first['term_id'] and repeated['subscription']['spent']==1
    with pytest.raises(ValueError,match='уже использован'):
        service.activate_test_tariff('any-branch','business',actor='super',idempotency_key='first',at=AT)
    with pytest.raises(ValueError,match='сгорание'):
        service.activate_test_tariff('any-branch','business',actor='super',idempotency_key='replace',at=AT)
    future=service.activate_test_tariff('any-branch','business',actor='super',idempotency_key='future',mode='period_end',at=AT)
    assert future['starts_at']==first['ends_at'] and future['subscription']['term_id']==first['term_id']
    assert service.subscription_view('any-branch',at=future['starts_at'])['plan_id']=='business'
    history=service.history('any-branch')
    assert not history['orders'] and not history['payments']
    assert sum(row['event_type']=='tariff_test_activated' for row in history['operations'])==2
    with service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM billing_consents').fetchone()[0]==0
