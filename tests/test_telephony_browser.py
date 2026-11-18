"""Optional real-browser regression; synthetic private state and disabled hardware.

Set GATEBOT_BROWSER_TESTS=1; install Playwright and provide Chrome/Edge locally.
No browser download is required. Ordinary CI can run the API/serial regressions.
"""
import os
from pathlib import Path
import threading

import pytest

from test_web_security import web, TEST_PASSWORD


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')
def test_separate_call_and_sms_screens_chats_and_history_clear(web, monkeypatch, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    browser_path = next((p for p in (
        Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),
        Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'),
    ) if p.exists()), None)
    if browser_path is None:
        pytest.skip('No local Chrome/Edge browser')
    import sim800c
    from telephony_store import TelephonyStore
    from werkzeug.serving import make_server
    queue = sim800c.CallQueue(enabled=False, dry_run=True)
    monkeypatch.setattr(sim800c, '_queue', queue)
    store = TelephonyStore()
    phone=web.links.load_links()[web.token_a]['phone']
    web.links.update_user(web.token_a,{'user_name':'Анна Соколова'})
    store.add('sms', 'incoming', phone=phone, text='Пример <img src=x onerror=alert(1)>', status='received')
    store.add('sms', 'outgoing', phone=phone, text='', action='sms_delete', status='sim_cleanup_complete')
    store.add('call', 'incoming', phone=phone, status='ended')
    server = make_server('127.0.0.1', 0, web.app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = 'http://127.0.0.1:' + str(server.server_port)
    errors = []
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(executable_path=str(browser_path), headless=True,
                args=['--disable-background-networking', '--disable-component-update', '--no-first-run'])
            page = browser.new_page(viewport={'width':1360,'height':900})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('dialog', lambda dialog: dialog.accept())
            page.goto(origin + '/admin/login')
            page.locator('[name=username]').fill('super')
            page.locator('[name=password]').fill(TEST_PASSWORD)
            page.locator('button[type=submit]').click()
            page.wait_for_url('**/admin/users')
            page.goto(origin + '/admin/telephony')
            playwright.expect(page.locator('#chatList .phone-chat')).to_have_count(1)
            playwright.expect(page.locator('#chatList')).to_contain_text('Анна Соколова')
            page.locator('#chatList .phone-chat').click()
            playwright.expect(page.locator('#chatMessages .phone-message')).to_have_count(1)
            playwright.expect(page.locator('#chatMessages img')).to_have_count(0)
            page.locator('#smsForm textarea').fill('Проверка кириллицы. ' * 20)
            page.locator('#smsForm button[type=submit]').click()
            playwright.expect(page.locator('#chatMessages .phone-message')).to_have_count(2)
            playwright.expect(page.locator('#chatMessages')).to_contain_text('Имитация')
            page.screenshot(path=str(tmp_path / 'sms-chats.png'), full_page=True)
            # An individual delete affects only that message, with a focused dialog.
            page.locator('#chatMessages .phone-message').first.hover()
            page.locator('#chatMessages .message-action').first.click()
            page.get_by_role('menuitem',name='Удалить сообщение',exact=True).click()
            page.locator('#confirmDelete').click()
            playwright.expect(page.locator('#chatMessages .phone-message')).to_have_count(1)
            # Preserve drafts when switching between new/existing conversations.
            page.locator('#smsForm textarea').fill('Черновик')
            page.locator('#newChatOpen').click()
            page.locator('#newChatPhone').fill('+1234567')
            page.locator('#newChatForm button[type=submit]').click()
            page.locator('#chatList .phone-chat').click()
            playwright.expect(page.locator('#smsForm textarea')).to_have_value('Черновик')
            page.locator('#threadMore').click();page.locator('#clearChat').click();page.locator('#confirmDelete').click()
            playwright.expect(page.locator('#chatList .phone-chat')).to_have_count(0)
            page.locator('#modeCalls').click()
            playwright.expect(page.locator('#smsPanel')).to_be_hidden()
            playwright.expect(page.locator('#callList article')).to_have_count(1)
            playwright.expect(page.locator('#callList')).to_contain_text('Анна Соколова')
            page.screenshot(path=str(tmp_path / 'calls.png'),full_page=True)
            # Small screen navigation: list -> thread -> back, composer stays visible.
            store.add('sms','incoming',phone=phone,text='Проверка мобильного экрана',status='received')
            page.set_viewport_size({'width':390,'height':844})
            page.locator('#modeSms').click()
            playwright.expect(page.locator('#chatList .phone-chat')).to_have_count(1)
            page.locator('#chatList .phone-chat').click()
            playwright.expect(page.locator('.sidebar')).to_be_hidden()
            playwright.expect(page.locator('#smsForm textarea')).to_be_visible()
            playwright.expect(page.locator('#chatMessages')).to_contain_text('Проверка мобильного экрана')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(tmp_path / 'sms-mobile.png'),full_page=True)
            page.locator('#backToChats').click()
            playwright.expect(page.locator('.sidebar')).to_be_visible()
            page.locator('#modemStatus').click()
            playwright.expect(page.locator('#detailsDialog')).to_be_visible()
            page.keyboard.press('Escape')
            page.locator('#sidebarMore').click();page.locator('#clearAllSms').click();page.locator('#confirmDelete').click()
            playwright.expect(page.locator('#chatList .phone-chat')).to_have_count(0)
            assert not errors
            assert web.calls == []
            browser.close()
    finally:
        server.shutdown()
        thread.join(3)
        queue.shutdown()
