"""Help navigation and browser-to-bot-to-browser support, using fake transports."""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_ui_browser import web,browser_ui,sign_in


pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


def test_help_scope_and_support_roundtrip_without_live_messages(web,browser_ui,tmp_path,monkeypatch):
    from bot_state import BotStateStore
    from bot_workflows import BotWorkflows
    from storage import config
    from storage.sql_documents import DocumentStore,migrate_legacy,read_document,write_document
    migrate_legacy(tmp_path,activate=True);monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    products=web.module.product_services();identity=products.identity
    identity.attach_console_account('super','+15550999123')
    identity.attach_console_account('tenant-a',web.links.load_links()[web.token_a]['phone'],client_token=web.token_a)
    with identity.product.connect() as db:
        branches=read_document(db,'branches.json');branches['12']['yclients_enabled']=False;write_document(db,'branches.json',branches)
        owner=identity._admin(db,'super')
        for channel,user_id in [('telegram','80001'),('max','80002')]:db.execute('INSERT INTO bot_bindings(person_id,platform,user_id,assurance) VALUES(?,?,?,"admin_verified")',(owner['person_id'],channel,user_id))
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    sender=Mock(return_value=True);products.support.sender=sender;products.support.ready=lambda channel:True
    page,origin,expect,errors=browser_ui
    sign_in(page,origin,'tenant-a');requests=[];page.on('request',lambda request:requests.append(request.url))
    assert page.goto(origin+'/admin/help').status==200
    expect(page.locator('#help-modem')).to_have_count(0)
    expect(page.locator('#help-yclients-connect')).to_have_count(0)
    expect(page.locator('#help-standalone')).to_have_count(1)
    page.get_by_role('searchbox',name='Поиск по инструкциям').fill('пропуск')
    expect(page.locator('#help-passes')).to_be_visible()
    page.locator('#help-passes summary').click();expect(page.locator('#help-passes ol')).to_be_visible()
    assert not any('/admin/api/support' in url for url in requests)
    page.get_by_role('button',name='Поддержка',exact=True).click()
    expect(page.locator('#supportDialog')).to_be_visible()
    expect(page.locator('#supportBranch option')).to_have_count(1)
    expect(page.locator('#supportBranch')).not_to_be_visible()
    page.get_by_role('textbox',name='Сообщение в поддержку').fill('Не нахожу свой пропуск — синтетический тест')
    with page.expect_response(lambda response:response.url.endswith('/admin/api/support/messages') and response.request.method=='POST') as response:
        page.locator('#supportForm button[type=submit]').click()
    assert response.value.status==200;thread=response.value.json()['thread_id']
    expect(page.locator('#supportMessages')).to_contain_text('синтетический тест')
    assert products.support.process_once()==2 and sender.call_count==2
    workflows=BotWorkflows(SimpleNamespace(identity=identity,support=products.support,branches=web.branches),BotStateStore(tmp_path/'support-browser-bot.sqlite3'))
    workflows.action('telegram','80001','x:reply:'+str(thread))
    workflows.message('telegram','80001','Ответ поддержки из Telegram — синтетический тест')
    expect(page.locator('#supportMessages')).to_contain_text('Ответ поддержки из Telegram',timeout=9000)
    screenshots=Path.cwd()/'private'/'browser-checks';screenshots.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(screenshots/'support-desktop.png'),full_page=True)
    assert page.locator('#supportDialog').bounding_box()['width']>800
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert page.locator('#supportForm button[type=submit]').bounding_box()['y']+page.locator('#supportForm button[type=submit]').bounding_box()['height']<=844
    page.screenshot(path=str(screenshots/'support-mobile.png'),full_page=True)
    page.get_by_role('button',name='Закрыть поддержку',exact=True).click()
    expect(page.locator('#supportDialog')).not_to_be_visible()
    assert page.goto(origin+'/admin/support').status==200
    page.locator('[data-thread="'+str(thread)+'"]').click()
    expect(page.locator('#supportMessages')).to_contain_text('Ответ поддержки из Telegram')
    page.goto(origin+'/');expect(page.locator('#supportLauncher')).to_have_count(0)
