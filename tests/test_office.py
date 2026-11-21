"""The branch PWA is separately scoped, authenticated and cannot push to another tenant."""
from unittest.mock import Mock
import os
import pytest
from pwa_service import PwaService
from product_notifications import ProductNotifications
from storage import config
from storage.sql_documents import DocumentStore, migrate_legacy
from test_identity_product import identities
from test_pwa_push import subscription
from test_web_security import web, login, post
from test_ui_browser import browser_ui, sign_in


def test_office_navigation_login_and_role(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    identity.attach_console_account('tenant-a','+15550100901')
    c=web.app.test_client()
    assert c.get('/office/manifest.webmanifest').json['scope']=='/office/'
    assert c.get('/office/sw.js').headers['Service-Worker-Allowed']=='/office/'
    assert c.get('/office/messages?thread=12').location.endswith('/office/login')
    login(c,'super')
    assert c.get('/office/').status_code==403
    c=web.app.test_client(); csrf=login(c,'tenant-a')
    for section in ('clients','billing','new-client','guest','settings','messages','help','notifications'):
        response=c.get('/office/'+section)
        assert response.status_code==200,(section,response.status_code)
        assert '/office/manifest.webmanifest' in response.text and '/admin/telephony' not in response.text
    assert c.get('/office/logs').status_code==404
    billing=c.get('/office/billing?branch_id=123')
    assert 'Branch 12' in billing.text and 'Branch 123' not in billing.text
    assert '/admin/tariffs' not in billing.text
    device=subscription('office')
    assert c.post('/office/push/subscribe',json={}).status_code==403
    assert post(c,csrf,'/office/push/subscribe',{'subscription':device,'events':['client_message'],'consent':True}).status_code==200
    assert post(c,csrf,'/office/push/state',{'endpoint':device['endpoint']}).json['connected']
    assert c.post('/admin/logout',data={'csrf_token':csrf,'mobile_office':'1'}).location.endswith('/office/login')
    assert not web.module.product_services().pwa.office.settings('tenant-a',device['endpoint'])['connected']


def test_office_device_event_and_revalidation(identities,monkeypatch):
    state=identities
    network=Mock(return_value={'status':'accepted'})
    monkeypatch.setattr(config,'BASE_URL','https://example.invalid')
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    pwa=PwaService(state.service,transport=network)
    out=ProductNotifications(state.service,state.links,state.branches,state.billing,pwa=pwa)
    device=subscription('desk')
    with pytest.raises(PermissionError):
        pwa.office.subscribe('owner',device,['pass_requested'],True)
    with pytest.raises(ValueError):
        pwa.office.subscribe('north',device,['sms_received'],True)
    saved=pwa.office.subscribe('north',device,['pass_requested'],True)
    with pytest.raises(ValueError):
        pwa.subscribe('example-client',device,consent=True)
    for event,branch in [('client_message','11'),('pass_requested','12'),('pass_requested','11')]:
        out.event(event,branch,{'event_id':event+branch,'thread_id':11,'text':'Private message'})
    out.process_once()
    network.assert_called_once()
    payload=network.call_args.args[1]
    assert payload['url']=='/office/messages?thread=11' and 'Private message' not in str(payload)
    out.event('pass_requested','11',{'event_id':'second'})
    with state.auth.store.transaction() as admins:
        admins['north']['session_version']+=1
    out.process_once()
    assert network.call_count==1
    assert pwa.office.deliver(saved,{'event':'pass_requested','branch_id':'11'})['status']=='skipped'


def test_desktop_push_global_event_role_and_changed_preferences(identities,monkeypatch):
    state = identities
    monkeypatch.setattr(config,'BASE_URL','https://example.invalid')
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    network = Mock(return_value={'status':'accepted'})
    pwa = PwaService(state.service,transport=network)
    out = ProductNotifications(state.service,state.links,state.branches,state.billing,pwa=pwa)
    pwa.office.subscribe('owner',subscription('global'),None,True,desktop=True)
    pwa.office.subscribe('north',subscription('branch'),None,True,desktop=True)
    state.service.save_admin_preferences('owner',{'active_events':['sms_received'],'events':{'sms_received':{'push':True}}})
    out.event('sms_received',None,{'event_id':'first'})
    out.process_once()
    network.assert_called_once()
    assert network.call_args.args[1]['url']=='/admin/notifications'
    out.event('sms_received',None,{'event_id':'second'})
    state.service.save_admin_preferences('owner',{'events':{'sms_received':{'push':False}}})
    # A previously open version of the device form still posts its own selection.
    device=subscription('global')
    pwa.office.subscribe('owner',device,['sms_received'],True,desktop=True)
    assert 'sms_received' not in pwa.office.settings('owner',device['endpoint'],desktop=True)['selected']
    out.process_once()
    network.assert_called_once()


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')
def test_desktop_admin_can_connect_browser_and_choose_events(web,tmp_path,monkeypatch,browser_ui):
    from test_client_pwa_browser import mock_push
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    services=web.module.product_services()
    services.identity.attach_console_account('super','+15550100901')
    page,origin,expect,errors=browser_ui
    mock_push(page)
    sign_in(page,origin)
    page.evaluate("""() => {const subscribe=PushManager.prototype.subscribe;
        PushManager.prototype.subscribe=function(options){
          if(!window.simulatedTemporaryFailure){window.simulatedTemporaryFailure=true;throw new DOMException('Registration failed - push service error','AbortError');}
          return subscribe.call(this,options);
        };}""")
    offer=page.locator('[data-admin-push-offer]')
    expect(offer).to_be_visible()
    assert page.evaluate('fakePermissionCalls')==0
    offer.get_by_role('button',name='Включить').click()
    expect(offer).to_be_hidden()
    assert page.evaluate('fakePermissionCalls')==1
    page.goto(origin+'/admin/notification-settings')
    expect(page.locator('[data-office-push] [role=status]')).to_have_text('Уведомления подключены.')
    page.locator('[name=sms_received_push]').check()
    page.get_by_role('button',name='Сохранить',exact=True).click()
    expect(page.locator('#identityNotice')).to_contain_text('сохранены')
    assert services.identity.admin_preferences('super')['events']['sms_received']['push']
    page.get_by_role('button',name='Отключить на этом устройстве').click()
    expect(page.locator('[name=sms_received_push]')).to_be_disabled()
    assert not errors and not web.calls


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')
def test_mobile_office_install_scope_push_and_offline(web,tmp_path,monkeypatch,browser_ui):
    from test_client_pwa_browser import mock_push
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    services=web.module.product_services()
    services.identity.attach_console_account('tenant-a','+15550100901')
    page,origin,expect,errors=browser_ui
    device=mock_push(page)
    sign_in(page,origin,'tenant-a')
    page.set_viewport_size({'width':390,'height':844})
    page.goto(origin+'/office/settings')
    panel=page.locator('[data-office-push]')
    panel.get_by_role('button',name='Подключить Push').click()
    expect(panel.get_by_role('status')).to_have_text('Уведомления подключены.')
    assert services.pwa.office.settings('tenant-a',device['endpoint'])['selected']==['client_message','pass_requested']
    page.locator('.office-nav').get_by_text('Сообщения').click()
    expect(page.locator('[data-admin-desk]')).to_be_visible()
    page.wait_for_function("navigator.serviceWorker.controller?.scriptURL.endsWith('/office/sw.js')")
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.context.set_offline(True)
    page.reload(wait_until='domcontentloaded')
    expect(page.get_by_text('Отсутствует подключение к интернету')).to_be_visible()
    assert web.calls==[] and not errors
