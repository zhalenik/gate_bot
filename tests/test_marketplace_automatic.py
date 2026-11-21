"""Automatic marketplace installation, without real CRM, SMS or email effects."""
from registration_samples import agreements
import base64
import hashlib
import hmac
import json
import re
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
    raw = json.dumps(profile if profile is not None else {**PROFILE, 'user_id': 112},
                     ensure_ascii=False, separators=(',', ':')).encode()
    return MultiDict({'salon_id': branch, 'user_data': base64.b64encode(raw).decode(),
                     'user_data_sign': hmac.new(b'offline-partner-key', raw, hashlib.sha256).hexdigest()})


def auto_begin(state):
    state.api.status.side_effect = lambda *args, **kwargs: (
        {'state': 'active', 'installer_matches': True} if state.api.install.called else {'state': 'pending'})
    return state.service.begin(signed_args(), state.browser)


def sms(s, raw, profile=PROFILE):
    challenge = s.service.request_registration_code(raw, s.browser, '99', profile['phone'], 'local-test', documents=agreements(s.service.identity))
    assert challenge['ready']
    code = re.search(r'\b[0-9]{6}\b', s.identity.sent[-1][2]).group()
    return challenge['challenge_id'], code


def finish(s, raw, profile=PROFILE):
    challenge, code = sms(s, raw, profile)
    return s.service.complete_registration(raw, s.browser, '99', profile, challenge, code, PASSWORD, PASSWORD, documents=agreements(s.service.identity))


@pytest.mark.parametrize('local_legacy_attempt,provider_pending,field',
                         [(True, False, 'id'), (False, False, 'user_id'), (False, True, 'id')])
def test_signed_installer_completes_without_directory_permission(marketplace, local_legacy_attempt, provider_pending, field):
    s = marketplace
    if local_legacy_attempt:
        # alpha.15 has installed the app but failed its directory check.
        with s.service.store.connect() as db:
            db.execute('INSERT INTO marketplace_branches VALUES(?, "active", 0, ?)', ('99', s.identity.clock[0]))
    get_calls = []
    history = {'logs': [{'source': 'marketplace', 'status_from': 'uninstalled', 'status_to': 'active',
                         'user_id': 112, 'changed_at': '2026-10-01 11:20:45'}],
               'connection_status': {'salon_id': 99, 'marketplace_application_id': 123, 'status': 'active'}}
    def request(method, url, **kwargs):
        if method == 'POST':
            assert provider_pending and url.endswith('/marketplace/partner/callback')
            return SimpleNamespace(status_code=201)
        assert url.endswith('/marketplace/salon/99/application/123')
        get_calls.append(url)
        data = history
        if provider_pending and len(get_calls) == 1:
            data = {'connection_status': {'status': 'pending'}}
        return SimpleNamespace(status_code=200, json=lambda: {'success': True, 'data': data})
    transport = Mock()
    transport.request.side_effect = request
    s.service.api = MarketplaceAPI('offline-partner-key', '123', session=transport, installation_enabled=True)
    raw = s.service.begin(signed_args(profile={**PROFILE, field: 112}), s.browser)
    result = s.service.onboard(raw, s.browser, '99')
    assert result['state'] == 'active' and result['registration_required']
    assert s.identity.branches.get_branch('99') is None
    finish(s, raw)
    assert s.identity.branches.get_branch('99').enabled
    assert transport.request.call_count == (3 if provider_pending else 1)


def test_signed_user_with_wrong_installation_never_gets_role(marketplace):
    s = marketplace
    args = signed_args(profile={**PROFILE, 'user_id': 27})
    args['user_id'] = '999'  # untrusted, outside user_data
    raw = s.service.begin(args, s.browser)
    s.api.status.return_value = {'state': 'active', 'installer_matches': False}
    with pytest.raises(MarketplaceError):
        s.service.onboard(raw, s.browser, '99')
    s.api.status.assert_called_once_with('99', user_id='27')
    s.api.install.assert_not_called()
    assert s.identity.branches.get_branch('99') is None
    with pytest.raises(MarketplaceError):
        s.service.begin(signed_args(profile={**PROFILE, 'id': 11, 'user_id': 12}), s.browser)


def test_disconnect_during_first_status_read_cancels_install(marketplace):
    s = marketplace
    raw = s.service.begin(signed_args(profile={**PROFILE, 'id': 112}), s.browser)
    def status(branch_id, **kwargs):
        s.service.disconnect({'salon_id': branch_id, 'application_id': 123,
                              'partner_token': 'offline-partner-key', 'event': 'uninstall'})
        return {'state': 'uninstalled'}
    s.api.status.side_effect = status
    assert s.service.onboard(raw, s.browser, '99')['code'] == 'connection_changed'
    s.api.install.assert_not_called()


def test_signed_install_creates_active_branch_admin_client_trial_and_system_credentials(marketplace):
    s = marketplace
    events=[]
    s.identity.service.event_sink=lambda *args: events.append(args)
    raw = auto_begin(s)
    assert s.service.view(raw, s.browser)['auto_onboarding']
    s.api.install.assert_not_called()  # GET hand-off itself has no external effect.
    result = s.service.onboard(raw, s.browser, '99')
    assert result['state'] == 'active' and result['registration_required']
    assert not events
    assert s.identity.branches.get_branch('99') is None
    finish(s, raw)
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
    assert len(s.identity.sent) == 1 and s.identity.sent[0][0] == 'sms'
    assert s.api.install.call_args.args == ('99',)
    assert s.api.status.call_args.kwargs == {'user_id': '112'}
    s.service.onboard(raw, s.browser, '99')
    assert s.api.install.call_count == 1 and s.api.status.call_count == 2
    assert [event[:2] for event in events] == [('branch_registered','99')]
    assert 'YCLIENTS' in events[0][2]['message']


def test_sms_bound_to_browser_phone_and_branch_preserves_edited_owner_on_reconnect(marketplace):
    s = marketplace
    raw = auto_begin(s)
    s.service.onboard(raw, s.browser, '99')
    edited = {**PROFILE, 'salon_name':'Updated salon', 'name':'New name',
              'email':'edited@example.invalid','phone':'+15550100555'}
    original, original_code = sms(s, raw, edited)
    assert s.identity.sent[-1][1] == PROFILE['phone']
    assert s.service.view(raw,s.browser)['branches'][0]['otp']['step']=='original'
    with pytest.raises(ValueError):
        s.service.complete_registration(raw, s.browser, '99', edited, original, original_code, PASSWORD, PASSWORD, documents=agreements(s.service.identity))
    with pytest.raises(ValueError):
        s.service.verify_original_phone(raw, s.browser, '99', '+15550100666', original, original_code)
    with pytest.raises(ValueError):
        s.service.verify_original_phone(raw, s.browser, '99', edited['phone'], original, 'wrong')
    s.service.verify_original_phone(raw,s.browser,'99',edited['phone'],original,original_code)
    challenge, code = sms(s, raw, edited)
    assert s.identity.sent[-1][1] == edited['phone'] and len(s.identity.sent) == 2
    def complete(token=raw, browser=s.browser, profile=edited, value=code):
        return s.service.complete_registration(token, browser, '99', profile, challenge, value, PASSWORD, PASSWORD, documents=agreements(s.service.identity))
    with pytest.raises(MarketplaceError):
        complete(browser='other-browser')
    another = auto_begin(s)
    s.service.onboard(another, s.browser, '99')
    with pytest.raises(ValueError):
        complete(token=another)
    with pytest.raises(ValueError):
        complete(profile=PROFILE)
    with pytest.raises(ValueError):
        complete(value='invalid')
    assert s.service.view(raw,s.browser)['branches'][0]['otp']['challenge_id'] == challenge
    assert s.identity.branches.get_branch('99') is None
    assert not s.identity.service.integration_credentials('99', 'yclients')
    with s.service.store.connect() as db:
        assert db.execute('SELECT attempts FROM identity_otp WHERE id=?', (challenge,)).fetchone()[0] == 2
    assert complete() == edited['email']
    assert s.identity.auth.verify_admin(edited['email'], PASSWORD)
    assert s.identity.branches.load_branches()['99']['name'] == edited['salon_name']
    assert s.identity.auth.get_admin(edited['email'])['phone'] == edited['phone']
    with pytest.raises(PermissionError):
        complete()
    resumed = s.service.onboard(another, s.browser, '99')
    assert resumed['account']['username'] == edited['email'] and not resumed['account']['can_set_password']
    with pytest.raises(PermissionError):
        complete(token=another)
    assert s.api.install.call_count == 1


def test_wrong_company_profile_never_creates_branch_or_local_rights(marketplace):
    s = marketplace
    raw = auto_begin(s)
    s.api.status.side_effect = [
        {'state': 'pending'}, {'state': 'active', 'installer_matches': False},
        {'state': 'active', 'installer_matches': True}]
    with pytest.raises(MarketplaceError):
        s.service.onboard(raw, s.browser, '99')
    assert s.identity.branches.get_branch('99') is None
    assert PROFILE['email'] not in s.identity.auth.load_admins()
    assert s.identity.service.integration_credentials('99', 'yclients') == {}
    # Fixing the verification failure checks the installation; no second POST.
    assert s.service.onboard(raw, s.browser, '99', reconcile=True)['state'] == 'active'
    assert s.api.install.call_count == 1


def test_incomplete_signed_contacts_can_be_completed_after_installer_validation(marketplace):
    s = marketplace
    raw = s.service.begin(signed_args(profile={'id': 112, 'name': 'Owner'}), s.browser)
    s.api.status.return_value = {'state': 'active', 'installer_matches': True}
    assert s.service.view(raw, s.browser)['auto_onboarding']
    assert s.service.onboard(raw, s.browser, '99')['registration_required']
    assert finish(s, raw) == PROFILE['email']


def test_signed_salon_alone_without_installer_or_original_phone_requires_new_redirect(marketplace):
    s = marketplace
    raw = s.service.begin(signed_args(profile={'salon_id':99,'email':'owner@example.invalid'}), s.browser)
    state = s.service.view(raw, s.browser)
    assert state['needs_fresh_redirect'] and not state['auto_onboarding']
    with pytest.raises(MarketplaceError):
        s.service.onboard(raw,s.browser,'99')
    with pytest.raises(MarketplaceError):
        s.service.request_registration_code(raw,s.browser,'99',PROFILE['phone'],'local-test', documents=agreements(s.service.identity))
    assert not s.identity.sent and not s.api.method_calls


def test_unknown_install_reconciles_by_get_and_keeps_local_state_uncreated(marketplace):
    s = marketplace
    raw = auto_begin(s)
    s.api.install.return_value = {'state': 'unknown', 'code': 'network_result_unknown'}
    assert s.service.onboard(raw, s.browser, '99')['state'] == 'unknown'
    assert s.identity.branches.get_branch('99') is None
    assert s.service.onboard(raw, s.browser, '99', reconcile=True)['state'] == 'active'
    assert s.api.install.call_count == 1 and s.api.status.call_count == 2


@pytest.mark.parametrize('during', ['install', 'status'])
def test_uninstall_wins_before_local_branch_even_exists(marketplace, during):
    s = marketplace
    raw = auto_begin(s)
    def disconnect(*args, **kwargs):
        if during == 'status' and not s.api.install.called:
            return {'state': 'pending'}
        s.service.disconnect({'salon_id': 99, 'application_id': 123,
                              'partner_token': 'offline-partner-key', 'event': 'uninstall'})
        return {'state': 'active', 'installer_matches': True}
    getattr(s.api, during).side_effect = disconnect
    assert s.service.onboard(raw, s.browser, '99')['code'] == 'connection_changed'
    assert s.identity.branches.get_branch('99') is None
    assert PROFILE['email'] not in s.identity.auth.load_admins()


def test_signed_old_pending_marketplace_branch_activates_without_super_review(marketplace):
    s = marketplace
    raw = auto_begin(s)
    s.service.onboard(raw, s.browser, '99')
    finish(s, raw)
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
    raw = auto_begin(s)
    s.service.onboard(raw, s.browser, '99')
    finish(s,raw)
    token = s.service.begin(signed_args('98'), s.browser)
    s.service.onboard(token, s.browser, '98')
    with pytest.raises(PermissionError):
        s.service.complete_registration(token, s.browser, '98', PROFILE, 'none', '123456', PASSWORD, PASSWORD, documents=agreements(s.service.identity))
    assert s.identity.branches.get_branch('98') is None
    assert s.api.install.call_count == 1


@pytest.mark.parametrize('state', ['uninstalled', 'frozen'])
def test_automatic_reconcile_stops_reader_and_preserves_local_access(marketplace, state):
    s = marketplace
    raw = auto_begin(s)
    s.service.onboard(raw, s.browser, '99')
    finish(s,raw)
    links = s.identity.links.load_links()
    s.api.status.side_effect = None
    s.api.status.return_value = {'state': state}
    assert s.service.onboard(raw, s.browser, '99', reconcile=True)['state'] == state
    assert s.identity.branches.load_branches()['99']['yclients_marketplace_state'] == state
    assert s.identity.service.integration_credentials('99', 'yclients') == {}
    assert s.identity.links.load_links() == links and s.identity.branches.get_branch('99').enabled


def test_disconnect_and_expiry_block_registration_even_with_correct_sms(marketplace):
    s = marketplace
    raw = auto_begin(s)
    s.service.onboard(raw, s.browser, '99')
    challenge, code = sms(s, raw)
    s.service.disconnect({'salon_id':99,'application_id':123,'partner_token':'offline-partner-key','event':'uninstall'})
    with pytest.raises(MarketplaceError):
        s.service.complete_registration(raw, s.browser, '99', PROFILE, challenge, code, PASSWORD, PASSWORD, documents=agreements(s.service.identity))
    assert s.identity.branches.get_branch('99') is None
    assert not s.identity.service.integration_credentials('99', 'yclients')


def test_first_onboarding_cannot_skip_consent_install_by_requesting_reconcile(marketplace):
    s = marketplace
    raw = auto_begin(s)
    with pytest.raises(MarketplaceError): s.service.onboard(raw, s.browser, '99', reconcile=True)
    s.api.status.assert_not_called()
    s.api.install.assert_not_called()


def test_old_signed_profile_without_identity_never_queries_directory_or_installs(marketplace):
    s = marketplace
    transport = Mock()
    s.service.api = MarketplaceAPI('offline-partner-key', '123', session=transport, installation_enabled=True)
    args = signed_args(profile=PROFILE)
    args['user_id'] = '112'  # A query parameter cannot replace a field in signed data.
    raw = s.service.begin(args, s.browser)
    state = s.service.view(raw, s.browser)
    assert state['needs_fresh_redirect'] and not state['auto_onboarding']
    with pytest.raises(MarketplaceError):
        s.service.onboard(raw, s.browser, '99')
    transport.request.assert_not_called()
    assert s.identity.branches.get_branch('99') is None
    assert PROFILE['email'] not in s.identity.auth.load_admins()


@pytest.mark.parametrize('selected', ['99', '98'])
def test_signed_salon_id_binds_contact_profile_to_one_branch(marketplace, selected):
    s = marketplace
    profile = {**PROFILE, 'salon_id': 99}
    if selected != '99':
        with pytest.raises(MarketplaceError):
            raw = s.service.begin(signed_args(branch=selected, profile=profile), s.browser)
            s.service.onboard(raw, s.browser, selected)
        assert s.api.method_calls == []
        assert s.identity.branches.get_branch(selected) is None
        return
    raw = s.service.begin(signed_args(branch=selected, profile=profile), s.browser)
    s.api.status.return_value = {'state': 'active'}
    result = s.service.onboard(raw, s.browser, selected)
    assert result['state'] == 'active' and result['registration_required']
    finish(s, raw)
    assert s.identity.branches.get_branch(selected).enabled
    s.api.status.assert_called_once_with(selected)
    s.api.install.assert_not_called()


def test_real_app_automatic_post_is_csrf_bound_and_first_password_logs_in(marketplace, monkeypatch):
    import app as module
    s = marketplace
    s.api.status.return_value = {'state': 'active', 'installer_matches': True}
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
    assert result.json['registration_required']
    assert s.identity.branches.get_branch('99') is None
    headers = {'X-CSRF-Token': csrf}
    assert client.post('/yclients/marketplace/code', json={'branch_id':'99','phone':PROFILE['phone']}).status_code == 403
    from legal_documents import ADMIN_DOCS,current
    with s.service.store.connect() as db:
        documents={d['kind']:d['version'] for d in current(db,ADMIN_DOCS)}
    assert client.post('/yclients/marketplace/code', json={'branch_id':'99','phone':PROFILE['phone']}, headers=headers).status_code==400
    sms_result = client.post('/yclients/marketplace/code', json={'documents':documents,'branch_id':'99','phone':PROFILE['phone']}, headers=headers)
    assert sms_result.status_code == 200 and sms_result.json['ready']
    code = re.search(r'\b[0-9]{6}\b', s.identity.sent[-1][2]).group()
    payload = {'documents':documents,'branch_id':'99','profile':PROFILE,'challenge':sms_result.json['challenge_id'],
               'code':code,'password':PASSWORD,'confirm':PASSWORD}
    assert client.post('/yclients/marketplace/register', json={**payload,'confirm':'mismatch'}, headers=headers).status_code == 400
    assert client.post('/yclients/marketplace/register', json={**payload,'code':'wrong'}, headers=headers).status_code == 400
    result = client.post('/yclients/marketplace/register', json=payload, headers=headers)
    assert result.status_code == 200 and result.json['redirect_url'] == '/admin/integrations'
    with client.session_transaction() as session:
        assert session['admin_username'] == PROFILE['email'] and session['admin_logged_in']
        assert 'yc_marketplace_intent' not in session
