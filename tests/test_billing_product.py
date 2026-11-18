"""Offline commercial-domain regressions. No real payments, messages or hardware."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import sqlite3

import pytest

from billing_service import BillingService, epoch, month_at
from analytics_service import AnalyticsService
from storage.product_store import ProductStore

START=datetime(2026,1,31,12,tzinfo=timezone.utc)


@pytest.fixture
def billing(tmp_path):
    service=BillingService(tmp_path/'application.sqlite3')
    service.catalog.publish_offer({'filename':'offer.pdf','url':'/admin/billing-assets/'+'a'*32+'.pdf','sha256':'b'*64},actor='owner',at=START)
    return service


def paid_order(service,plan,**kwargs):
    return service.create_order('12',plan,offer_version=service.catalog.offer()['id'],accept_offer=True,**kwargs)


def gift(service,quota=2,branch='12',days=90):
    return service.grant_gift(branch,quota,actor='owner',days=days,mode='replace',at=START)


def admit(service,request='r1',person='p1',branch='12',gate='g1',at=START,**kw):
    return service.admit(request,branch,person,gate,at=at,**kw)


def test_two_calendar_months_trial_is_unlimited_and_not_reissued(billing):
    trial=billing.activate_trial('12','Europe/Moscow',at=START)
    assert trial['ends_at'].startswith('2026-03-31T12:00:00')
    assert trial['unlimited'] and trial['quota'] is None
    for i in range(110):
        result=admit(billing,str(i),str(i),gate=str(i),coalesce=False)
        assert result['admitted']
        billing.finalize(result['command_id'],'started',at=START)
        billing.finalize(result['command_id'],'command_sent',at=START)
    assert billing.subscription_view('12',at=START)['spent']==110
    assert billing.activate_trial('12',at=START+timedelta(days=40))['ends_at']==trial['ends_at']
    assert not admit(billing,'late','late',at=datetime(2026,3,31,12,tzinfo=timezone.utc))['admitted']


def test_legacy_branches_are_not_suddenly_blocked(billing):
    view=billing.subscription_view('12',at=START)
    assert view['source']=='legacy' and view['unlimited'] and view['available']


def test_last_quota_is_atomic_and_coalesced_commands_pay_once(billing):
    gift(billing,1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows=list(pool.map(lambda i:admit(billing,str(i),str(i),gate=str(i),coalesce=False),range(8)))
    assert sum(r['admitted'] for r in rows)==1
    accepted=next(r for r in rows if r['admitted'])
    with billing.store.connect() as db:command=db.execute('SELECT gate_id FROM hardware_commands WHERE id=?',(accepted['command_id'],)).fetchone()
    coalesced=admit(billing,'other','other',gate=command['gate_id'])
    assert coalesced['admitted'] and coalesced['coalesced'] and coalesced['command_id']==accepted['command_id']
    assert billing.authorize_command(accepted['command_id'],at=START)['authorized']
    billing.finalize(accepted['command_id'],'started',at=START)
    billing.finalize(accepted['command_id'],'command_sent',at=START)
    view=billing.subscription_view('12',at=START)
    assert view['spent']==1 and view['reserved']==0


def test_person_cooldown_shared_across_channels_and_repeated_id_is_not_charged(billing):
    gift(billing)
    first=admit(billing)
    duplicate=admit(billing)
    assert duplicate['duplicate'] and duplicate['command_id']==first['command_id']
    assert admit(billing,'r2',source='telegram')['reason']=='cooldown'
    assert billing.cooldown_remaining('12','p1',at=START+timedelta(seconds=5))==15
    billing.finalize(first['command_id'],'failed','port_unavailable',at=START)
    assert billing.cooldown_remaining('12','p1',at=START)==0
    assert billing.subscription_view('12',at=START)['reserved']==0


def test_coalescing_never_crosses_branch_gate_or_period(billing):
    gift(billing,10);gift(billing,10,branch='123')
    a=admit(billing);b=admit(billing,'b','b',branch='123');c=admit(billing,'c','c',gate='g2')
    assert len({a['command_id'],b['command_id'],c['command_id']})==3


def test_started_unknown_is_not_refunded_and_recover_does_not_replay(billing):
    gift(billing)
    first=admit(billing);second=admit(billing,'r2','p2',gate='g2')
    billing.finalize(first['command_id'],'started',at=START)
    billing.recover(at=START+timedelta(seconds=5))
    view=billing.subscription_view('12',at=START)
    assert view['spent']==1 and view['reserved']==0
    with billing.store.connect() as db:
        states={r['id']:r['status'] for r in db.execute('SELECT id,status FROM hardware_commands')}
    assert states[first['command_id']]=='unknown' and states[second['command_id']]=='cancelled'


def test_own_reservation_survives_month_boundary_without_moving_charge(billing):
    gift(billing,1)
    at=datetime(2026,2,28,11,59,50,tzinfo=timezone.utc)
    first=admit(billing,at=at)
    period=billing.subscription_view('12',at=at)['period_id']
    after=at+timedelta(seconds=20)
    assert billing.authorize_command(first['command_id'],at=after)['authorized']
    billing.finalize(first['command_id'],'started',at=after)
    assert billing.subscription_view('12',at=after)['spent']==0
    with billing.store.connect() as db:assert billing._usage(db,period)['spent']==1


def test_cancel_one_coalesced_request_keeps_other_and_no_empty_command(billing):
    gift(billing,1)
    a=admit(billing);b=admit(billing,'b','b')
    billing.cancel_request('r1',at=START)
    assert billing.authorize_command(a['command_id'],at=START)['authorized']
    billing.cancel_request('b',at=START)
    assert not billing.authorize_command(a['command_id'],at=START)['authorized']
    assert billing.subscription_view('12',at=START)['remaining']==1


def test_gift_does_not_clear_usage_or_automatically_resume_suspension(billing):
    gift(billing,3)
    result=admit(billing)
    billing.finalize(result['command_id'],'started',at=START)
    reduced=gift(billing,0)
    assert reduced['current_period_quota']==1
    billing.suspend('12',True,actor='owner',at=START)
    gift(billing,5)
    view=billing.subscription_view('12',at=START)
    assert view['spent']==1 and view['quota']==5 and view['status']=='suspended'
    view=billing.add_quota('12',10,actor='owner',reason='Подарок',at=START)
    assert view['quota']==15 and view['spent']==1


def test_payment_stub_and_order_idempotency_never_activate_paid_access(billing):
    billing.activate_trial('12',at=START)
    first=paid_order(billing,'pro',actor='admin',idempotency_key='abc',at=START)
    second=paid_order(billing,'pro',actor='admin',idempotency_key='abc',at=START)
    assert first['id']==second['id'] and first['amount_kopecks']==350000
    assert billing.checkout(first['id'],branch_id='12',at=START)['status']=='payment_unavailable'
    assert billing.subscription_view('12',at=START)['status']=='trial'
    assert not billing.history('12')['payments']


def test_immutable_ledger_and_domain_events(billing):
    gift(billing);admit(billing)
    for table in ('quota_ledger','product_events'):
        with pytest.raises(sqlite3.IntegrityError):
            with billing.store.connect() as db:db.execute('DELETE FROM '+table)


def test_nested_shared_database_transaction_rolls_back_identity_and_quota(tmp_path):
    path=tmp_path/'application.sqlite3';store=ProductStore(path)
    with store.connect() as db:db.execute('CREATE TABLE identity_fixture(id TEXT)')
    with pytest.raises(RuntimeError):
        with store.connect() as db:
            db.execute('INSERT INTO identity_fixture VALUES("x")')
            service=BillingService(path)
            admit(service)
            raise RuntimeError('rollback')
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM identity_fixture').fetchone()[0]==0
        assert not db.execute('SELECT name FROM sqlite_master WHERE name="gate_requests"').fetchone()


def test_analytics_filters_scope_and_zero_intervals(billing):
    analytics=AnalyticsService(billing.store)
    a=admit(billing,source='telegram',pass_type='guest');billing.finalize(a['command_id'],'command_sent',at=START)
    admit(billing,'b','p2',source='web',pass_type='permanent',gate='g2')
    admit(billing,'c','p3',branch='123')
    analytics.record_client('c1','12','p1',created_at=START)
    analytics.record_client('c1','12','p1',created_at=START+timedelta(days=1))
    query={'from':START.isoformat(),'to':(START+timedelta(days=3)).isoformat(),'group_by':['day']}
    result=analytics.query(query,allowed_branches=['12'])
    assert result['summary']['requests']==2 and result['summary']['new_branch_clients']==1
    assert result['summary']['average_per_day']==round(2/3,6)
    assert len(result['groups'])==4 and result['groups'][1]['requests']==0
    with pytest.raises(PermissionError):analytics.query({**query,'branches':['123']},allowed_branches=['12'])
    assert analytics.query({**query,'pass_types':['guest']},allowed_branches=['12'])['summary']['requests']==1
    assert analytics.records(query,allowed_branches=['12'])['total']==2


def test_calendar_month_anchor_does_not_drift_after_february():
    assert datetime.fromtimestamp(month_at(epoch(START),1,'Europe/Moscow'),timezone.utc).day==28
    assert datetime.fromtimestamp(month_at(epoch(START),2,'Europe/Moscow'),timezone.utc).day==31


def test_future_gifts_append_and_prevent_false_expiry_notices(billing):
    initial=billing.activate_trial('12',at=START)
    near=datetime.fromisoformat(initial['ends_at'])-timedelta(days=7)
    assert billing.due_notices(at=near)[0]['event_type']=='subscription_expiring'
    first=billing.grant_gift('12',100,actor='owner',days=30,at=near)
    second=billing.grant_gift('12',500,actor='owner',days=30,at=near)
    assert second['starts_at']==first['ends_at']
    assert billing.due_notices(at=near)==[]
    assert billing.subscription_view('12',at=first['starts_at'])['quota']==100
    assert billing.subscription_view('12',at=second['starts_at'])['quota']==500


def test_verified_payment_hook_is_atomic_and_switch_preserves_historical_usage(billing):
    order=paid_order(billing,'start',actor='admin',idempotency_key='paid-start',at=START)
    with pytest.raises(ValueError):billing.apply_payment(order['id'],'provider1',49999,at=START)
    assert not billing.history('12')['payments']
    assert billing.apply_payment(order['id'],'provider1',50000,at=START)['applied']
    assert billing.apply_payment(order['id'],'provider1',50000,at=START)['duplicate']
    first=admit(billing);billing.finalize(first['command_id'],'command_sent',at=START)
    before=billing.subscription_view('12',at=START)
    upgrade=paid_order(billing,'business',actor='admin',idempotency_key='paid-upgrade',mode='now',confirm_burn=True,at=START)
    assert upgrade['kind']=='tariff' and upgrade['amount_kopecks']==100000
    billing.apply_payment(upgrade['id'],'provider2',100000,at=START)
    after=billing.subscription_view('12',at=START)
    assert after['spent']==0 and after['quota']==500 and after['period_id']!=before['period_id']
    with billing.store.connect() as db:assert billing._usage(db,before['period_id'])['spent']==1
    with pytest.raises(ValueError):billing.apply_payment(upgrade['id'],'provider2',50000,at=START)
    for table in ('billing_payments',):
        with pytest.raises(sqlite3.IntegrityError):
            with billing.store.connect() as db:db.execute('DELETE FROM '+table)


def test_bad_request_identity_and_stale_order_are_rejected(billing):
    with pytest.raises(ValueError):billing.create_order('12','start',actor='admin',idempotency_key=None,at=START)
    with pytest.raises(ValueError):admit(billing,request=None)
    order=paid_order(billing,'start',actor='admin',idempotency_key='start',at=START)
    with pytest.raises(ValueError):billing.apply_payment(order['id'],'late',50000,at=START+timedelta(hours=2))
    assert not billing.history('12')['payments']


def test_analytics_scoped_coverage_and_dst_bucket_boundaries(billing):
    analytics=AnalyticsService(billing.store)
    admit(billing,'outside','outside',branch='123',at=START-timedelta(days=100))
    admit(billing)
    query={'from':'2026-10-25T00:30:00+02:00','to':'2026-10-25T03:30:00+01:00','group_by':['hour'],'timezone':'Europe/Berlin'}
    result=analytics.query(query,allowed_branches=['12'])
    assert result['coverage_from']==START.isoformat()
    assert len(result['groups'])==5
    labels=[r['dimensions']['hour'] for r in result['groups']]
    assert '2026-10-25T02:00:00+02:00' in labels and '2026-10-25T02:00:00+01:00' in labels
    with pytest.raises(ValueError):analytics.query({**query,'timezone':'Not/AZone'},allowed_branches=['12'])
    with pytest.raises(ValueError):analytics.query({**query,'group_by':[{}]},allowed_branches=['12'])


def test_deny_audit_does_not_create_account_or_charge(billing):
    assert billing.record_denied('deny','12','person','gate','pass_expired',at=START)
    assert not billing.record_denied('deny','12','person','gate','pass_expired',at=START)
    with billing.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM billing_accounts').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM quota_ledger').fetchone()[0]==0


def test_gift_preview_does_not_grant_and_paid_shortening_needs_confirmation(billing):
    order=paid_order(billing,'start',actor='a',idempotency_key='purchase',at=START)
    billing.apply_payment(order['id'],'payment',50000,at=START)
    preview=billing.grant_gift('12',500,actor='owner',days=1,mode='replace',preview=True,at=START)
    assert preview['shortens_paid_term'] and billing.subscription_view('12',at=START)['source']=='paid'
    with pytest.raises(ValueError):billing.grant_gift('12',500,actor='owner',days=1,mode='replace',at=START)
    result=billing.grant_gift('12',500,actor='owner',days=1,mode='replace',confirm_shorten=True,at=START)
    assert result['ends_at']==preview['ends_at']


def test_analytics_previous_equal_period_comparison(billing):
    admit(billing,'earlier','earlier',at=START-timedelta(days=2))
    admit(billing,'now','now',at=START)
    admit(billing,'more','more',at=START)
    result=AnalyticsService(billing.store).query({'from':START.isoformat(),'to':(START+timedelta(days=2)).isoformat(),'compare_previous':True},allowed_branches=['12'])
    assert result['comparison']['summary']['requests']==1
    assert result['comparison']['changes']['requests']==1


def test_timezone_change_freezes_current_period_and_preserves_original_day(billing):
    gift(billing,10)
    first=admit(billing);billing.finalize(first['command_id'],'command_sent',at=START)
    before=billing.subscription_view('12',at=START)
    changed=billing.schedule_timezone('12','America/New_York',at=START+timedelta(days=5))
    now=billing.subscription_view('12',at=START+timedelta(days=5))
    assert now['period_id']==before['period_id'] and now['period_ends_at']==before['period_ends_at']
    assert now['spent']==1 and now['quota']==10 and now['billing_timezone']=='Europe/Moscow'
    assert changed['effective_at']==before['period_ends_at']
    after=billing.subscription_view('12',at=changed['effective_at'])
    assert after['spent']==0 and after['billing_timezone']=='America/New_York'
    assert after['period_starts_at']==before['period_ends_at']
    assert after['period_ends_at'].startswith('2026-03-31T19:00:00')
    assert billing.subscription_view('12',at=after['period_ends_at'])['period_ends_at'].startswith('2026-04-30T19:00:00')


def test_pending_timezone_change_can_be_cancelled_without_new_period(billing):
    gift(billing)
    before=billing.subscription_view('12',at=START)
    billing.schedule_timezone('12','Asia/Vladivostok',at=START)
    billing.schedule_timezone('12','Europe/Moscow',at=START+timedelta(days=1))
    after=billing.subscription_view('12',at=START+timedelta(days=1))
    assert after['period_id']==before['period_id'] and after['timezone_change'] is None
