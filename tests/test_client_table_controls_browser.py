"""Client-table roles and notification modal in Chromium, all sends mocked."""
import os
import re
from types import SimpleNamespace

import pytest

from test_client_notification_web import preferences_web
from test_web_security import web,TEST_PASSWORD
from test_ui_browser import browser_ui,sign_in

pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


@pytest.fixture
def table_state(preferences_web):
    web,identity=preferences_web;sent=[]
    identity.sender=lambda *args:sent.append(args) or {'status':'sent'}
    return SimpleNamespace(web=web,identity=identity,sent=sent)


def promote_client(state):
    identity,web=state.identity,state.web
    identity.attach_console_account('tenant-a','+15550100611')
    invited=identity.invite('super','12',client_token=web.token_a)
    token=invited['link'].rsplit('/',1)[-1];challenge=identity.request_otp(token,ip='127.0.0.1')
    code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    identity.accept_invitation(token,challenge['challenge_id'],code,'Client A','client-admin@example.invalid',TEST_PASSWORD)
    state.sent.clear()


def test_notification_status_icons_modal_scope_connections_and_mobile(table_state,browser_ui,tmp_path):
    state=table_state;web,identity=state.web,state.identity;page,origin,expect,errors=browser_ui
    web.branches.update_settings(12,{'yclients_enabled':False})
    web.links.bind_identity('telegram','81001',token=web.token_a)
    identity.save_prefs_for_token(web.token_a,{'enabled':True,'channels':{'telegram':True}})
    sign_in(page,origin,'tenant-a');page.locator('#searchInput').fill('Client A')
    status=page.locator('#usersTableBody .client-notifications-status');expect(status).to_have_attribute('data-active-channels','telegram');expect(status.locator('svg')).to_have_count(1)
    expect(page.locator('#usersTableBody input[type=checkbox]')).to_have_count(0)
    status.click();modal=page.get_by_role('dialog',name='Уведомления клиента',exact=True)
    expect(modal.get_by_role('switch')).to_have_count(4)
    expect(modal.get_by_role('switch',name='Push',exact=True)).to_be_disabled()
    expect(modal.get_by_role('switch',name='Мессенджеры (сообщение от администратора)',exact=True)).to_have_count(0)
    modal.get_by_role('switch',name='Telegram Bot',exact=True).uncheck();expect(status).to_have_attribute('data-active-channels','')
    modal.get_by_role('switch',name='MAX Bot',exact=True).click()
    modal.get_by_role('button',name='Получить ссылку для клиента',exact=True).click()
    expect(modal.get_by_label('Ссылка для подключения клиента',exact=True)).to_have_value(re.compile(r'https://max\.ru/.*\?start='))
    expect(modal).to_contain_text('в своём аккаунте')
    expect(modal.get_by_role('switch',name='MAX Bot',exact=True)).not_to_be_checked()
    assert page.url.endswith('/admin/users') and not identity.bot_admin('max','81002')
    web.links.bind_identity('max','81002',token=web.token_a)
    modal.get_by_role('button',name='Проверить подключения',exact=True).click()
    expect(modal.get_by_role('switch',name='MAX Bot',exact=True)).to_be_checked()
    expect(status).to_have_attribute('data-active-channels','max')
    modal.get_by_role('switch',name='Email',exact=True).click();modal.get_by_label('Email клиента',exact=True).fill('client@example.invalid')
    modal.get_by_role('button',name='Отправить подтверждение',exact=True).click()
    expect(modal).to_contain_text('Письмо принято к отправке')
    assert state.sent[-1][0:2]==('email','client@example.invalid')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(tmp_path/'client-notification-modal.png'),full_page=True)
    assert web.calls==[] and not errors


def test_admin_badge_edit_revoke_and_last_admin_guard(table_state,browser_ui):
    state=table_state;web,identity=state.web,state.identity;page,origin,expect,errors=browser_ui
    promote_client(state)
    sign_in(page,origin);page.locator('#searchInput').fill('Client A')
    expect(page.locator('#usersTableBody .client-role-badge')).to_have_text('Администратор')
    page.locator('#usersTableBody [aria-label=Редактировать]').click()
    modal=page.get_by_role('dialog',name='Редактировать пользователя',exact=True)
    expect(modal.locator('[data-role-info]')).to_contain_text('Администратор филиала')
    revoke=modal.get_by_role('button',name='Отозвать права администратора',exact=True);expect(revoke).to_be_enabled();revoke.click()
    page.get_by_role('dialog',name='Отозвать права администратора?',exact=True).get_by_role('button',name='Отозвать права',exact=True).click()
    expect(page.locator('#usersTableBody .client-role-badge')).to_have_count(0)
    assert web.token_a in web.links.load_links() and not identity.client_admin_state('super',web.token_a)['is_admin']
    page.locator('#searchInput').fill('+15550100611')
    expect(page.locator('#usersTableBody .client-role-badge')).to_contain_text('единственный')
    expect(page.locator('#usersTableBody [aria-label=Удалить]')).to_be_disabled()
    page.locator('#usersTableBody [aria-label=Редактировать]').click()
    expect(page.get_by_role('button',name='Отозвать права администратора',exact=True)).to_be_disabled()
    assert web.calls==[] and state.sent==[] and not errors


def test_deleting_admin_warns_and_revokes_role_together(table_state,browser_ui):
    state=table_state;web,identity=state.web,state.identity;page,origin,expect,errors=browser_ui
    promote_client(state);person_id=identity.client_admin_state('super',web.token_a)['person_id']
    sign_in(page,origin);page.locator('#searchInput').fill('Client A')
    page.locator('#usersTableBody [aria-label=Удалить]').click()
    modal=page.get_by_role('dialog',name='Удалить пользователя?',exact=True)
    expect(modal).to_contain_text('Это администратор филиала')
    expect(modal).to_contain_text('отзовёт его права администратора')
    modal.get_by_role('button',name='Удалить',exact=True).click()
    expect(page.locator('#usersTableBody tr')).to_have_count(0)
    assert web.token_a not in web.links.load_links()
    assert not any(row['person_id']==person_id and row['status']=='active' for row in identity.list_admins('super','12'))
    assert web.calls==[] and state.sent==[] and not errors
