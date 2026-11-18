"""Branch messenger selection: real web boundaries, fake integrations and browser."""
import os
from unittest.mock import Mock

import pytest

from messenger_service import MessengerService
from storage import config
from storage.sql_documents import DocumentStore,migrate_legacy,read_document,write_document
from test_web_security import web,login,post
from test_ui_browser import browser_ui,sign_in


@pytest.fixture
def messenger_web(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    service=web.module.product_services().identity
    monkeypatch.setattr(config,'YC_PARTNER_TOKEN','offline-partner-token')
    service.save_integration('super','12','yclients',{'user_token':'offline-branch-user-token'})
    outgoing=Mock(side_effect=AssertionError('Settings must not send messages'))
    monkeypatch.setattr('yclients_delivery.YClientsDelivery.send',outgoing)
    monkeypatch.setattr('chatpush.send_message',outgoing)
    return web,service,outgoing


def test_authentication_primary_scope_and_csrf(messenger_web):
    web,identity,outgoing=messenger_web;client=web.app.test_client()
    assert client.get('/admin/api/messenger-settings?branch_id=12').status_code==401
    csrf=login(client,'tenant-a')
    own=client.get('/admin/api/messenger-settings').get_json()['messaging']
    assert own['selected']=='auto' and own['provider']=='yclients' and own['configured']
    assert client.get('/admin/api/messenger-settings?branch_id=123').status_code==403
    assert post(client,csrf,'/admin/api/messenger-settings',{'branch_id':'123','provider':'auto'}).status_code==403
    assert client.post('/admin/api/messenger-settings',json={'provider':'yclients'}).status_code==403
    saved=post(client,csrf,'/admin/api/messenger-settings',{'provider':'yclients'})
    assert saved.status_code==200 and saved.get_json()['messaging']['selected']=='yclients'
    assert 'offline-' not in saved.get_data(as_text=True)
    outgoing.assert_not_called()


@pytest.mark.parametrize('body',[None,[],'invalid',{'provider':[]},{'provider':{}},{'provider':True},{'provider':123},{'provider':'unknown'},{'provider':'auto','branch_id':{}}])
def test_bad_payload_is_client_error_not_500(messenger_web,body):
    web,identity,outgoing=messenger_web;client=web.app.test_client();csrf=login(client,'super')
    if isinstance(body,dict) and 'branch_id' not in body:body={'branch_id':'12',**body}
    response=post(client,csrf,'/admin/api/messenger-settings',body)
    assert response.status_code==400
    assert MessengerService(identity).settings('super','12')['selected']=='auto'
    outgoing.assert_not_called()


def test_chatpush_requires_token_super_cross_scope_and_standalone_rejected(messenger_web):
    web,identity,outgoing=messenger_web;client=web.app.test_client();csrf=login(client,'tenant-a')
    assert post(client,csrf,'/admin/api/messenger-settings',{'provider':'chatpush'}).status_code==400
    identity.save_integration('tenant-a','12','chatpush',{'token':'offline-chatpush-token'})
    assert post(client,csrf,'/admin/api/messenger-settings',{'provider':'chatpush'}).status_code==200
    csrf=login(client,'super')
    assert post(client,csrf,'/admin/api/messenger-settings',{'branch_id':'123','provider':'yclients'}).status_code==200
    with identity.product.connect() as db:
        branches=read_document(db,'branches.json');branches['123']['yclients_enabled']=False;write_document(db,'branches.json',branches)
    assert post(client,csrf,'/admin/api/messenger-settings',{'branch_id':'123','provider':'auto'}).status_code==403
    outgoing.assert_not_called()


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')
def test_browser_selects_provider_and_refreshes_after_chatpush_connection(messenger_web,browser_ui):
    web,identity,outgoing=messenger_web;page,origin,expect,errors=browser_ui
    sign_in(page,origin,'tenant-a')
    assert page.goto(origin+'/admin/integrations').status==200
    panel=page.locator('#branchMessengerSettings');selector=panel.get_by_role('combobox',name='Способ отправки',exact=True)
    expect(panel).to_contain_text('Сервис подключён')
    expect(selector).to_have_value('auto')
    expect(selector.locator('option[value=chatpush]')).to_be_disabled()
    expect(panel).to_contain_text('Нужен ID клиента в YCLIENTS')
    cp=page.locator('section.panel').filter(has=page.get_by_role('heading',name='ChatPush',exact=True)).filter(has=page.locator('form'))
    cp.get_by_label('Токен ChatPush',exact=True).fill('browser-offline-chatpush')
    cp.get_by_role('button',name='Сохранить токен',exact=True).click()
    expect(selector.locator('option[value=chatpush]')).to_be_enabled()
    selector.select_option('chatpush')
    expect(panel).to_contain_text('по номеру телефона')
    panel.get_by_role('button',name='Сохранить способ отправки',exact=True).click()
    expect(panel).to_contain_text('Способ отправки сохранён')
    assert MessengerService(identity).settings('tenant-a','12')['selected']=='chatpush'
    selector.select_option('yclients')
    with page.expect_response(lambda response:'/admin/api/messenger-settings?' in response.url and response.request.method=='GET'):
        page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(selector).to_have_value('yclients')
    assert MessengerService(identity).settings('tenant-a','12')['selected']=='chatpush'
    panel.get_by_role('button',name='Сохранить способ отправки',exact=True).click()
    expect(panel).to_contain_text('Способ отправки сохранён')
    assert MessengerService(identity).settings('tenant-a','12')['provider']=='yclients'
    page.reload();expect(selector).to_have_value('yclients')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert web.calls==[] and not errors
    outgoing.assert_not_called()
