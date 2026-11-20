"""Optional offline browser roundtrip of modem cards and journal filtering."""
import os

import pytest

from telephony_store import TelephonyStore
from test_system_settings_web import system_web
from test_ui_browser import browser_ui, sign_in
from test_web_security import web


@pytest.mark.skipif(os.getenv('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')
def test_saved_modems_and_filtered_calls(system_web, browser_ui):
    page,origin,expect,errors = browser_ui
    first = TelephonyStore()
    second = TelephonyStore(first.store.path,modem_id='b'*32,modem_name='Линия 2')
    first.add('call','incoming',phone='1234567',status='ended')
    second.add('call','incoming',phone='1234568',status='ended')
    sign_in(page,origin)
    page.goto(origin+'/admin/system-settings')
    page.locator('#modemSettings summary').click()
    cards = page.locator('#modemCards fieldset')
    expect(cards).to_have_count(1)
    cards.first.locator('[name=port]').fill('COM4')
    page.locator('#modemAdd').click()
    cards.last.locator('[name=name]').fill('Линия 2')
    cards.last.locator('[name=port]').fill('COM5')
    cards.last.locator('[name=call_enabled]').uncheck()
    page.locator('#modemSave').click()
    expect(page.locator('#modemSettingsNotice')).to_contain_text('Модемы сохранены')
    page.reload()
    if not page.locator('#modemAdd').is_visible():
        page.locator('#modemSettings summary').click()
    expect(cards).to_have_count(2)
    expect(cards.last.locator('[name=call_enabled]')).not_to_be_checked()
    page.goto(origin+'/admin/telephony')
    page.locator('#modeCalls').click()
    expect(page.locator('#callList .call-row')).to_have_count(2)
    page.locator('#telephonyModem').select_option('b'*32)
    expect(page.locator('#callList .call-row')).to_have_count(1)
    expect(page.locator('#callList')).to_contain_text('Линия 2')
    assert not errors
