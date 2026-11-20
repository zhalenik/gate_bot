"""Paid SMS parts and branch opt-in: fake modem, durable SQL, no external effects."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from billing_service import BillingService
from storage import config
from storage.sql_documents import read_document, write_document
from test_identity_product import identities
from test_product_integration import product
from test_web_security import web, login, post


AT=datetime(2026,1,31,12,tzinfo=timezone.utc)
PHONE='+15550100101'


@pytest.fixture
def billing(tmp_path):
    service=BillingService(tmp_path/'application.sqlite3')
    service.grant_plan_gift('12',actor='super',quota=0,sms_quota=4,days=60,mode='replace',confirm_burn=True,at=AT)
    return service


def reserve(service,key='message',parts=1,**kwargs):
    return service.sms.reserve_sms('12',key,parts,phone=PHONE,at=AT,**kwargs)


def test_sms_has_own_balance_and_last_parts_are_reserved_atomically(billing):
    assert not billing.subscription_view('12',at=AT)['available']  # Calls exhausted, SMS still allowed.
    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes=list(pool.map(lambda n:reserve(billing,str(n),2),range(6)))
    assert sum(row['allowed'] for row in outcomes)==2
    view=billing.subscription_view('12',at=AT)
    assert view['sms']['remaining']==0 and view['sms']['reserved']==4
    accepted=next(row for row in outcomes if row['allowed'])
    repeated=reserve(billing,accepted['message_id'],2)
    assert repeated['duplicate'] and repeated['allowed']
    assert billing.subscription_view('12',at=AT)['sms']['reserved']==4


@pytest.mark.parametrize('status,parts,charged',[
    ('network_accepted',None,3),('failed',None,0),('cancelled',None,0),('simulated',None,0),
    ('unknown',None,3),
    ('partial',[{'sequence':1,'status':'network_accepted'},{'sequence':2,'status':'unknown'},{'sequence':3,'status':'pending'}],2),
    ('partial',[{'sequence':1,'status':'network_accepted'},{'sequence':2,'status':'failed'},{'sequence':3,'status':'pending'}],1),
])
def test_terminal_parts_settle_once_and_release_unused_reserve(billing,status,parts,charged):
    assert reserve(billing,parts=3)['allowed']
    assert billing.sms.finish_sms('message',status,segments=parts,at=AT)
    assert not billing.sms.finish_sms('message','network_accepted',at=AT)
    view=billing.subscription_view('12',at=AT)
    assert view['sms']['reserved']==0 and view['sms']['spent']==charged
    assert view['sms']['remaining']==4-charged
    detail=billing.sms.details(['12'])['items'][0]
    assert detail['phone']==PHONE and detail['charged']==charged and detail['parts']==3
    assert billing.sms.details([])['items']==[] and billing.sms.details(['99'])['items']==[]
    with billing.store.connect() as db:
        with pytest.raises(Exception,match='immutable'):
            db.execute('UPDATE sms_quota_ledger SET spent_delta=0')


def test_whole_term_and_packages_expire_without_losing_old_detail(billing):
    reserve(billing,parts=2);billing.sms.finish_sms('message','network_accepted',at=AT)
    before=billing.subscription_view('12',at=AT)
    extra=billing.catalog.save('package',{'name':'SMS extra','resource':'sms','quantity':5,'price_kopecks':10000,'logo':'','active':True},actor='super')
    billing.gift_package('12',extra['id'],actor='super',at=AT)
    assert billing.subscription_view('12',at=AT+timedelta(days=32))['sms']['remaining']==7
    billing.grant_plan_gift('12',actor='super',quota=0,sms_quota=1,days=30,mode='extend',at=AT)
    later=billing.subscription_view('12',at=before['ends_at'])
    assert later['sms']['remaining']==1 and later['sms']['credits']==0
    assert billing.sms.details(['12'])['items'][0]['charged']==2


def test_registration_only_is_exempt_and_restart_uses_durable_segments(product,monkeypatch):
    p=product;out=p.services.notifications
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    queue=SimpleNamespace(dry_run=False,journal=out.telephony,add_manual=Mock(return_value={'status':'success','delivery':'queued','command_id':'synthetic'}))
    monkeypatch.setattr('sim800c._get_queue',lambda:queue)
    registration={'branch_id':'10','purpose':'otp','event':'verification_code','billing_exempt':'registration','idempotency_key':'registration'}
    assert out.send('sms',PHONE,'Registration code',registration)['delivery']=='queued'
    assert not p.services.billing.sms.pending()
    recovery={**registration,'event':'password_reset','idempotency_key':'reset'};recovery.pop('billing_exempt')
    assert out.send('sms',PHONE,'Reset code',recovery)['code']=='sms_quota_exhausted'
    assert queue.add_manual.call_count==1
    p.services.billing.grant_plan_gift('10',actor='super',quota=0,sms_quota=10,days=30,mode='replace',confirm_burn=True)
    message={**recovery,'idempotency_key':'reset-after-gift'}
    response=out.send('sms',PHONE,'Я'*150,message)
    record=out.telephony.snapshot()['records'][response['record_id']]
    assert record['total_parts']==3
    assert p.services.billing.subscription_view('10')['sms']['reserved']==3
    out.telephony.update(record['id'],status='unknown',segments=[{'sequence':1,'status':'network_accepted'},
        {'sequence':2,'status':'submitting'},{'sequence':3,'status':'pending'}])
    out.recover()
    assert p.services.billing.subscription_view('10')['sms']['spent']==2
    assert p.services.billing.subscription_view('10')['sms']['reserved']==0
    assert out.send('sms',PHONE,'Я'*150,message)['status']=='unknown'
    assert queue.add_manual.call_count==2


def test_client_sms_requires_two_fresh_optins_and_survives_branch_disable(identities):
    s=identities;token='example-client'
    assert 'sms' not in s.service.prefs_for_token(token)['allowed_channels']
    with pytest.raises(ValueError,match='SMS'):
        s.service.save_prefs_for_token(token,{'enabled':True,'channels':{'sms':True}})
    client=s.links.load_links()[token]
    with s.product.connect() as db:
        db.execute('INSERT OR REPLACE INTO notification_preferences VALUES(?,"client","11",?)',
                   (client['person_id'],json.dumps({'channels':{'sms':True}})))
        branches=read_document(db,'branches.json');branches['11'].update(sms_notifications_enabled=True,yclients_enabled=False)
        write_document(db,'branches.json',branches)
    prefs=s.service.prefs_for_token(token)
    assert 'sms' in prefs['allowed_channels'] and 'messengers' not in prefs['allowed_channels']
    assert prefs['connected']['sms'] and not prefs['channels']['sms']
    prefs=s.service.save_prefs_for_token(token,{'enabled':True,'channels':{'sms':True}})
    assert prefs['effective_channels']['sms']
    s.branches.update_settings('11',{'sms_notifications_enabled':False})
    assert 'sms' not in s.service.prefs_for_token(token)['channels']
    s.branches.update_settings('11',{'sms_notifications_enabled':True})
    assert s.service.prefs_for_token(token)['effective_channels']['sms']


def test_pending_client_sms_checks_switch_and_success_uses_segment_quota(product,monkeypatch):
    p=product;out=p.services.notifications;identity=p.services.identity
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    p.branches.update_settings('10',{'sms_notifications_enabled':True})
    identity.save_prefs_for_token(p.token,{'enabled':True,'channels':{'sms':True}})
    p.services.billing.grant_plan_gift('10',actor='super',quota=0,sms_quota=10,days=30,mode='replace',confirm_burn=True)
    def accept(kind,phone,**kwargs):
        from sms_submit import encode_submit
        segments=[{'sequence':part['sequence'],'status':'network_accepted'} for part in encode_submit(phone,kwargs['text'])]
        out.telephony.update(kwargs['record_id'],status='network_accepted',segments=segments)
        return {'status':'success','delivery':'queued','command_id':kwargs['command_id']}
    queue=SimpleNamespace(dry_run=False,journal=out.telephony,add_manual=Mock(side_effect=accept))
    monkeypatch.setattr('sim800c._get_queue',lambda:queue)
    payload={**p.links.load_links()[p.token],'token':p.token,'event':'granted'}
    out.enqueue(payload)
    p.branches.update_settings('10',{'sms_notifications_enabled':False})
    out.process_once();queue.add_manual.assert_not_called()
    p.branches.update_settings('10',{'sms_notifications_enabled':True})
    p.links.toggle_user_access(p.token,False);p.links.toggle_user_access(p.token,True)
    out.enqueue({**p.links.load_links()[p.token],'token':p.token,'event':'granted'})
    out.process_once();out.process_once()
    assert queue.add_manual.call_count==1
    assert p.services.billing.subscription_view('10')['sms']['spent']>0
    assert p.services.billing.subscription_view('10')['sms']['reserved']==0
    details=p.services.billing.sms.details(['10'])['items']
    assert len(details)==1 and details[0]['client_token']==p.token and details[0]['client_name']=='Client'


def test_queued_sms_guard_rechecks_cancellation_and_period_before_first_part(billing):
    reserve(billing)
    assert billing.sms.authorize_sms('message',at=AT)
    billing.cancel_subscription('12',actor='owner',immediate=True,confirm_burn=True,at=AT+timedelta(seconds=1))
    assert not billing.sms.authorize_sms('message',at=AT+timedelta(seconds=2))
    billing.sms.finish_sms('message','failed',at=AT+timedelta(seconds=2))
    assert billing.sms.details(['12'])['items'][0]['charged']==0
    billing.grant_plan_gift('12',actor='super',quota=0,sms_quota=4,days=30,mode='replace',confirm_burn=True,at=AT+timedelta(days=1))
    assert billing.sms.reserve_sms('12','next',2,phone=PHONE,at=AT+timedelta(days=1))['allowed']
    billing.grant_plan_gift('12',actor='super',quota=0,sms_quota=4,days=30,mode='replace',confirm_burn=True,at=AT+timedelta(days=2))
    assert not billing.sms.authorize_sms('next',at=AT+timedelta(days=2))


def test_modem_guard_runs_before_submit_and_only_once_for_multipart():
    from sim800c import Sim800Modem, ModemError
    from test_telephony import SMSSerial
    for allowed in (False,True):
        port=SMSSerial();guard_calls=[]
        modem=Sim800Modem('FAKE',serial_factory=lambda **kwargs:port,startup_delay=0)
        def guard():
            guard_calls.append(True)
            if not allowed:raise ModemError('authorization_expired')
        try:
            result=modem.send_sms(PHONE,'Я'*150,progress=lambda parts:None,before_submit=guard)
        finally:modem.close()
        assert len(guard_calls)==1
        assert len(port.payloads)==(3 if allowed else 0)
        assert result['status']==('network_accepted' if allowed else 'failed')


def test_branch_switch_is_scoped_and_manual_guest_delivery_requires_optin(web,tmp_path,monkeypatch):
    from storage.sql_documents import migrate_legacy,DocumentStore
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    services=web.module.product_services()
    send=Mock(return_value={'status':'success','delivery':'queued'})
    monkeypatch.setattr(services.notifications,'send',send)
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    client=web.app.test_client();csrf=login(client)
    assert not client.get('/admin/api/branch-settings').json['settings']['sms_notifications_enabled']
    data={'branch_id':12,'name':'North','enabled':True,'timezone':'Europe/Moscow','buttons':[],
          'sms_notifications_enabled':True}
    assert post(client,csrf,'/admin/api/branch-settings',{**data,'branch_id':123}).status_code==403
    assert not web.branches.get_branch(123).sms_notifications_enabled
    assert post(client,csrf,'/admin/api/branch-settings',data).status_code==200
    response=post(client,csrf,'/admin/api/create-temp-link',{'branch_id':12,'phone':PHONE,'send_link':True,'channel':'sms'})
    assert response.status_code==200 and response.json['delivery']['status']=='sent'
    assert send.call_args.args[0:2]==('sms',PHONE)
    assert send.call_args.args[3]['branch_id']=='12' and send.call_args.args[3]['client_token']==response.json['token']
    web.branches.update_settings(12,{'sms_notifications_enabled':False})
    rejected=post(client,csrf,'/admin/api/send-link',{'token':response.json['token'],'channel':'sms'})
    assert rejected.status_code==503 and send.call_count==1
    assert post(client,csrf,'/admin/api/send-link',{'token':web.token_b,'channel':'sms'}).status_code==404


def test_bot_sms_offer_is_explicit_and_cannot_be_replayed(product,monkeypatch):
    from bot_state import BotStateStore
    from bot_workflows import BotWorkflows
    p=product;flows=BotWorkflows(p.access,BotStateStore(p.root/'sms-bot.sqlite3'))
    p.branches.update_settings('10',{'sms_notifications_enabled':True})
    send=Mock(return_value={'status':'success','delivery':'queued'})
    monkeypatch.setattr(p.services.identity,'_send',send)
    flows.action('telegram','91','x:issue:10')
    confirm=flows.message('telegram','91',PHONE)
    granted=flows.action('telegram','91',confirm['actions'][0]['callback'])
    send.assert_not_called()
    callback=next(row['callback'] for row in granted['actions'] if row['callback'].startswith('x:sms:'))
    assert 'очередь' in flows.action('telegram','91',callback)['text']
    with pytest.raises(ValueError):flows.action('telegram','91',callback)
    assert send.call_count==1 and send.call_args.args[3]['branch_id']=='10'


@pytest.mark.parametrize('change',['expire','revoke','revision','revocation_notice','late_appointment'])
def test_queued_access_sms_rechecks_access_event_and_revision_before_modem(product,monkeypatch,change):
    p=product;out=p.services.notifications
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    p.branches.update_settings('10',{'sms_notifications_enabled':True})
    p.services.identity.save_prefs_for_token(p.token,{'enabled':True,'channels':{'sms':True}})
    p.services.billing.grant_plan_gift('10',actor='super',quota=0,sms_quota=10,days=30,mode='replace',confirm_burn=True)
    if change=='revocation_notice':p.links.toggle_user_access(p.token,False)
    if change=='late_appointment':
        with p.links.store.transaction() as links:
            links[p.token].update(access_source='appointment',appointment_notice_until=time.time()+300)
    queue=SimpleNamespace(dry_run=False,journal=out.telephony,add_manual=Mock(return_value={'status':'success','delivery':'queued'}))
    monkeypatch.setattr('sim800c._get_queue',lambda:queue)
    out.enqueue({**p.links.load_links()[p.token],'token':p.token,'event':'revoked' if change=='revocation_notice' else 'granted'})
    out.process_once()
    if change=='revocation_notice':
        queue.add_manual.assert_not_called()
        assert p.services.billing.subscription_view('10')['sms']['reserved']==0
        return
    guard=queue.add_manual.call_args.kwargs['before_submit']
    assert guard()
    with p.links.store.transaction() as links:
        row=links[p.token]
        if change=='expire':row['access_expires']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        elif change=='revision':row['access_revision']=row.get('access_revision',0)+1
        elif change=='late_appointment':row['appointment_notice_until']=time.time()-1
        else:row.update(gate_access=False,admin_blocked=False)
    assert not guard()
    record_id=queue.add_manual.call_args.kwargs['record_id']
    out.telephony.update(record_id,status='failed',code='authorization_expired')
    out.reconcile_hardware()
    sms=p.services.billing.subscription_view('10')['sms']
    assert sms['spent']==0 and sms['reserved']==0
