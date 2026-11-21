"""Marketplace browser hand-off; all provider and onboarding requests are mocked."""
import os
import time

import pytest
from flask import render_template
from test_web_security import web
from test_ui_browser import browser_ui


pytestmark = pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')


def _state(**changes):
    return dict(auto_onboarding=True, authenticated=False, configured=True, enabled=True,
                expired=False, expires_at=time.time() + 3600, account=None,
                profile={'name':'Owner','salon_name':'Example salon','email':'owner@example.invalid','phone':'+15550100555'},
                branches=[{'id': '456', 'name': 'Example branch', 'state': 'pending', 'accessible': False}], **changes)


def _show(web, page, origin, state):
    with web.app.test_request_context('/yclients/marketplace'):
        document = render_template('marketplace/connect.html', state=state, admin_page=False)
    page.route('**/yclients/marketplace', lambda route: route.fulfill(content_type='text/html', body=document))
    page.goto(origin + '/yclients/marketplace')


@pytest.mark.parametrize('change_phone',[False,True])
def test_auto_install_reconcile_and_password_completion(web, browser_ui, change_phone):
    page, origin, expect, errors = browser_ui
    sent, passwords = [], []

    def onboard(route):
        sent.append(route.request.post_data_json)
        assert route.request.headers.get('x-csrf-token')
        route.fulfill(json={'state': 'unknown'} if len(sent) == 1 else {
            'state': 'active', 'registration_required':True})

    def password(route):
        passwords.append(route.request.post_data_json)
        assert route.request.headers.get('x-csrf-token')
        route.fulfill(json={'redirect_url': '/admin/integrations'})

    page.route('**/yclients/marketplace/onboard', onboard)
    sent_code=[]
    def sms(route):
        sent_code.append(route.request.post_data_json)
        original=change_phone and len(sent_code)==1
        route.fulfill(json={'ready':True,'retry_after':60,'challenge_id':'original' if original else 'offline-challenge',
                            'phone_hint':'••• 0555','step':'original' if original else 'registration'})
    page.route('**/yclients/marketplace/code', sms)
    page.route('**/yclients/marketplace/register', password)
    page.route('**/admin/integrations', lambda route: route.fulfill(content_type='text/html', body='<h1>Cabinet ready</h1>'))
    _show(web, page, origin, _state())
    expect(page.locator('#marketplaceNotice')).to_contain_text('Ответ пока не подтверждён')
    assert sent == [{'branch_id': '456', 'reconcile': False}]
    expect(page.locator('a[href="/register-branch?marketplace=1"]')).to_have_count(0)
    page.get_by_role('button', name='Проверить состояние', exact=True).click()
    expect(page.locator('#marketplacePasswordForm')).to_be_visible()
    assert sent[-1] == {'branch_id': '456', 'reconcile': True}
    expect(page.locator('#marketplaceName')).to_have_value('Owner')
    expect(page.locator('#marketplaceBranchName')).to_have_value('Example salon')
    page.locator('#marketplaceName').fill('Updated name')
    phone='+15550100666' if change_phone else '+15550100555'
    if change_phone:
        page.locator('#marketplacePhone').fill(phone)
        expect(page.locator('#marketplacePhoneChange')).to_be_visible()
        assert not sent_code
    page.locator('#agree-admin-privacy').check();page.locator('#agree-terms').check()
    documents=page.evaluate('window.registrationChoices()')
    page.locator('#marketplaceSendCode').click()
    if change_phone:
        expect(page.locator('#marketplacePasswordSave')).to_be_disabled()
        expect(page.locator('#marketplaceCodeStatus')).to_contain_text('из YCLIENTS')
        page.locator('#marketplaceCode').fill('654321')
        page.locator('#marketplaceVerifyOriginal').click()
    expect(page.locator('#marketplaceCodeStatus')).to_contain_text('Код отправлен')
    expect(page.locator('#marketplaceSendCode')).to_be_disabled()
    assert sent_code[0] == {'branch_id':'456','phone':phone,'documents':documents}
    if change_phone:
        assert sent_code[1] == {**sent_code[0],'original_challenge':'original','original_code':'654321'}
    page.locator('#marketplaceCode').fill('123456')
    page.locator('#marketplacePassword').fill('Offline-example-password')
    page.locator('#marketplacePasswordConfirm').fill('Different-example-password')
    page.locator('#marketplacePasswordSave').click()
    expect(page.locator('#marketplaceNotice')).to_contain_text('Пароли не совпадают')
    assert passwords == []
    page.locator('#marketplacePasswordConfirm').fill('Offline-example-password')
    page.locator('#marketplacePasswordSave').click()
    page.wait_for_url('**/admin/integrations')
    assert len(passwords)==1 and passwords[0]['profile']['name']=='Updated name'
    assert passwords[0]['challenge']=='offline-challenge' and passwords[0]['code']=='123456'
    assert len(sent) == 2 and web.calls == []


def test_multiple_branches_require_choice_and_existing_account_requires_login(web, browser_ui):
    page, origin, expect, errors = browser_ui
    sent = []

    def onboard(route):
        sent.append(route.request.post_data_json)
        route.fulfill(json={'state': 'active', 'account': {'username': 'existing@example.invalid',
                                                         'password_setup_required': False, 'can_set_password': False}})

    page.route('**/yclients/marketplace/onboard', onboard)
    state = _state()
    state['branches'].append({'id': '457', 'name': 'Second example', 'state': 'pending', 'accessible': False})
    _show(web, page, origin, state)
    expect(page.get_by_role('button', name='Подключить этот филиал', exact=True)).to_have_count(2)
    page.wait_for_load_state('networkidle')
    assert sent == []
    page.locator('#marketplaceBranches section').nth(1).get_by_role('button', name='Подключить этот филиал', exact=True).click()
    expect(page.locator('#marketplaceAccountLink')).to_be_visible()
    expect(page.locator('#marketplaceAccountLink')).to_have_attribute('href', '/admin/login?next=marketplace')
    expect(page.locator('#marketplacePasswordForm')).to_be_hidden()
    expect(page.locator('#marketplaceBranches section').first.get_by_role('button', name='Подключить этот филиал', exact=True)).to_be_disabled()
    assert sent == [{'branch_id': '457', 'reconcile': False}] and web.calls == []


def test_existing_authenticated_branch_uses_scoped_connection(web, browser_ui):
    page, origin, expect, errors = browser_ui
    sent, automatic = [], []

    def connect(route):
        sent.append(route.request.post_data_json)
        route.fulfill(json={'state': 'active'})

    page.route('**/yclients/marketplace/connect', connect)
    page.route('**/yclients/marketplace/onboard', lambda route: automatic.append(route.request.post_data_json) or route.fulfill(json={'state': 'rejected'}))
    state = _state()
    state['authenticated'] = True
    state['branches'][0].update(accessible=True, pending_verification=False, state='unknown')
    _show(web, page, origin, state)
    expect(page.locator('#marketplaceAccountLink')).to_have_attribute('href', '/admin/integrations')
    expect(page.locator('#marketplacePasswordForm')).to_be_hidden()
    assert sent == [{'branch_id': '456', 'reconcile': True}]
    assert automatic == [] and web.calls == []


@pytest.mark.parametrize('blocked', ['disabled', 'expired'])
def test_auto_install_respects_disabled_and_expired_handoff(web, browser_ui, blocked):
    page, origin, expect, errors = browser_ui
    sent = []
    page.route('**/yclients/marketplace/onboard', lambda route: sent.append(route.request.post_data_json) or route.fulfill(json={'state': 'active'}))
    state = _state()
    state['enabled'] = blocked != 'disabled'
    state['expired'] = blocked == 'expired'
    _show(web, page, origin, state)
    expect(page.get_by_role('button', name='Подключить', exact=True)).to_be_disabled()
    page.wait_for_load_state('networkidle')
    assert sent == [] and web.calls == []
