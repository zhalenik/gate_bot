"""Browser connection wizard acceptance with synthetic tenants and fake providers."""
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from test_ui_browser import web, browser_ui, sign_in


pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


def test_integration_token_only_chatpush_and_yclients_2fa(web,browser_ui,tmp_path,monkeypatch):
    from storage.sql_documents import DocumentStore,migrate_legacy,read_document,write_document
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    identity.attach_console_account('super',web.links.load_links()[web.token_a]['phone'],client_token=web.token_a)
    with identity.product.connect() as db:
        branches=read_document(db,'branches.json');branches['123']['yclients_enabled']=False;write_document(db,'branches.json',branches)
    authorize=Mock(side_effect=[{'requires_2fa':True,'challenge':'offline-uuid-123'}, {'user_token':'offline-private-yc-token','account':{'id':44,'name':'Example YC account','email':'yc-user@example.invalid'}}])
    check=Mock(return_value={'status':'success','connected':True,'code':'ready','checks':[{'key':'contacts','state':'success','code':'verified'}]})
    monkeypatch.setattr('yclients_connection.YClientsConnector.authorize',authorize)
    monkeypatch.setattr('yclients_connection.YClientsConnector.check_permissions',check)
    page,origin,expect,errors=browser_ui
    sign_in(page,origin)
    assert page.goto(origin+'/admin/integrations').status==200
    expect(page.locator('#integrationBranch option')).to_have_count(1)
    expect(page.locator('[name=user_token]')).to_have_count(0)
    yc=page.locator('section.panel').filter(has=page.get_by_role('heading',name='Учётная запись YCLIENTS',exact=True))
    cp=page.locator('section.panel').filter(has=page.get_by_role('heading',name='ChatPush',exact=True)).filter(has=page.locator('form'))
    expect(cp.locator('[name=scope]')).to_have_count(0)
    expect(cp.get_by_role('button',name='Проверить доступ')).to_have_count(0)
    cp.get_by_label('Токен ChatPush',exact=True).fill('offline-private-chatpush-token')
    cp.get_by_role('button',name='Сохранить токен',exact=True).click()
    expect(cp.locator('[name=token]')).to_have_value('')
    expect(cp).to_contain_text('Токен сохранён')
    assert identity.integration_credentials('12','chatpush')=={'token':'offline-private-chatpush-token'}
    expect(page.locator('a[href="/admin/chatpush"]')).to_have_count(0)
    yc.get_by_label('Телефон или email в YCLIENTS',exact=True).fill('offline@example.invalid')
    yc.get_by_label('Пароль YCLIENTS',exact=True).fill('offline-password')
    yc.get_by_role('button',name='Войти в YCLIENTS',exact=True).click()
    expect(yc.locator('[name=code]')).to_be_visible()
    yc.get_by_label('Код двухфакторной защиты',exact=True).fill('123456')
    yc.get_by_role('button',name='Войти в YCLIENTS',exact=True).click()
    expect(yc).to_contain_text('Подключение проверено')
    expect(yc).to_contain_text('Example YC account')
    expect(yc).to_contain_text('yc-user@example.invalid')
    expect(yc.locator('[name=password]')).to_have_value('')
    assert identity.integration_credentials('12','yclients')['user_token']=='offline-private-yc-token'
    assert 'offline-private' not in page.locator('body').inner_text()
    assert authorize.call_count==2 and check.call_count==1
    screenshots=Path.cwd()/'private'/'browser-checks';screenshots.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(screenshots/'integrations-desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(screenshots/'integrations-mobile.png'),full_page=True)
    assert page.goto(origin+'/admin/chatpush').status==404


def test_marketplace_integration_has_status_and_no_manual_login(web,browser_ui,tmp_path,monkeypatch):
    from storage.sql_documents import DocumentStore,migrate_legacy,read_document,write_document
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    identity.attach_console_account('super',web.links.load_links()[web.token_a]['phone'],client_token=web.token_a)
    with identity.product.connect() as db:
        branches=read_document(db,'branches.json');branches['123']['yclients_enabled']=False
        branches['12'].update(yclients_connection_mode='marketplace',yclients_connection_source='marketplace',yclients_marketplace_state='active')
        write_document(db,'branches.json',branches)
    identity.save_integration('super','12','yclients',{'user_token':'offline-system-token'},marketplace=True)
    authorizer=Mock(side_effect=AssertionError('No manual auth for marketplace'))
    checker=Mock(return_value={'status':'success','connected':True,'code':'ready','checks':[]})
    monkeypatch.setattr('yclients_connection.YClientsConnector.authorize',authorizer)
    monkeypatch.setattr('yclients_connection.YClientsConnector.check_permissions',checker)
    page,origin,expect,errors=browser_ui
    sign_in(page,origin);page.goto(origin+'/admin/integrations')
    yc=page.locator('section.panel').filter(has=page.get_by_role('heading',name='Приложение YCLIENTS Marketplace',exact=True))
    expect(yc.locator('[name=login]')).to_be_hidden()
    expect(yc.locator('[name=password]')).to_be_hidden()
    yc.get_by_role('button',name='Проверить доступ',exact=True).click()
    expect(yc).to_contain_text('Подключение проверено')
    authorizer.assert_not_called();checker.assert_called_once()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def test_marketplace_registration_hides_branch_id_and_yclients_credentials(web,browser_ui):
    page,origin,expect,errors=browser_ui
    page.goto(origin+'/yclients/marketplace/connect?salon_id=998')
    page.goto(origin+'/register-branch?marketplace=1')
    expect(page.locator('#identityDescription')).to_contain_text('Филиал уже выбран')
    expect(page.locator('[name=branch_id]')).to_have_value('998')
    expect(page.locator('[name=branch_id]')).to_be_hidden()
    expect(page.locator('[name=yclients_enabled]')).to_be_hidden()
    expect(page.get_by_label('Пароль GateBot (от 12 символов)',exact=True)).to_be_visible()
    expect(page.get_by_label('Пароль YCLIENTS',exact=True)).to_have_count(0)
    page.get_by_role('link',name='Другой способ регистрации',exact=True).click()
    expect(page.locator('[name=branch_id]')).to_be_visible()
