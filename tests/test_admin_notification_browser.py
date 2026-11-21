"""Offline browser acceptance of selected versus actually connected channels."""
import os

import pytest

from test_ui_browser import browser_ui, sign_in
from test_web_security import web


pytestmark = pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1',
                                reason='Opt-in browser acceptance')


def test_event_filter_read_all_and_branch_pause_across_client_pages(web, browser_ui, tmp_path, monkeypatch):
    from storage import config
    from storage.sql_documents import DocumentStore, migrate_legacy
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path/'admins.json'))
    monkeypatch.setattr(config,'CLIENT_OUTBOUND_ENABLED',True)
    services = web.module.product_services()
    services.identity.attach_console_account('tenant-a','+15550300101')
    page, origin, expect, _ = browser_ui
    sign_in(page, origin, 'tenant-a')
    page.goto(origin+'/admin/notification-settings')
    check = page.locator('input[name=quota_warning]')
    check.uncheck()
    page.get_by_role('button',name='Сохранить',exact=True).click()
    expect(page.locator('#identityNotice')).to_contain_text('сохранены')
    page.reload()
    expect(check).not_to_be_checked()
    check.check()
    page.get_by_role('button',name='Сохранить',exact=True).click()
    expect(page.locator('#identityNotice')).to_contain_text('сохранены')
    services.notifications.event('quota_warning','12',{'event_id':'browser-quota','quota':100,'spent':81})
    page.goto(origin+'/admin/notifications')
    expect(page.locator('#readAllNotifications')).to_be_visible()
    page.locator('#readAllNotifications').click()
    expect(page.locator('#readAllNotifications')).to_be_disabled()
    page.reload()
    page.locator('#showUnreadNotifications').click()
    expect(page.get_by_text('Всё прочитано',exact=True)).to_be_visible()
    page.goto(origin+'/admin/client-notices')
    switch = page.get_by_role('switch')
    expect(switch).to_be_checked()
    switch.click()
    expect(switch).not_to_be_checked()
    for path in ('/preferences/'+web.token_a,'/pwa/'+web.token_a+'/preferences'):
        page.goto(origin+path)
        expect(page.locator('#clientMailingState')).to_have_text('Уведомления отключены администратором')
        expect(page.locator('#clientChannel-email')).to_be_disabled()
    page.goto(origin+'/admin/users')
    page.get_by_role('button',name='Уведомления: Client A',exact=True).click()
    expect(page.locator('.client-notifications-modal')).to_contain_text('Уведомления отключены администратором')
    expect(page.locator('#admin-client-channel-email')).to_be_disabled()
    page.goto(origin+'/admin/client-notices')
    page.get_by_role('switch').click()
    page.goto(origin+'/preferences/'+web.token_a)
    expect(page.locator('#clientChannel-email')).to_be_enabled()


def test_unbound_max_preference_is_honest_and_survives_save(web, browser_ui, tmp_path, monkeypatch):
    from storage.sql_documents import DocumentStore, migrate_legacy

    page, origin, expect, errors = browser_ui
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    identity = web.module.product_services().identity
    identity.attach_console_account('super', '+15550300303')
    identity.save_admin_preferences('super', {'events': {'sms_received': {'max': True}}})
    sign_in(page, origin)
    page.goto(origin + '/admin/notification-settings')
    card = page.locator('#adminNotificationChannels [data-channel=max]')
    expect(card).to_contain_text('Аккаунт не подключён')
    expect(card.get_by_role('button', name='Подключить MAX')).to_be_visible()
    expect(page.locator('[name=sms_received_max]')).to_be_checked()
    expect(page.locator('[name=sms_received_max]')).to_be_disabled()
    expect(page.locator('[name=quota_warning_telegram]')).to_be_disabled()
    expect(page.locator('#adminNotificationUnavailable')).to_contain_text('MAX')
    page.get_by_role('button', name='Сохранить', exact=True).click()
    expect(page.locator('#identityNotice')).to_contain_text('нужно завершить подключение')
    assert identity.admin_preferences('super')['events']['sms_received']['max'] is True

    ticket = identity.issue_bind_ticket('super', 'max')
    identity.bind_ticket('max', '92', ticket)
    page.evaluate("window.dispatchEvent(new Event('focus'))")
    expect(card).to_contain_text('Аккаунт подключён')
    expect(card.get_by_role('button', name='Подключить MAX')).to_be_hidden()
    expect(page.locator('#adminNotificationUnavailable')).to_be_hidden()
    expect(page.locator('[name=sms_received_max]')).to_be_checked()
    expect(page.locator('[name=sms_received_max]')).to_be_enabled()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert web.calls == []


@pytest.mark.parametrize('username', ['super', 'tenant-a'])
def test_profile_email_connects_in_card_and_disconnected_switches_preserve_choices(web, browser_ui, tmp_path, monkeypatch, username):
    from storage.sql_documents import DocumentStore, migrate_legacy

    page, origin, expect, errors = browser_ui
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    identity = web.module.product_services().identity
    phone = web.links.load_links()[web.token_a]['phone']
    if username != 'super':
        identity.attach_console_account(username, phone, client_token=web.token_a)
    identity.admin_email(username, 'profile@example.invalid')
    identity.save_admin_preferences(username, {'events': {'gate_requested': {'email': True, 'max': True}}})
    ticket = identity.issue_bind_ticket(username, 'telegram')
    identity.bind_ticket('telegram', '92', ticket)
    sign_in(page, origin, username)
    page.goto(origin + '/admin/notification-settings')

    email_card = page.locator('#adminNotificationChannels [data-channel=email]')
    expect(page.locator('#adminNotificationChannels .stat-card')).to_have_count(4)
    expect(page.get_by_role('heading', name='Email для уведомлений')).to_have_count(0)
    expect(email_card).to_contain_text('profile@example.invalid')
    expect(email_card).to_contain_text('Адрес подключён')
    expect(email_card.get_by_role('button', name='Изменить email')).to_be_visible()
    expect(page.locator('[name=gate_requested_email]')).to_be_checked()
    expect(page.locator('[name=gate_requested_email]')).to_be_enabled()
    expect(page.locator('[name=gate_requested_telegram]')).to_be_enabled()
    expect(page.locator('[name=gate_requested_max]')).to_be_disabled()

    # An in-card email edit must keep an unsaved event choice in the table.
    # A slow refresh started before the edit must not restore the previous address.
    page.evaluate("""() => {
        const previous = JSON.parse(document.getElementById('identityBootstrap').textContent).data.preferences;
        const originalFetch = window.fetch;
        window.fetch = (url, options) => {
            if (url === '/admin/api/identity/notification-settings' && (!options?.method || options.method === 'GET')) {
                return new Promise(resolve => {
                    window.releaseConnectionRefresh = () => {
                        window.fetch = originalFetch;
                        resolve(new Response(JSON.stringify({preferences: previous}), {status: 200, headers: {'Content-Type': 'application/json'}}));
                    };
                });
            }
            return originalFetch(url, options);
        };
        window.dispatchEvent(new Event('focus'));
    }""")
    page.locator('[name=gate_error_telegram]').check()
    email_card.get_by_role('button', name='Изменить email').click()
    expect(email_card.get_by_label('Email', exact=True)).to_have_value('profile@example.invalid')
    email_card.get_by_label('Email', exact=True).fill('edited@example.invalid')
    email_card.get_by_role('button', name='Сохранить email', exact=True).click()
    expect(page.locator('#identityNotice')).to_contain_text('Email сохранён')
    page.evaluate('window.releaseConnectionRefresh()')
    expect(email_card).to_contain_text('edited@example.invalid')
    expect(page.locator('[name=gate_error_telegram]')).to_be_checked()
    assert web.links.load_links()[web.token_a]['email'] == ('' if username=='super' else 'edited@example.invalid')
    expect(page.locator('[name=gate_requested_email]')).to_be_enabled()

    # Clear only the email contact, preserve the disabled selected event on save.
    email_card.get_by_role('button', name='Изменить email').click()
    email_card.get_by_label('Email', exact=True).fill('')
    email_card.get_by_role('button', name='Сохранить email', exact=True).click()
    expect(email_card.get_by_role('button', name='Подключить Email')).to_be_visible()
    expect(page.locator('[name=gate_requested_email]')).to_be_disabled()
    expect(page.locator('[name=gate_requested_email]')).to_be_checked()
    page.get_by_role('button', name='Сохранить', exact=True).click()
    expect(page.locator('#identityNotice')).to_contain_text('Настройки сохранены')
    saved = identity.admin_preferences(username)
    assert saved['email'] == '' and saved['channels']['email'] is False
    assert saved['events']['gate_requested']['email'] is True
    assert saved['events']['gate_requested']['max'] is True
    assert saved['events']['gate_error']['telegram'] is True

    email_card.get_by_role('button', name='Подключить Email').click()
    email_card.get_by_label('Email', exact=True).fill('restored@example.invalid')
    email_card.get_by_role('button', name='Отмена', exact=True).click()
    expect(page.locator('#adminNotificationEmailForm')).to_be_hidden()
    assert web.links.load_links()[web.token_a]['email'] == ''
    email_card.get_by_role('button', name='Подключить Email').click()
    email_card.get_by_label('Email', exact=True).fill('restored@example.invalid')
    email_card.get_by_role('button', name='Сохранить email', exact=True).click()
    expect(page.locator('[name=gate_requested_email]')).to_be_enabled()
    expect(page.locator('[name=gate_requested_email]')).to_be_checked()
    page.reload()
    expect(email_card).to_contain_text('restored@example.invalid')
    expect(page.locator('[name=gate_requested_email]')).to_be_enabled()
    page.set_viewport_size({'width': 390, 'height': 844})
    email_card.get_by_role('button', name='Изменить email').click()
    expect(email_card.get_by_label('Email', exact=True)).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / ('notification-channels-' + username + '.png')), full_page=True)
    assert web.calls == []
