"""Local Chrome acceptance of QR administration and a verified client journey."""
import os
import re
from urllib.parse import urlsplit

import pytest

from test_web_security import web
from test_ui_browser import browser_ui, sign_in


pytestmark = pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')


def test_printed_gate_qr_phone_verification_device_and_revocation(web, browser_ui, tmp_path, monkeypatch):
    from storage.sql_documents import DocumentStore, migrate_legacy
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    web.links.toggle_user_access(web.token_a, True)
    identity = web.module.product_services().identity
    sent = []
    identity.sender = lambda channel, target, text, context: sent.append((channel, target, text, context)) or {'status': 'queued'}
    web.module.access_service.cooldown_seconds = 20
    page, origin, expect, errors = browser_ui
    sign_in(page, origin)
    page.goto(origin + '/admin/settings')
    page.locator('#settingsBranch').select_option('12')
    page.get_by_role('tab', name='Ворота и QR').click()
    expect(page.get_by_role('button', name='QR-код', exact=True)).to_be_visible()
    page.get_by_role('button', name='QR-код', exact=True).click()
    expect(page.get_by_role('button', name='Создать QR-код', exact=True)).to_be_visible()
    page.get_by_role('button', name='Создать QR-код', exact=True).click()
    expect(page.locator('.qr-dialog img')).to_be_visible()
    page.wait_for_function('document.querySelector(".qr-dialog img").naturalWidth > 0')
    link = page.get_by_label('Постоянная ссылка ворот').input_value()
    with page.expect_download() as downloaded:
        page.get_by_role('link', name='Скачать PNG').click()
    assert downloaded.value.suggested_filename == 'gate-qr.png'
    page.screenshot(path=str(tmp_path / 'gate-qr-admin.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    pending = []
    page.route('**/gate/*/state', lambda route: pending.append(route))
    page.goto(origin + urlsplit(link).path)
    expect(page.locator('.header-signin')).to_have_count(0)
    expect(page.locator('a.brand')).to_have_count(0)
    expect(page.locator('#qrOpen')).to_be_hidden()
    page.locator('#qrPhone').fill(web.links.load_links()[web.token_a]['phone'])
    page.locator('#qrSendCode').click()
    expect(page.locator('#qrCodeForm')).to_be_visible()
    expect(page.locator('#qrResend')).to_be_disabled()
    expect(page.locator('#qrRemember')).to_be_checked()
    page.locator('#qrRemember').uncheck()
    expect(page.locator('#qrRememberHint')).to_contain_text('12 часов')
    assert len(sent) == 1
    # A slow initial state response must not replace the new SMS challenge.
    assert pending
    pending.pop(0).fulfill(json={'state': 'phone_required', 'challenge': {'ready': False, 'retry_after': 0}})
    expect(page.locator('#qrCodeForm')).to_be_visible()
    page.unroute('**/gate/*/state')
    page.locator('#qrResend').dispatch_event('click')
    assert len(sent) == 1
    code = re.search(r'\b\d{6}\b', sent[0][2]).group()
    page.locator('#qrCode').fill(code)
    page.locator('#qrVerify').click()
    expect(page.locator('#qrOpen')).to_be_visible()
    expect(page.locator('#qrPreferences')).to_be_visible()
    device_cookie = next(cookie for cookie in page.context.cookies() if cookie['name'].startswith('gatebot_qr_'))
    assert device_cookie['expires'] == -1 and device_cookie['httpOnly'] and device_cookie['sameSite'] == 'Lax'
    page.reload()
    expect(page.locator('#qrOpen')).to_be_visible()
    expect(page.locator('#qrPhoneForm')).to_be_hidden()
    assert len(sent) == 1
    with page.expect_response('**/gate/*/open') as opened:
        page.locator('#qrOpen').click()
    assert opened.value.status == 200
    expect(page.locator('#qrOpen')).to_be_disabled()
    expect(page.locator('#qrCooldown')).to_be_visible()
    assert len(web.calls) == 1
    page.screenshot(path=str(tmp_path / 'gate-qr-client-mobile.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    web.links.toggle_user_access(web.token_a, False)
    page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
    expect(page.locator('#qrOpen')).to_be_hidden()
    expect(page.locator('#qrHeading')).to_have_text('Пропуск не действует')
    page.locator('#qrForget').click()
    expect(page.locator('#qrPhoneForm')).to_be_visible()
    assert len(web.calls) == 1
