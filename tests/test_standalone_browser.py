"""Standalone public registration in a real browser; SMS remains a fake."""
import os
import re
import pytest
from test_web_security import web
from test_ui_browser import browser_ui, sign_in

pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


def test_standalone_registration_needs_no_company_id_and_completes(web,browser_ui,tmp_path,monkeypatch):
    from storage.sql_documents import migrate_legacy,DocumentStore
    page,origin,expect,errors=browser_ui
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    services=web.module.product_services();sent=[]
    services.identity.sender=lambda *args:sent.append(args) or {'status':'sent'}
    page.goto(origin+'/register-branch')
    page.locator('[name=yclients_enabled]').select_option('false')
    expect(page.locator('[name=branch_id]')).to_be_hidden()
    for field,value in [('branch_name','Standalone example'),('name','Local owner'),('phone','+15550400606'),('username','local-owner@example.invalid'),('password','offline-browser-password')]:
        page.locator('[name='+field+']').fill(value)
    with page.expect_response('**/register-branch/code') as sent_code:page.locator('#otpResend').click()
    assert sent_code.value.status==200
    expect(page.locator('#otpDeliveryStatus')).to_contain_text('отправлена')
    expect(page.locator('[name=yclients_enabled]')).to_have_value('false')
    expect(page.locator('[name=yclients_enabled]')).to_be_enabled()
    code=re.search(r'\b[0-9]{6}\b',sent[-1][2]).group();page.locator('[name=code]').fill(code)
    with page.expect_response('**/register-branch/complete') as completed:
        page.get_by_role('button',name='Завершить регистрацию',exact=True).click()
    assert completed.value.status==200
    result=completed.value.json();assert result['status']=='active' and result['yclients_enabled'] is False
    branch=web.branches.get_branch(result['branch_id'])
    assert branch.enabled and branch.buttons==[] and branch.id_origin=='internal'
    expect(page.get_by_role('link',name='Войти',exact=True)).to_be_visible()
    assert web.calls==[]


def test_standalone_clients_hide_only_crm_and_keep_manual_actions(web,browser_ui,tmp_path,monkeypatch):
    from storage.sql_documents import migrate_legacy,DocumentStore
    page,origin,expect,errors=browser_ui
    migrate_legacy(tmp_path,activate=True);monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    web.branches.update_settings('12',{'yclients_enabled':False,'buttons':[]})
    with web.branches.store.transaction() as branches:branches['12']['hardware_routes_verified']=False
    with web.links.store.transaction() as links:links[web.token_a]['tg_id']=101
    sign_in(page,origin,'tenant-a')
    expect(page.locator('th[onclick*="has_yclients"]')).to_be_hidden()
    expect(page.locator('#usersTableBody tr')).to_have_count(1)
    expect(page.locator('#usersTableBody [title="Копировать ссылку"]')).to_be_visible()
    status=page.locator('#usersTableBody .client-notifications-status')
    expect(status).to_be_enabled();expect(status).to_have_attribute('data-active-channels','telegram');status.click()
    expect(page.get_by_role('switch',name='Мессенджеры (сообщение от администратора)',exact=True)).to_have_count(0)
    page.get_by_role('switch',name='Telegram Bot',exact=True).uncheck()
    expect(status).to_have_attribute('data-active-channels','')
    page.get_by_role('dialog',name='Уведомления клиента',exact=True).get_by_role('button',name='Закрыть',exact=True).click()
    page.locator('#usersTableBody [title="Редактировать"]').click()
    expect(page.get_by_label('YCLIENTS ID',exact=True)).to_have_count(0)
    expect(page.get_by_label('Telegram — подтвержденная привязка')).to_be_visible()
    page.get_by_role('button',name='Отмена',exact=True).click()
    page.locator('#usersTableBody [title="Назначить администратором"]').click()
    expect(page.locator('.modal option[value=chatpush]')).to_have_count(0)
    page.get_by_role('button',name='Отмена',exact=True).click()
    page.goto(origin+'/preferences/'+web.token_a)
    expect(page.locator('input[name=chatpush]')).to_have_count(0)
    expect(page.locator('input[name=sms]')).to_have_count(0)
    expect(page.locator('input[name=messengers]')).to_have_count(0)
    page.goto(origin+'/admin/settings')
    expect(page.locator('#settingsHardwareHint')).to_be_hidden()
    expect(page.locator('#addSettingsButton')).to_be_enabled()
    expect(page.locator('#settingsYclients')).to_be_hidden()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert web.calls==[]


def test_super_creates_standalone_in_settings_with_server_assigned_id(web,browser_ui,tmp_path,monkeypatch):
    from storage.sql_documents import migrate_legacy,DocumentStore
    page,origin,expect,errors=browser_ui
    migrate_legacy(tmp_path,activate=True);monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    sign_in(page,origin)
    page.goto(origin+'/admin/settings');page.locator('#newBranchBtn').click()
    page.locator('#settingsYclients').select_option('false')
    expect(page.locator('#settingsBranchId')).to_be_hidden()
    expect(page.locator('#settingsButtons tr')).to_have_count(0)
    page.locator('#settingsName').fill('No CRM example')
    with page.expect_response('**/admin/api/branch-settings') as created:page.locator('#saveSettingsButton').click()
    assert created.value.status==200
    request=created.value.request.post_data_json;assert 'branch_id' not in request and request['yclients_enabled'] is False
    result=created.value.json()['settings'];assert result['branch_id']>=1000000000000 and result['id_origin']=='internal'
    expect(page.locator('#settingsBranchId')).to_have_value(str(result['branch_id']))
    expect(page.locator('#settingsYclients')).to_be_disabled()
    assert web.calls==[]
