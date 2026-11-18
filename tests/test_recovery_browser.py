"""Password proof in Chrome; all SMS go to an in-memory fake."""
import os
import re
import pytest
from test_web_security import web
from test_ui_browser import browser_ui
from test_recovery_web import prepare

pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')

def test_sms_recovery_requires_two_passwords_before_login(web,browser_ui,tmp_path,monkeypatch):
    identity,sent=prepare(web,tmp_path,monkeypatch)
    page,origin,expect,errors=browser_ui
    page.goto(origin+'/admin/login');page.get_by_role('link',name='Не помню пароль').click()
    page.locator('#recoveryPhoneValue').fill('+15550400101')
    with page.expect_response('**/admin/recover/code') as response:page.locator('#recoverySend').click()
    assert response.value.status==200
    expect(page.locator('#recoverySend')).to_be_disabled()
    code=re.search(r'\b[0-9]{6}\b',sent[-1][2]).group()
    page.locator('#recoveryCodeValue').fill(code)
    page.get_by_role('button',name='Подтвердить телефон').click()
    expect(page.locator('#recoveryPassword')).to_be_visible()
    assert page.request.get(origin+'/admin/api/users').status==401
    page.locator('#newPassword').fill('new-offline-browser-password')
    page.locator('#confirmPassword').fill('different-offline-password')
    page.get_by_role('button',name='Сохранить новый пароль').click()
    expect(page.locator('#recoveryNotice')).to_contain_text('не совпадают')
    page.locator('#confirmPassword').fill('new-offline-browser-password')
    with page.expect_response('**/admin/recover/complete') as completed:page.get_by_role('button',name='Сохранить новый пароль').click()
    assert completed.value.status==200
    expect(page.locator('#recoveryNotice')).to_contain_text('Пароль изменён')
    assert page.request.get(origin+'/admin/api/users').status==401
    assert web.auth.verify_admin('tenant-a','new-offline-browser-password')
    assert web.calls==[]
