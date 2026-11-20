"""Marketplace iframe, cookie isolation and single-use continuation, entirely offline."""
import os
import re
from html import escape
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from test_identity_product import identities, PASSWORD
from test_marketplace_automatic import signed_args
from test_web_security import web
from test_yclients_marketplace import marketplace


ROOT = '/yclients/marketplace'


def test_frame_cookie_and_single_use_first_party_handoff(web):
    client = web.app.test_client()
    entry = client.get(ROOT + '/connect?salon_id=12', headers={'Sec-Fetch-Dest': 'iframe'})
    assert entry.status_code == 303 and 'X-Frame-Options' not in entry.headers
    assert entry.headers['Content-Security-Policy'] == 'frame-ancestors https://yclients.ru https://*.yclients.ru'
    cookie = entry.headers['Set-Cookie']
    for value in ('__Secure-gatebot-marketplace=', 'HttpOnly', 'Secure', 'Partitioned', 'SameSite=None', 'Path=' + ROOT):
        assert value in cookie
    assert client.get(ROOT + '/state').status_code == 400
    assert client.get(ROOT + '/state?frame=1').json['branches'][0]['id'] == '12'
    ticket = re.fullmatch(r'/yclients/marketplace\?frame=start#([\w-]{43})', entry.location)[1]
    first_party = web.app.test_client()
    landing = first_party.get(ROOT + '/resume')
    csrf = re.search(rb'name="csrf-token" content="([^"]+)"', landing.data)[1].decode()
    assert landing.headers['X-Frame-Options'] == 'DENY'
    assert first_party.post(ROOT + '/resume', json={'ticket': ticket}).status_code == 403
    for expected in (200, 400):
        assert first_party.post(ROOT + '/resume', json={'ticket': ticket}, headers={'X-CSRF-Token': csrf}).status_code == expected
    assert first_party.get(ROOT + '/state').json['branches'][0]['id'] == '12'
    assert web.app.test_client().get('/admin/login').headers['X-Frame-Options'] == 'DENY'


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')
@pytest.mark.parametrize('scenario', ['existing', 'cookies_blocked', 'register'])
def test_cross_site_marketplace_frame_in_chrome(web, marketplace, monkeypatch, scenario):
    from werkzeug.serving import make_server
    playwright = pytest.importorskip('playwright.sync_api')
    executable = next((path for path in (Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
                       Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')) if path.exists()), None)
    if not executable:
        pytest.skip('Chrome/Edge not installed')
    query = 'salon_id=12'
    if scenario == 'register':
        s = marketplace
        s.api.status.return_value = {'state': 'active', 'installer_matches': True}
        monkeypatch.setattr(web.module, 'product_services', lambda: SimpleNamespace(identity=s.identity.service,
                            provider=lambda auth, access: {'marketplace': s.service, 'service': s.identity.service, 'auth': auth}))
        monkeypatch.setattr(web.module, 'admin_auth', s.identity.auth)
        query = urlencode(signed_args())

    def serve_local_app(environ, start_response):
        if environ['HTTP_HOST'].split(':')[0] == 'yclients.ru':
            start_response('200 OK', [('Content-Type', 'text/html')])
            return [f'<iframe src="https://gatebot.test{ROOT}/connect?{escape(query)}"></iframe>'.encode()]
        def send_headers(status, headers, exc_info=None):
            if scenario == 'cookies_blocked':
                headers = [(k, v) for k, v in headers if not (k.lower() == 'set-cookie' and v.startswith('__Secure-gatebot-marketplace='))]
            return start_response(status, headers, exc_info)
        return web.app(environ, send_headers)

    server = make_server('127.0.0.1', 0, serve_local_app, ssl_context='adhoc', threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as pw:
            target = f'127.0.0.1:{server.server_port}'
            # Retain real origins, TLS, browser headers and CHIPS, with both sites on loopback.
            hosts = f'MAP yclients.ru {target}, MAP gatebot.test {target}, MAP * ~NOTFOUND'
            browser = pw.chromium.launch(executable_path=str(executable), args=[
                '--disable-background-networking', '--no-proxy-server', '--host-resolver-rules=' + hosts])
            context = browser.new_context(ignore_https_errors=True)
            inside = context.new_page()
            errors = []
            inside.on('pageerror', lambda error: errors.append(str(error)))
            inside.goto('https://yclients.ru/test-gatebot')
            frame = inside.frame_locator('iframe')
            if scenario == 'register':
                playwright.expect(frame.get_by_role('heading', name='Регистрация', exact=True)).to_be_visible()
                frame.get_by_label('Название филиала').fill('Office')
                frame.get_by_role('button', name='Получить код по SMS').click()
                playwright.expect(frame.locator('#marketplaceCodeStatus')).to_contain_text('Код отправлен')
                code = re.search(r'\b[0-9]{6}\b', s.identity.sent[-1][2]).group()
                frame.get_by_label('Код из SMS', exact=True).fill(code)
                frame.locator('#marketplacePassword').fill(PASSWORD)
                frame.locator('#marketplacePasswordConfirm').fill(PASSWORD)
                frame.locator('#marketplacePasswordSave').click()
                playwright.expect(frame.get_by_role('heading', name='Регистрация завершена')).to_be_visible()
                assert s.identity.branches.get_branch('99').name == 'Office'
                assert not errors
                browser.close()
                return
            if scenario == 'cookies_blocked':
                link = frame.get_by_role('link', name='Открыть в отдельной вкладке')
                playwright.expect(frame.locator('#frameStatus')).to_contain_text('в отдельной вкладке')
            else:
                link = frame.get_by_role('link', name='Войти', exact=True)
                playwright.expect(frame.locator('#marketplaceBranches')).to_contain_text('12')
            with context.expect_page() as opened:
                link.click()
            destination = opened.value
            destination.wait_for_url('**/yclients/marketplace' if scenario == 'cookies_blocked' else '**/admin/login?next=marketplace')
            assert destination.evaluate("fetch('/yclients/marketplace/state').then(r => r.json()).then(s => s.branches[0].id)") == '12'
            browser.close()
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
