"""Synthetic Chromium flow through restart-only system settings and shared SMTP."""
import os
from pathlib import Path
import pytest

from test_ui_browser import browser_ui,sign_in
from test_web_security import web
from test_system_settings_web import system_web
from storage import config


pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


def test_system_settings_edit_restart_status_secret_retention_and_mobile(system_web,browser_ui):
    web,store=system_web;page,origin,expect,errors=browser_ui
    sign_in(page,origin)
    assert page.goto(origin+'/admin/system-settings').status==200
    expect(page.locator('[data-shared-header]')).to_have_count(1)
    expect(page.locator('#systemSettingsSave')).to_be_enabled()
    page.get_by_label('Порт веб-сервера',exact=True).fill('6799')
    page.locator('#systemSettingsSearch').fill('BOT_TOKEN')
    page.get_by_label('Токен Telegram Bot',exact=True).fill('browser-synthetic-token')
    with page.expect_response(lambda response:response.url.endswith('/admin/api/system-settings') and response.request.method=='POST') as saved:
        page.locator('#systemSettingsSave').click()
    assert saved.value.status==200 and 'browser-synthetic-token' not in saved.value.text()
    expect(page.locator('#systemSettingsNotice')).to_contain_text('после перезапуска')
    expect(page.get_by_label('Токен Telegram Bot',exact=True)).to_have_value('')
    assert store.read()['values']['BOT_TOKEN']=='browser-synthetic-token'
    page.locator('#systemSettingsSearch').fill('PORT')
    expect(page.get_by_label('Порт веб-сервера',exact=True)).to_have_value('6799')
    expect(page.locator('[data-key=PORT]')).to_contain_text('Сейчас: 5000')
    page.locator('#systemSettingsSearch').fill('')
    page.locator('#systemSmtp > summary').click()
    expect(page.locator('#smtpForm')).to_be_visible()
    expect(page.locator('#smtpForm button[type=submit]')).to_be_enabled()
    screenshots=Path.cwd()/'private'/'browser-checks';screenshots.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(screenshots/'system-settings-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(screenshots/'system-settings-mobile.png'))
    sign_in(page,origin,'tenant-a')
    expect(page.locator('a[href="/admin/system-settings"]')).to_have_count(0)
    assert page.goto(origin+'/admin/system-settings').status==403
    assert web.calls==[] and not errors


def test_official_com_url_and_legacy_invalid_fields_can_be_opened_and_fixed(system_web,browser_ui,monkeypatch):
    web,store=system_web;page,origin,expect,errors=browser_ui
    for environment in (config.SYSTEM_BASE_ENVIRONMENT,config.SYSTEM_ACTIVE_ENVIRONMENT):
        monkeypatch.setitem(environment,'YC_BASE_URL','https://api.yclients.com/api/v1')
        monkeypatch.setitem(environment,'GATEBOT_TIMEZONE','Legacy/Unknown')
        monkeypatch.setitem(environment,'PORT','legacy-not-a-number')
    sign_in(page,origin)
    assert page.goto(origin+'/admin/system-settings').status==200
    expect(page.locator('#systemSettingsSave')).to_be_enabled()
    expect(page.locator('#setting-YC_BASE_URL')).to_have_value('https://api.yclients.com/api/v1')
    expect(page.locator('#systemSettingsState')).to_contain_text('Проверьте параметры: 2')
    expect(page.locator('#setting-PORT')).to_have_value('legacy-not-a-number')
    expect(page.locator('#setting-PORT')).to_have_attribute('aria-invalid','true')
    page.locator('#setting-PORT').fill('6798')
    page.locator('#setting-GATEBOT_TIMEZONE').fill('Europe/Moscow')
    with page.expect_response(lambda response:response.url.endswith('/admin/api/system-settings') and response.request.method=='POST') as saved:
        page.locator('#systemSettingsSave').click()
    assert saved.value.status==200
    expect(page.locator('#systemSettingsNotice')).to_contain_text('после перезапуска')
    assert store.read()['values']['PORT']=='6798'
    page.reload()
    expect(page.locator('#setting-PORT')).to_have_value('6798')
    expect(page.locator('[data-key=PORT]')).to_contain_text('текущего запуска')
    assert config.SYSTEM_ACTIVE_ENVIRONMENT['PORT']=='legacy-not-a-number'
    assert web.calls==[] and not errors
