"""Offline pass timestamps and explicit Email delivery from client/guest forms."""
from datetime import datetime,timedelta,timezone
import os
from unittest.mock import Mock

import pytest

from branch_manager import BranchManager
from link_manager import LinkManager
from storage import config
from storage.pass_changes import changed_at
from storage.sql_documents import DocumentStore,migrate_legacy
from test_identity_product import identities,accept
from test_web_security import web,login,post
from test_ui_browser import browser_ui,sign_in


AT=datetime(2026,11,18,12,tzinfo=timezone.utc)
PHONE='+15550100444'


@pytest.fixture(params=['legacy','sql'])
def passes(request,tmp_path,monkeypatch):
    clock=[AT]
    monkeypatch.setattr('storage.pass_changes.now',lambda:clock[0])
    monkeypatch.setattr('link_manager.now',lambda *args:clock[0])
    branches=BranchManager(tmp_path/'branches.json');branches.add_branch({'branch_id':11,'name':'Synthetic','buttons':[]})
    links=LinkManager(tmp_path/'links.json')
    if request.param=='sql':migrate_legacy(tmp_path,activate=True)
    return links,clock


def test_pass_timestamp_changes_only_with_rights_and_real_timer_expiry(passes):
    links,clock=passes
    token=links.create_link({'branch_id':11,'phone':PHONE,'user_name':'Synthetic'})
    original=links.load_links()[token]['pass_changed_at'];assert original==AT.isoformat()
    clock[0]+=timedelta(minutes=10)
    links.update_user(token,{'user_name':'Updated','email':'client@example.invalid','id_ycl':'51'})
    links.set_mailing_enabled(token,True);links.toggle_user_access(token,True)
    assert links.load_links()[token]['pass_changed_at']==original
    links.toggle_user_access(token,False)
    blocked=links.load_links()[token]['pass_changed_at'];assert blocked==clock[0].isoformat()
    clock[0]+=timedelta(minutes=10);links.toggle_user_access(token,False)
    assert links.load_links()[token]['pass_changed_at']==blocked
    expiry=clock[0]+timedelta(minutes=15);links.set_expiry_at(token,expiry)
    assert links.load_links()[token]['pass_changed_at']==clock[0].isoformat()
    clock[0]=expiry+timedelta(minutes=5)
    assert links.cleanup_expired_accesses()==1
    assert links.load_links()[token]['pass_changed_at']==expiry.isoformat()
    clock[0]+=timedelta(minutes=5);assert links.cleanup_expired_accesses()==0
    assert links.load_links()[token]['pass_changed_at']==expiry.isoformat()


def test_crm_access_changes_have_timestamp_but_repeated_snapshot_does_not(passes):
    links,clock=passes
    token=links.create_link({'branch_id':11,'phone':PHONE,'gate_access':False,'access_source':'appointment'})
    clock[0]+=timedelta(minutes=10)
    eligible={'51':{'phone':PHONE,'name':'Synthetic'}}
    assert links.reconcile_appointments(11,eligible,at=clock[0])['changed']==1
    granted=links.load_links()[token]['pass_changed_at'];assert granted==clock[0].isoformat()
    clock[0]+=timedelta(minutes=10)
    assert links.reconcile_appointments(11,{'51':{'phone':PHONE,'name':'Updated','email':'crm@example.invalid'}},at=clock[0])['changed']==0
    assert links.load_links()[token]['pass_changed_at']==granted
    assert links.reconcile_appointments(11,{},at=clock[0])['changed']==1
    assert links.load_links()[token]['pass_changed_at']==clock[0].isoformat()
    clock[0]=datetime.fromisoformat(links.load_links()[token]['access_expires'])+timedelta(seconds=1)
    assert links.reconcile_appointments(11,eligible,at=clock[0])['changed']==0
    assert links.load_links()[token]['gate_access'] is False


def test_role_grant_and_revoke_timestamp_follow_actual_identity_writers(identities,monkeypatch):
    s=identities;clock=[AT]
    monkeypatch.setattr('storage.pass_changes.now',lambda:clock[0])
    accept(s)
    assert s.links.load_links()['example-client']['pass_changed_at']==clock[0].isoformat()
    clock[0]+=timedelta(minutes=10)
    s.service.import_legacy()
    assert s.links.load_links()['example-client']['pass_changed_at']==AT.isoformat()
    s.service.revoke_client_admin('owner','example-client')
    assert s.links.load_links()['example-client']['pass_changed_at']==clock[0].isoformat()


def test_historical_fallback_does_not_invent_recent_pass_changes():
    assert changed_at({'created':'2024-01-01T15:00:00+03:00'})=='2024-01-01T12:00:00+00:00'
    assert changed_at({}) is None
    assert changed_at({'pass_changed_at':'invalid','created':'invalid'}) is None


@pytest.mark.parametrize('endpoint',['/admin/api/create-link','/admin/api/create-temp-link'])
def test_email_pass_delivery_validates_before_creation_and_uses_smtp(web,tmp_path,monkeypatch,endpoint):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    web.module.product_services()
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    smtp=Mock(return_value=True);monkeypatch.setattr('smtp_mailing.send_email',smtp)
    web.branches.update_settings(12,{'yclients_enabled':False})
    client=web.app.test_client();csrf=login(client)
    data={'branch_id':12,'user_name':'Email client','phone':PHONE,'send_link':True,'channel':'email'}
    before=len(web.links.load_links())
    for email in ('','invalid'):
        assert post(client,csrf,endpoint,{**data,'email':email}).status_code==400
        assert len(web.links.load_links())==before
    result=post(client,csrf,endpoint,{**data,'email':'client@example.invalid'})
    assert result.status_code==200 and result.json['delivery']['status']=='sent'
    token=result.json['token'];saved=web.links.load_links()[token]
    assert saved['email']=='client@example.invalid' and not saved['mailing_enabled']
    assert smtp.call_count==1 and smtp.call_args.args[0]=='client@example.invalid'
    assert '/opn/'+token in smtp.call_args.args[2]
    assert post(client,csrf,endpoint,{**data,'branch_id':123,'email':'other@example.invalid'}).status_code==403
    assert smtp.call_count==1 and web.calls==[]


def test_client_api_defaults_to_newest_pass_change_with_tenant_scope(web,monkeypatch):
    clock=[AT];monkeypatch.setattr('storage.pass_changes.now',lambda:clock[0])
    web.links.toggle_user_access(web.token_a,False)
    clock[0]+=timedelta(minutes=10);web.links.toggle_user_access(web.token_b,False)
    client=web.app.test_client();login(client,'super')
    assert [row['token'] for row in client.get('/admin/api/users').json['users']]==[web.token_b,web.token_a]
    client=web.app.test_client();login(client)
    assert [row['token'] for row in client.get('/admin/api/users').json['users']]==[web.token_a]


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')
def test_browser_changed_column_age_and_email_for_new_and_guest_pass(web,browser_ui,tmp_path,monkeypatch):
    page,origin,expect,errors=browser_ui
    clock=[datetime.now(timezone.utc)-timedelta(hours=3)]
    monkeypatch.setattr('storage.pass_changes.now',lambda:clock[0])
    web.links.toggle_user_access(web.token_a,False)
    clock[0]+=timedelta(hours=2);web.links.toggle_user_access(web.token_b,False)
    sign_in(page,origin)
    expect(page.locator('#usersTableBody tr').first).to_contain_text('Client B')
    expect(page.locator('#usersTableBody tr').first.locator('td[data-label="Изменён"]')).to_have_text('1ч')
    assert page.evaluate('''()=>[0,59,61,3599,3600,86399,86400].map(seconds=>passChangedAge(new Date(1700000000000-seconds*1000).toISOString(),1700000000000))''')==['1м','1м','1м','59м','1ч','23ч','1д']
    page.locator('th[onclick="sortUsersBy(\'pass_changed_at\')"]').click()
    expect(page.locator('#usersTableBody tr').first).to_contain_text('Client A')
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    web.module.product_services()
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    smtp=Mock(return_value=True);monkeypatch.setattr('smtp_mailing.send_email',smtp)
    web.branches.update_settings(12,{'yclients_enabled':False})
    page.goto(origin+'/admin/add-user')
    page.locator('#branchId').select_option('12');page.locator('#userName').fill('Email browser client')
    page.locator('#phone').fill(PHONE);page.locator('#clientEmail').fill('browser@example.invalid')
    page.locator('#sendLink').check();page.locator('#createDeliveryChannel').select_option('email')
    with page.expect_response('**/admin/api/create-link') as created:page.locator('#createLinkForm button[type=submit]').click()
    assert created.value.json()['delivery']['status']=='sent'
    page.goto(origin+'/admin/temp-link');page.locator('#tempBranch').select_option('12')
    page.locator('#tempPhone').fill('+15550100555');page.locator('#tempEmail').fill('guest@example.invalid')
    page.locator('#sendLinkCheckbox').check();page.locator('#tempDeliveryChannel').select_option('email')
    with page.expect_response('**/admin/api/create-temp-link') as guest:page.locator('#createTempBtn').click()
    assert guest.value.json()['delivery']['status']=='sent'
    assert [call.args[0] for call in smtp.call_args_list]==['browser@example.invalid','guest@example.invalid']
    assert web.calls==[] and not errors
