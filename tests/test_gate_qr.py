"""Gate QR regression tests: no hardware or external delivery."""
from registration_samples import agreements
import re
from types import SimpleNamespace

import pytest
from flask import Flask, abort, request, session

from access_service import AccessService
from gate_qr_service import DEVICE_SECONDS, SESSION_DEVICE_SECONDS, GateQRService, GateQRUnavailable
from gate_qr_web import create_blueprint
from identity_service import OtpRateLimit
from tests.test_identity_product import identities, PHONE


@pytest.fixture
def qr(identities):
    state = identities
    state.branches.update_settings('11', {'buttons': [
        {'type': 'gate', 'label': 'Entry', 'phone': '+15550200001'},
        {'type': 'gate', 'label': 'Exit', 'phone': '+15550200002'},
        {'type': 'admin', 'label': 'Call administrator', 'phone': '+15550200003'},
    ]})
    calls = []
    access = AccessService(state.links, state.branches,
                           opener=lambda phone, **kwargs: calls.append(phone) or {'status': 'success', 'delivery': 'simulated', 'confirmed': False},
                           cooldown_seconds=20)
    svc = GateQRService(state.service, state.links, state.branches, access)
    gate = state.branches.get_branch('11').buttons[0]
    result = svc.admin_view('north', '11', gate.stable_id, 'create')
    raw = result['url'].rsplit('/', 1)[1]
    return SimpleNamespace(base=state, svc=svc, access=access, calls=calls, raw=raw, gate=gate)


def verify(qr, raw=None, browser='browser-one'):
    raw = raw or qr.raw
    code = qr.svc.request_code(raw, PHONE, browser, '127.0.0.1')
    digits = re.search(r'\b\d{6}\b', qr.base.sent[-1][2]).group()
    return qr.svc.verify_code(raw, browser, code['challenge_id'], digits)


def test_public_identifier_never_grants_access_and_scopes_phone(qr):
    assert qr.svc.state(qr.raw)['state'] == 'phone_required'
    with pytest.raises(PermissionError): qr.svc.open(qr.raw, None, 'request-0000000001')
    assert qr.svc.request_code(qr.raw, '+15550177777', 'browser')['state'] == 'not_found'
    qr.base.links.create_link({'branch_id': 12, 'user_name': 'Other branch', 'phone': '+15550177777'})
    assert qr.svc.request_code(qr.raw, '+15550177777', 'browser')['state'] == 'not_found'
    assert not qr.base.sent and not qr.calls
    assert qr.svc.admin_view('north', '11', qr.gate.stable_id)['url'].endswith(qr.raw)
    with pytest.raises(PermissionError): qr.svc.admin_view('north', '12', qr.gate.stable_id, 'create')


def test_code_verifies_device_shared_access_and_cooldown(qr):
    device = verify(qr)
    assert len(qr.base.sent[-1][2].encode('utf-16-be')) <= 140
    assert qr.base.sent[-1][3]['purpose'] == 'otp'
    assert qr.svc.state(qr.raw, device)['state'] == 'active'
    assert qr.svc.open(qr.raw, device, 'request-0000000002')['status'] == 'success'
    assert qr.svc.open(qr.raw, device, 'request-0000000003')['http_status'] == 429
    assert qr.calls == ['+15550200001']
    with qr.base.product.connect() as db:
        assert not db.execute('SELECT 1 FROM gate_qr_devices WHERE token_hash=?', (device,)).fetchone()
    qr.svc.forget_device(qr.raw, device)
    assert qr.svc.state(qr.raw, device)['state'] == 'phone_required'
    with pytest.raises(PermissionError): qr.svc.open(qr.raw, device, 'request-0000000004')


def test_sms_resend_global_limit_browser_binding_and_attempt_budget(qr):
    challenge = qr.svc.request_code(qr.raw, PHONE, 'one', '127.0.0.1')['challenge_id']
    code = re.search(r'\b\d{6}\b', qr.base.sent[-1][2]).group()
    assert qr.svc.challenge_status(qr.raw, 'one')['retry_after'] == 60
    assert qr.svc.challenge_status(qr.raw, 'other')['challenge_id'] is None
    with pytest.raises(OtpRateLimit): qr.svc.request_code(qr.raw, PHONE, 'other')
    with pytest.raises(ValueError): qr.svc.verify_code(qr.raw, 'other', challenge, code)
    wrong = '000000' if code != '000000' else '111111'
    for _ in range(5):
        with pytest.raises(ValueError): qr.svc.verify_code(qr.raw, 'one', challenge, wrong)
    with pytest.raises(ValueError): qr.svc.verify_code(qr.raw, 'one', challenge, code)
    with qr.base.product.connect() as db:
        assert db.execute('SELECT attempts FROM identity_otp WHERE id=?', (challenge,)).fetchone()[0] == 5
    qr.base.clock[0] += 61
    device = verify(qr)
    assert qr.svc.state(qr.raw, device)['state'] == 'active'


def test_failed_simulated_delivery_never_approves_code(qr):
    def send(channel, phone, text, context):
        qr.base.sent.append((channel, phone, text, context))
        return {'status': 'success', 'delivery': 'simulated'}
    qr.base.service.sender = send
    challenge = qr.svc.request_code(qr.raw, PHONE, 'browser')
    assert challenge['state'] == 'delivery_failed' and not challenge['ready']
    digits = re.search(r'\b\d{6}\b', qr.base.sent[-1][2]).group()
    with pytest.raises(ValueError): qr.svc.verify_code(qr.raw, 'browser', challenge['challenge_id'], digits)


def test_no_pass_duplicate_phone_subscription_and_global_otp_limit(qr):
    qr.base.links.toggle_user_access('example-client', False)
    assert qr.svc.request_code(qr.raw, PHONE, 'browser')['state'] == 'no_pass'
    qr.base.links.toggle_user_access('example-client', True)
    qr.access.billing = SimpleNamespace(subscription_view=lambda branch: {'available': False})
    assert qr.svc.request_code(qr.raw, PHONE, 'browser')['state'] == 'no_pass'
    qr.access.billing = None
    duplicate = qr.base.links.create_link({'branch_id': 11, 'user_name': 'Duplicate', 'phone': PHONE})
    assert qr.svc.request_code(qr.raw, PHONE, 'browser')['state'] == 'ambiguous'
    qr.base.links.delete_link(duplicate)
    assert not qr.base.sent
    invitation = qr.base.service.invite('owner', '11', client_token='example-client')
    qr.base.service.request_otp(invitation['link'].rsplit('/', 1)[1], ip='127.0.0.1', documents=agreements(qr.base.service))
    with pytest.raises(OtpRateLimit): qr.svc.request_code(qr.raw, PHONE, 'browser')


def test_revoke_link_new_code_and_expired_challenge(qr):
    challenge = qr.svc.request_code(qr.raw, PHONE, 'browser')
    digits = re.search(r'\b\d{6}\b', qr.base.sent[-1][2]).group()
    qr.base.clock[0] += 301
    with pytest.raises(ValueError): qr.svc.verify_code(qr.raw, 'browser', challenge['challenge_id'], digits)
    revoked = qr.svc.admin_view('north', '11', qr.gate.stable_id, 'revoke')
    assert not revoked['enabled'] and revoked['url'] is None
    with pytest.raises(GateQRUnavailable): qr.svc.state(qr.raw)
    enabled = qr.svc.admin_view('north', '11', qr.gate.stable_id, 'create')
    assert enabled['enabled'] and not enabled['url'].endswith(qr.raw)
    with pytest.raises(GateQRUnavailable): qr.svc.state(qr.raw)


def test_device_rechecks_pass_phone_expiry_and_branch(qr):
    device = verify(qr)
    qr.base.links.toggle_user_access('example-client', False)
    assert qr.svc.state(qr.raw, device)['state'] == 'no_pass'
    with pytest.raises(PermissionError): qr.svc.open(qr.raw, device, 'request-0000000005')
    qr.base.links.toggle_user_access('example-client', True)
    assert qr.svc.state(qr.raw, device)['state'] == 'active'
    qr.base.clock[0] += DEVICE_SECONDS + 1
    assert qr.svc.state(qr.raw, device)['state'] == 'phone_required'
    device = verify(qr)
    qr.base.links.update_user('example-client', {'phone': '+15550177777'})
    qr.base.links.update_user('example-client', {'phone': PHONE})
    assert qr.svc.state(qr.raw, device)['state'] == 'phone_required'  # no intervening browser visit needed
    assert not qr.calls


def test_rotation_revocation_and_removal_never_transfer_gate(qr):
    device = verify(qr)
    data = qr.base.branches.load_branches()['11']['buttons']
    old = qr.gate
    # Explicit ID preserves the physical gate when its number is edited.
    data[0]['phone'] = '+15550200009'
    qr.base.branches.update_settings('11', {'buttons': list(reversed(data))})
    assert qr.svc.open(qr.raw, device, 'request-0000000006')['status'] == 'success'
    assert qr.calls == ['+15550200009']
    new_url = qr.svc.admin_view('north', '11', old.stable_id, 'rotate')['url']
    with pytest.raises(GateQRUnavailable): qr.svc.state(qr.raw, device)
    raw = new_url.rsplit('/', 1)[1]
    assert qr.svc.state(raw, device)['state'] == 'active'
    qr.base.branches.update_settings('11', {'buttons': [b for b in data if b['stable_id'] != old.stable_id]})
    with pytest.raises(GateQRUnavailable): qr.svc.state(raw, device)
    # A newly added gate at the same old list position must not inherit its QR.
    remaining = qr.base.branches.load_branches()['11']['buttons']
    remaining.insert(0, {'type': 'gate', 'label': 'Replacement', 'phone': '+15550200009'})
    qr.base.branches.update_settings('11', {'buttons': remaining})
    with pytest.raises(GateQRUnavailable): qr.svc.state(raw, device)


def test_legacy_reorder_matches_route_and_rejects_arbitrary_stable_id(qr):
    before = qr.base.branches.get_branch('11').buttons
    source = qr.base.branches.load_branches()['11']['buttons']
    legacy = [{k: v for k, v in b.items() if k != 'stable_id'} for b in reversed(source)]
    qr.base.branches.update_settings('11', {'buttons': legacy})
    assert [(b.phone, b.stable_id) for b in qr.base.branches.get_branch('11').buttons] == [(b.phone, b.stable_id) for b in reversed(before)]
    legacy[0]['stable_id'] = 'invented-gate-identity'
    with pytest.raises(ValueError): qr.base.branches.update_settings('11', {'buttons': legacy})


def test_access_service_final_phone_and_gate_guards(qr):
    qr.base.links.update_user('example-client', {'phone': '+15550177777'})
    result = qr.access.open_for_token('example-client', qr.gate.id, expected_phone=PHONE, expected_gate_id=qr.gate.stable_id)
    assert result['http_status'] == 403 and not qr.calls
    result = qr.access.open_for_token('example-client', qr.gate.id, expected_phone='+15550177777', expected_gate_id='wrong-gate-identity')
    assert result['http_status'] == 403 and not qr.calls


@pytest.fixture
def qr_web(qr):
    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    app.config.update(TESTING=True, SECRET_KEY='offline-qr-browser-test', SESSION_COOKIE_SECURE=False)
    app.register_blueprint(create_blueprint(lambda: {'gate_qr': qr.svc, 'auth': qr.base.auth}))
    @app.context_processor
    def context():
        session.setdefault('csrf_token', 'offline-form-token')
        return {'csrf_token': lambda: session['csrf_token']}
    @app.before_request
    def csrf():
        if request.method == 'POST' and request.headers.get('X-CSRF-Token') != session.get('csrf_token'):
            abort(403)
    return SimpleNamespace(app=app, client=app.test_client(), qr=qr)


def test_web_cookie_csrf_scope_images_and_fixed_gate_payload(qr_web):
    web = qr_web; client = web.client; qr = web.qr; page = '/gate/' + qr.raw
    html = client.get(page)
    assert html.status_code == 200 and 'header-signin' not in html.text
    assert '<a class="brand"' not in html.text
    assert client.post(page + '/code', json={'phone': PHONE}).status_code == 403
    headers = {'X-CSRF-Token': 'offline-form-token'}
    challenge = client.post(page + '/code', json={'phone': PHONE}, headers=headers).json['challenge_id']
    digits = re.search(r'\b\d{6}\b', qr.base.sent[-1][2]).group()
    response = client.post(page + '/verify', json={'challenge_id': challenge, 'code': digits}, headers=headers)
    cookie = response.headers.get('Set-Cookie')
    assert response.json['state'] == 'active' and 'HttpOnly' in cookie and 'SameSite=Lax' in cookie and 'Max-Age=2592000' in cookie
    assert client.post(page + '/open', json={'request_id': 'request-0000000007', 'phone': '+15550199999'}, headers=headers).status_code == 400
    assert not qr.calls
    assert client.post(page + '/open', json={'request_id': 'request-0000000008'}, headers=headers).status_code == 200
    route = '/admin/api/branches/11/gates/' + qr.gate.stable_id + '/qr'
    assert client.get(route).status_code == 401
    with client.session_transaction() as session_data:
        session_data['admin_logged_in'] = True
        session_data['admin_username'] = 'north'
        with web.app.app_context():
            session_data['admin_auth_version'] = qr.base.auth._session_fingerprint(qr.base.auth.get_admin('north'))
    assert client.get(route).status_code == 200
    assert client.get(route + '.svg').mimetype == 'image/svg+xml'
    png = client.get(route + '.png?download=1')
    assert png.data.startswith(b'\x89PNG') and 'attachment' in png.headers['Content-Disposition']
    assert client.get('/admin/api/branches/12/gates/' + qr.gate.stable_id + '/qr').status_code == 403
    assert client.post(page + '/forget', json={}, headers=headers).status_code == 200
    assert client.post(page + '/state', json={}, headers=headers).json['state'] == 'phone_required'


def test_unremembered_cookie_is_session_only_with_server_expiry(qr_web):
    web = qr_web; client = web.client; qr = web.qr; page = '/gate/' + qr.raw
    client.get(page)
    headers = {'X-CSRF-Token': 'offline-form-token'}
    challenge = client.post(page + '/code', json={'phone': PHONE}, headers=headers).json['challenge_id']
    digits = re.search(r'\b\d{6}\b', qr.base.sent[-1][2]).group()
    response = client.post(page + '/verify', json={'challenge_id': challenge, 'code': digits, 'remember': False}, headers=headers)
    cookie = next(value for value in response.headers.getlist('Set-Cookie') if value.startswith('gatebot_qr_'))
    assert 'Max-Age' not in cookie and 'Expires' not in cookie
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
    assert response.json['verified'] and response.json['state'] == 'active'
    qr.base.clock[0] += SESSION_DEVICE_SECONDS + 1
    assert client.post(page + '/state', json={}, headers=headers).json['state'] == 'phone_required'


def test_temporary_client_stays_verified_without_preferences_link(qr):
    device = verify(qr)
    with qr.base.links.store.transaction() as links:
        links['example-client']['is_temporary'] = True
    state = qr.svc.state(qr.raw, device)
    assert state['verified'] and state['state'] == 'active' and state['is_temporary']
    assert state['preferences_url'] is None
    assert qr.svc.open(qr.raw, device, 'request-temp-00000001')['status'] == 'success'
