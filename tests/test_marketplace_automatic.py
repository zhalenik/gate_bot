"""Automatic marketplace installation, without real CRM, SMS or email effects."""
import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from werkzeug.datastructures import MultiDict

from test_identity_product import identities, PASSWORD
from test_yclients_marketplace import marketplace
from storage.sql_documents import read_document, write_document
from yclients_marketplace import MarketplaceAPI, MarketplaceError


PROFILE = {'name': 'Marketplace owner', 'phone': '+15550100444',
           'email': 'marketplace@example.invalid', 'salon_name': 'Marketplace salon'}


def signed_args(branch='99', profile=None):
    raw = json.dumps(profile or PROFILE, ensure_ascii=False, separators=(',', ':')).encode()
    return MultiDict({'salon_id': branch, 'user_data': base64.b64encode(raw).decode(),
                     'user_data_sign': hmac.new(b'offline-partner-key', raw, hashlib.sha256).hexdigest()})


def auto_begin(state):
    state.api.verify_user.return_value = True
    return state.service.begin(signed_args(), state.browser)


def test_signed_install_creates_active_branch_admin_client_trial_and_system_credentials(marketplace):
    s = marketplace
    raw = auto_begin(s)
    assert s.service.view(raw, s.browser)['auto_onboarding']
    s.api.install.assert_not_called()  # GET hand-off itself has no external effect.
    result = s.service.onboard(raw, s.browser, '99')
    assert result['state'] == 'active' and result['account']['can_set_password']
    branch = s.identity.branches.load_branches()['99']
    assert branch['enabled'] and branch['onboarding_status'] == 'active'
    assert branch['buttons'] == [] and not branch['hardware_routes_verified']
    assert s.identity.billing.subscription_view('99', at=s.identity.clock[0])['available']
    credentials = s.identity.service.integration_credentials('99', 'yclients')
    assert credentials == {'user_token': 'offline-system-user-token', 'partner_token': 'offline-partner-key'}
    admin = s.identity.auth.get_admin(PROFILE['email'])
    assert not admin['is_super'] and admin['branch_id'] == 99
    clients = [row for row in s.identity.links.load_links().values() if row['branch_id'] == 99]
    assert len(clients) == 1 and clients[0]['admin_role_active'] and clients[0]['email'] == PROFILE['email']
    assert not s.identity.sent
    assert s.api.install.call_args.args == ('99',)
    assert s.api.verify_user.call_args.args == ('99', PROFILE, 'offline-system-user-token')
    s.service.onboard(raw, s.browser, '99')
    assert s.api.install.call_count == 1 and s.api.verify_user.call_count == 1


def test_new_password_requires_original_browser_and_cannot_reset_existing_account(marketplace):
    s = marketplace
    raw = auto_begin(s)
    s.service.onboard(raw, s.browser, '99')
    another = auto_begin(s)
    result = s.service.onboard(another, s.browser, '99')
    assert not result['account']['can_set_password']
    with pytest.raises(PermissionError):
        s.service.set_onboarding_password(another, s.browser, PASSWORD, PASSWORD)
    with pytest.raises(MarketplaceError):
        s.service.set_onboarding_password(raw, 'other-browser', PASSWORD, PASSWORD)
    assert s.service.set_onboarding_password(raw, s.browser, PASSWORD, PASSWORD) == PROFILE['email']
    assert s.identity.auth.verify_admin(PROFILE['email'], PASSWORD)
    with pytest.raises(PermissionError):
        s.service.set_onboarding_password(raw, s.browser, PASSWORD, PASSWORD)
    assert not s.service.view(raw, s.browser)['account']['can_set_password']
    assert s.api.install.call_count == 1


def test_wrong_company_profile_never_creates_branch_or_local_rights(marketplace):
    s = marketplace
    raw = auto_begin(s)
    s.api.verify_user.side_effect = MarketplaceError('User not in company')
    with pytest.raises(MarketplaceError):
        s.service.onboard(raw, s.browser, '99')
    assert s.identity.branches.get_branch('99') is None
    assert PROFILE['email'] not in s.identity.auth.load_admins()
    assert s.identity.service.integration_credentials('99', 'yclients') == {}
    # Fixing the verification failure checks the installation; no second POST.
    s.api.verify_user.side_effect = None
    assert s.service.onboard(raw, s.browser, '99', reconcile=True)['state'] == 'active'
    assert s.api.install.call_count == 1 and s.api.status.call_count == 1


def test_unknown_install_reconciles_by_get_and_keeps_local_state_uncreated(marketplace):
    s = marketplace
    raw = auto_begin(s)
    s.api.install.return_value = {'state': 'unknown', 'code': 'network_result_unknown'}
    assert s.service.onboard(raw, s.browser, '99')['state'] == 'unknown'
    assert s.identity.branches.get_branch('99') is None
    assert s.service.onboard(raw, s.browser, '99', reconcile=True)['state'] == 'active'
    assert s.api.install.call_count == 1 and s.api.status.call_count == 1


@pytest.mark.parametrize('during', ['install', 'verify_user'])
def test_uninstall_wins_before_local_branch_even_exists(marketplace, during):
    s = marketplace
    raw = auto_begin(s)
    def disconnect(*args):
        s.service.disconnect({'salon_id': 99, 'application_id': 123,
                              'partner_token': 'offline-partner-key', 'event': 'uninstall'})
        return {'state': 'active'} if during == 'install' else True
    getattr(s.api, during).side_effect = disconnect
    assert s.service.onboard(raw, s.browser, '99')['code'] == 'connection_changed'
    assert s.identity.branches.get_branch('99') is None
    assert PROFILE['email'] not in s.identity.auth.load_admins()


def test_signed_old_pending_marketplace_branch_activates_without_super_review(marketplace):
    s = marketplace
    raw = auto_begin(s)
    s.service.onboard(raw, s.browser, '99')
    s.service.set_onboarding_password(raw, s.browser, PASSWORD, PASSWORD)
    with s.service.store.connect() as db:
        branches = read_document(db, 'branches.json')
        branches['99'].update(enabled=False, onboarding_status='pending_verification')
        write_document(db, 'branches.json', branches)
    result = s.service.onboard(auto_begin(s), s.browser, '99')
    assert result['state'] == 'active' and not result['account']['can_set_password']
    assert s.identity.branches.get_branch('99').enabled
    assert s.identity.auth.verify_admin(PROFILE['email'], PASSWORD)


def test_invalid_missing_expired_and_foreign_handoffs_never_install(marketplace):
    s = marketplace
    raw = s.service.begin(MultiDict({'salon_id': '99'}), s.browser)
    with pytest.raises(MarketplaceError): s.service.onboard(raw, s.browser, '99')
    raw = auto_begin(s)
    with pytest.raises(PermissionError): s.service.onboard(raw, s.browser, '98')
    with pytest.raises(MarketplaceError): s.service.onboard(raw, 'other', '99')
    s.service.enabled = False
    with pytest.raises(MarketplaceError): s.service.onboard(raw, s.browser, '99')
    s.service.enabled = True
    s.identity.clock[0] += 3601
    with pytest.raises(MarketplaceError): s.service.onboard(raw, s.browser, '99')
    s.api.install.assert_not_called()


def test_active_admin_cannot_acquire_second_branch_by_signed_profile(marketplace):
    s = marketplace
    s.service.onboard(auto_begin(s), s.browser, '99')
    token = s.service.begin(signed_args('98'), s.browser)
    with pytest.raises(PermissionError): s.service.onboard(token, s.browser, '98')
    assert s.identity.branches.get_branch('98') is None
    assert s.api.install.call_count == 1


@pytest.mark.parametrize('state', ['uninstalled', 'frozen'])
def test_automatic_reconcile_stops_reader_and_preserves_local_access(marketplace, state):
    s = marketplace
    raw = auto_begin(s)
    s.service.onboard(raw, s.browser, '99')
    links = s.identity.links.load_links()
    s.api.status.return_value = {'state': state}
    assert s.service.onboard(raw, s.browser, '99', reconcile=True)['state'] == state
    assert s.identity.branches.load_branches()['99']['yclients_marketplace_state'] == state
    assert s.identity.service.integration_credentials('99', 'yclients') == {}
    assert s.identity.links.load_links() == links and s.identity.branches.get_branch('99').enabled


def test_password_reset_invalidates_original_first_password_grant(marketplace):
    s = marketplace
    raw = auto_begin(s)
    s.service.onboard(raw, s.browser, '99')
    with s.service.store.connect() as db:
        admins = read_document(db, 'admins.json')
        admins[PROFILE['email']]['session_version'] += 1
        write_document(db, 'admins.json', admins)
    with pytest.raises(PermissionError):
        s.service.set_onboarding_password(raw, s.browser, PASSWORD, PASSWORD)


def test_first_onboarding_cannot_skip_consent_install_by_requesting_reconcile(marketplace):
    s = marketplace
    raw = auto_begin(s)
    with pytest.raises(MarketplaceError): s.service.onboard(raw, s.browser, '99', reconcile=True)
    s.api.status.assert_not_called()
    s.api.install.assert_not_called()


def test_provider_users_wire_contract_and_unapproved_or_mismatched_contacts():
    transport = Mock()
    user = {**PROFILE, 'id': 123, 'is_approved': True}
    transport.request.return_value = SimpleNamespace(status_code=200, json=lambda: {'success': True, 'data': [user]})
    api = MarketplaceAPI('offline-partner-key', '123', session=transport)
    assert api.verify_user('99', PROFILE, 'offline-system-user-token') is True
    args, kwargs = transport.request.call_args
    assert args == ('GET', 'https://api.yclients.ru/api/v1/company/99/users')
    assert kwargs['headers']['Authorization'] == 'Bearer offline-partner-key, User offline-system-user-token'
    assert kwargs['verify'] is True and kwargs['allow_redirects'] is False
    for changes in ({'is_approved': False}, {'phone': '+15550100666'}, {'email': 'different@example.invalid'}):
        user.update(PROFILE, is_approved=True)
        user.update(changes)
        with pytest.raises(MarketplaceError): api.verify_user('99', PROFILE, 'offline-system-user-token')


def test_real_app_automatic_post_is_csrf_bound_and_first_password_logs_in(marketplace, monkeypatch):
    import app as module
    s = marketplace
    s.api.verify_user.return_value = True
    monkeypatch.setattr(module, 'product_services', lambda: SimpleNamespace(identity=s.identity.service, provider=lambda auth, access=None:
        {'service': s.identity.service, 'auth': auth, 'marketplace': s.service,
         'branches': s.identity.branches, 'billing': s.identity.billing}))
    monkeypatch.setattr(module, 'admin_auth', s.identity.auth)
    monkeypatch.setattr(module, 'branch_manager', s.identity.branches)
    monkeypatch.setattr(module, 'link_manager', s.identity.links)
    client = module.app.test_client()
    response = client.get('/yclients/marketplace/connect', query_string=signed_args())
    assert response.status_code == 303 and 'user_data' not in response.location
    page = client.get(response.location)
    assert page.status_code == 200
    with client.session_transaction() as session:
        csrf = session['csrf_token']
    assert client.post('/yclients/marketplace/onboard', json={'branch_id': '99'}).status_code == 403
    result = client.post('/yclients/marketplace/onboard', json={'branch_id': '99'}, headers={'X-CSRF-Token': csrf})
    assert result.status_code == 200 and result.json['state'] == 'active'
    # Another browser receives no first-password grant or administrative session.
    assert module.app.test_client().post('/yclients/marketplace/password', json={'password': PASSWORD, 'confirm': PASSWORD}).status_code in (400, 403)
    bad = client.post('/yclients/marketplace/password', json={'password': PASSWORD, 'confirm': 'mismatch'}, headers={'X-CSRF-Token': csrf})
    assert bad.status_code == 400
    result = client.post('/yclients/marketplace/password', json={'password': PASSWORD, 'confirm': PASSWORD}, headers={'X-CSRF-Token': csrf})
    assert result.status_code == 200 and result.json['redirect_url'] == '/admin/integrations'
    with client.session_transaction() as session:
        assert session['admin_username'] == PROFILE['email'] and session['admin_logged_in']
        assert 'yc_marketplace_intent' not in session
    assert not s.identity.sent
