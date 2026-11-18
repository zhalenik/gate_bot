"""Explicit integration diagnostics use scoped recipients and mocked transports."""
from types import SimpleNamespace
from unittest.mock import Mock
import json
import uuid

import pytest

from storage import config
from storage.sql_documents import read_document, write_document
from test_messenger_settings_web import messenger_web
from test_web_security import web, login, post


ENDPOINT = '/admin/api/integration-test'


@pytest.fixture
def test_delivery_web(messenger_web, monkeypatch):
    web, identity, _ = messenger_web
    monkeypatch.setattr(config, 'OUTBOUND_ENABLED', True)
    monkeypatch.setattr(config, 'YCLIENTS_ENABLED', True)
    identity.save_integration('super', '12', 'chatpush', {'token': 'offline-test-chatpush'})
    web.links.update_user(web.token_a, {'id_ycl': 101})
    web.links.update_user(web.token_b, {'id_ycl': 202})
    token_no_yc = web.links.create_link({'branch_id': 12, 'user_name': 'Local recipient',
                                        'phone': '+15550100606', 'mailing_enabled': False})
    result = {'status': 'accepted', 'confirmed': False, 'code': 'provider_accepted'}
    cp = Mock(return_value=dict(result))
    yc = Mock(return_value=dict(result))
    monkeypatch.setattr('chatpush.send_message', cp)
    monkeypatch.setattr('yclients_delivery.YClientsDelivery.send', yc)
    return SimpleNamespace(web=web, identity=identity, cp=cp, yc=yc, token_no_yc=token_no_yc)


def payload(**updates):
    value = {'branch_id': '12', 'provider': 'chatpush', 'text': 'Synthetic integration check',
             'request_id': uuid.uuid4().hex, 'phone': '+15550100607'}
    value.update(updates)
    return value


def for_client(state, provider='yclients', **updates):
    value = payload(provider=provider)
    value.pop('phone')
    value['client_token'] = state.web.token_a
    value.update(updates)
    return value


def test_scoped_recipient_lists_require_login_and_do_not_send(test_delivery_web):
    s = test_delivery_web; client = s.web.app.test_client()
    assert client.get(ENDPOINT + '?branch_id=12&provider=chatpush').status_code == 401
    login(client)
    cp = client.get(ENDPOINT + '?branch_id=12&provider=chatpush')
    assert cp.status_code == 200
    state = cp.json['test']
    assert state['ready'] and state['max_length'] == 3500
    assert {row['token'] for row in state['clients']} == {s.web.token_a, s.token_no_yc}
    yc = client.get(ENDPOINT + '?branch_id=12&provider=yclients').json['test']
    assert yc['max_length'] == 16000
    assert [(r['token'], r['yclients_id']) for r in yc['clients']] == [(s.web.token_a, 101)]
    assert 'offline-' not in cp.get_data(as_text=True)
    assert client.get(ENDPOINT + '?branch_id=123&provider=chatpush').status_code == 403
    s.cp.assert_not_called(); s.yc.assert_not_called()


def test_primary_scope_csrf_and_super_rechecked_on_send(test_delivery_web):
    s = test_delivery_web
    person = s.identity.attach_console_account('tenant-a', s.web.links.load_links()[s.web.token_a]['phone'], client_token=s.web.token_a)
    with s.identity.product.connect() as db:
        db.execute('INSERT INTO branch_memberships(person_id,branch_id,status,created) VALUES(?,"123","active",0)', (person,))
    client = s.web.app.test_client(); csrf = login(client)
    assert client.post(ENDPOINT, json=payload()).status_code == 403
    assert post(client, csrf, ENDPOINT, payload(branch_id='123')).status_code == 403
    assert client.get(ENDPOINT + '?branch_id=123&provider=yclients').status_code == 403
    assert post(client, csrf, ENDPOINT, for_client(s, client_token=s.web.token_b)).status_code == 400
    s.cp.assert_not_called(); s.yc.assert_not_called()
    csrf = login(client, 'super')
    s.identity.save_integration('super', '123', 'chatpush', {'token': 'offline-second-branch'})
    result = post(client, csrf, ENDPOINT, payload(branch_id='123'))
    assert result.status_code == 200 and result.json['delivery']['status'] == 'accepted'
    assert s.cp.call_args.args[2] == '123'
    assert s.cp.call_args.kwargs['credentials'] == {'token': 'offline-second-branch'}


def test_chatpush_manual_normalization_client_selection_and_no_preferences_mutation(test_delivery_web):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    with s.identity.product.connect() as db:
        branches = read_document(db, 'branches.json'); branches['12']['messenger_provider'] = 'yclients'
        write_document(db, 'branches.json', branches)
        links = read_document(db, 'links.json'); links[s.web.token_a]['mailing_enabled'] = False
        write_document(db, 'links.json', links)
        before = [read_document(db, name) for name in ('branches.json', 'links.json', 'admins.json')]
        prefs = list(db.execute('SELECT * FROM notification_preferences'))
    result = post(client, csrf, ENDPOINT, payload(phone='8 (900) 000-00-55'))
    assert result.status_code == 200 and result.json['delivery']['confirmed'] is False
    assert s.cp.call_args.args[0] == '+79000000055'
    result = post(client, csrf, ENDPOINT, for_client(s, provider='chatpush'))
    assert result.json['delivery']['status'] == 'accepted'
    assert s.cp.call_args.args[0] == s.web.links.load_links()[s.web.token_a]['phone']
    with s.identity.product.connect() as db:
        assert before == [read_document(db, name) for name in ('branches.json', 'links.json', 'admins.json')]
        assert prefs == list(db.execute('SELECT * FROM notification_preferences'))
    s.yc.assert_not_called(); assert not s.web.calls


def test_yclients_uses_exact_server_client_id_and_own_branch_credentials(test_delivery_web):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    # auto chooses the connected ChatPush, but the explicit test must use YC.
    result = post(client, csrf, ENDPOINT, for_client(s))
    assert result.status_code == 200 and result.json['delivery']['status'] == 'accepted'
    s.yc.assert_called_once()
    assert s.yc.call_args.args[:3] == ('12', 101, 'Synthetic integration check')
    assert s.yc.call_args.args[3] == {'user_token': 'offline-branch-user-token', 'partner_token': 'offline-partner-token'}
    s.cp.assert_not_called()


def test_yclients_202_reaches_browser_as_accepted_and_same_request_never_resends(web, tmp_path, monkeypatch):
    from storage.sql_documents import DocumentStore, migrate_legacy
    from yclients_delivery import YClientsDelivery
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    identity = web.module.product_services().identity
    monkeypatch.setattr(config, 'OUTBOUND_ENABLED', True)
    monkeypatch.setattr(config, 'YCLIENTS_ENABLED', True)
    monkeypatch.setattr(config, 'YC_PARTNER_TOKEN', 'offline-partner-token')
    identity.save_integration('super', '12', 'yclients', {'user_token': 'offline-branch-user-token'})
    web.links.update_user(web.token_a, {'id_ycl': 101})
    session = Mock()
    session.post.return_value = SimpleNamespace(status_code=202,
        json=lambda: {'success': True, 'meta': {'message': 'Synthetic accepted response'}})
    delivery = YClientsDelivery(session=session, pace=lambda: None)
    monkeypatch.setattr('yclients_delivery.YClientsDelivery', lambda: delivery)
    client = web.app.test_client(); csrf = login(client)
    data = payload(provider='yclients', client_token=web.token_a)
    data.pop('phone')
    first = post(client, csrf, ENDPOINT, data)
    repeated = post(client, csrf, ENDPOINT, data)
    assert first.status_code == repeated.status_code == 200
    assert first.json == repeated.json
    result = first.json['delivery']
    assert result['status'] == 'accepted' and result['code'] == 'provider_accepted'
    assert result['http_status'] == 202 and result['confirmed'] is False
    assert result['message'].startswith('Сервис принял сообщение.')
    session.post.assert_called_once()
    with identity.product.connect() as db:
        assert [row['state'] for row in db.execute('SELECT state FROM message_effects')] == ['accepted']


@pytest.mark.parametrize('change', [
    {'provider': 'auto'}, {'provider': []}, {'text': ''}, {'text': '   '}, {'text': None},
    {'request_id': 'short'}, {'request_id': []}, {'client_id': 101}, {'target': '+15550100999'},
    {'phone': []}, {'phone': True}, {'phone': 'not a phone'}, {'branch_id': []},
])
def test_malformed_inputs_cannot_reach_transport(test_delivery_web, change):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    result = post(client, csrf, ENDPOINT, payload(**change))
    assert result.status_code == 400
    s.cp.assert_not_called(); s.yc.assert_not_called()


@pytest.mark.parametrize('mode', ['yc_phone', 'both', 'none', 'missing_client', 'no_yc_id', 'foreign_client'])
def test_single_scoped_recipient_is_required(test_delivery_web, mode):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    data = for_client(s)
    if mode == 'yc_phone': data = payload(provider='yclients')
    elif mode == 'both': data['phone'] = '+15550100999'
    elif mode == 'none': data.pop('client_token')
    elif mode == 'missing_client': data['client_token'] = 'missing'
    elif mode == 'no_yc_id': data['client_token'] = s.token_no_yc
    elif mode == 'foreign_client': data['client_token'] = s.web.token_b
    assert post(client, csrf, ENDPOINT, data).status_code == 400
    s.cp.assert_not_called(); s.yc.assert_not_called()


@pytest.mark.parametrize('provider,limit', [('chatpush', 3500), ('yclients', 16000)])
def test_provider_text_boundaries(test_delivery_web, provider, limit):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    data = for_client(s, provider=provider, text='Я' * limit)
    assert post(client, csrf, ENDPOINT, data).json['delivery']['status'] == 'accepted'
    bad = for_client(s, provider=provider, text='Я' * (limit + 1))
    assert post(client, csrf, ENDPOINT, bad).status_code == 400
    assert s.cp.call_count + s.yc.call_count == 1


@pytest.mark.parametrize('mode,provider,reason', [
    ('outbound_off', 'chatpush', 'outbound_disabled'),
    ('yc_off', 'yclients', 'yclients_disabled'),
    ('cp_missing', 'chatpush', 'credentials_missing'),
    ('yc_missing', 'yclients', 'credentials_missing'),
    ('frozen', 'yclients', 'integration_unavailable'),
    ('uninstalled', 'yclients', 'integration_unavailable'),
])
def test_disconnected_or_disabled_provider_never_falls_back(test_delivery_web, monkeypatch, mode, provider, reason):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    if mode == 'outbound_off': monkeypatch.setattr(config, 'OUTBOUND_ENABLED', False)
    elif mode == 'yc_off': monkeypatch.setattr(config, 'YCLIENTS_ENABLED', False)
    else:
        with s.identity.product.connect() as db:
            if mode.endswith('_missing'):
                db.execute('DELETE FROM identity_integrations WHERE branch_id="12" AND kind=?', (provider,))
            else:
                branches = read_document(db, 'branches.json'); branches['12']['yclients_marketplace_state'] = mode
                write_document(db, 'branches.json', branches)
    state = client.get(ENDPOINT + '?branch_id=12&provider=' + provider).json['test']
    assert not state['ready'] and state['reason'] == reason
    delivery = post(client, csrf, ENDPOINT, for_client(s, provider=provider)).json['delivery']
    assert delivery['status'] == 'failed' and delivery['code'] == reason
    s.cp.assert_not_called(); s.yc.assert_not_called()


def test_standalone_is_rejected_on_list_and_send(test_delivery_web):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    with s.identity.product.connect() as db:
        branches = read_document(db, 'branches.json'); branches['12']['yclients_enabled'] = False
        write_document(db, 'branches.json', branches)
    for provider in ('chatpush', 'yclients'):
        assert client.get(ENDPOINT + '?branch_id=12&provider=' + provider).status_code == 403
        assert post(client, csrf, ENDPOINT, for_client(s, provider=provider)).status_code == 403
    s.cp.assert_not_called(); s.yc.assert_not_called()


@pytest.mark.parametrize('status', ['accepted', 'unknown', 'failed'])
def test_same_request_does_not_repeat_even_unknown_and_conflicts_fail(test_delivery_web, status):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    s.cp.return_value = {'status': status, 'code': 'transport_error' if status == 'unknown' else 'provider_accepted' if status == 'accepted' else 'provider_rejected',
                         'message': 'private provider details', 'provider_id': 'private provider id', 'confirmed': True}
    data = payload(); first = post(client, csrf, ENDPOINT, data)
    again = post(client, csrf, ENDPOINT, data)
    assert first.status_code == again.status_code == 200
    assert first.json['delivery']['status'] == again.json['delivery']['status'] == status
    assert first.json['delivery']['confirmed'] is False
    assert 'private provider' not in first.get_data(as_text=True)
    assert post(client, csrf, ENDPOINT, {**data, 'phone': '+15550100998'}).status_code == 409
    assert post(client, csrf, ENDPOINT, {**data, 'text': 'Changed content'}).status_code == 409
    s.cp.assert_called_once(); s.yc.assert_not_called()
    with s.identity.product.connect() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM integration_test_requests')]
        assert len(rows) == 1
        encoded = json.dumps(rows)
        assert data['text'] not in encoded and data['phone'] not in encoded


def test_transport_exception_is_unknown_without_raw_error_or_retry(test_delivery_web):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    s.yc.side_effect = RuntimeError('private upstream credential detail')
    data = for_client(s)
    for _ in range(2):
        response = post(client, csrf, ENDPOINT, data)
        assert response.json['delivery']['status'] == 'unknown'
        assert 'private upstream' not in response.get_data(as_text=True)
    s.yc.assert_called_once(); s.cp.assert_not_called()


@pytest.mark.parametrize('change', ['recipient', 'role'])
def test_recipient_and_actor_are_rechecked_before_transport(test_delivery_web, monkeypatch, change):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    notices = s.web.module.product_services().notifications
    original_send = notices.send
    def changed_before_claim(*args, **kwargs):
        if change == 'recipient': s.web.links.update_user(s.web.token_a, {'id_ycl': 404})
        else:
            with s.identity.product.connect() as db:
                admins = read_document(db, 'admins.json'); admins['tenant-a']['disabled'] = True
                write_document(db, 'admins.json', admins)
        return original_send(*args, **kwargs)
    monkeypatch.setattr(notices, 'send', changed_before_claim)
    result = post(client, csrf, ENDPOINT, for_client(s))
    assert result.status_code == 200 and result.json['delivery']['status'] == 'skipped'
    s.cp.assert_not_called(); s.yc.assert_not_called()


def test_delete_branch_removes_test_requests_and_their_effects(test_delivery_web):
    from branch_lifecycle import BranchLifecycleService
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    assert post(client, csrf, ENDPOINT, payload()).json['delivery']['status'] == 'accepted'
    with s.identity.product.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM integration_test_requests').fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM message_effects').fetchone()[0] == 1
    lifecycle = BranchLifecycleService(s.identity); preview = lifecycle.preview('super', '12')
    lifecycle.delete('super', '12', confirmation='12', revision=preview['revision'])
    with s.identity.product.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM integration_test_requests').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM message_effects').fetchone()[0] == 0
        assert '123' in read_document(db, 'branches.json')


@pytest.mark.parametrize('provider', ['chatpush', 'yclients'])
def test_unselected_broken_integration_does_not_block_test(test_delivery_web, monkeypatch, provider):
    s = test_delivery_web; client = s.web.app.test_client(); csrf = login(client)
    original = s.identity.integration_credentials
    def credentials(branch_id, kind):
        if kind != provider:
            raise AssertionError('The unrelated integration must not be read')
        return original(branch_id, kind)
    monkeypatch.setattr(s.identity, 'integration_credentials', credentials)
    assert client.get(ENDPOINT + '?branch_id=12&provider=' + provider).json['test']['ready']
    assert post(client, csrf, ENDPOINT, for_client(s, provider=provider)).json['delivery']['status'] == 'accepted'
    assert s.cp.call_count + s.yc.call_count == 1


def test_concurrent_duplicate_claim_sends_only_once(test_delivery_web):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from integration_test_delivery import IntegrationTestDelivery
    s = test_delivery_web
    service = IntegrationTestDelivery(s.identity, s.web.module.product_services().notifications)
    entered, release = Event(), Event()
    def transport(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return {'status': 'accepted', 'confirmed': False}
    s.cp.side_effect = transport
    data = payload()
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service.send, 'tenant-a', data)
        try:
            assert entered.wait(8)
            duplicate = service.send('tenant-a', data)
            assert duplicate['status'] == 'unknown' and duplicate['code'] == 'effect_in_progress'
            s.cp.assert_called_once()
        finally:
            release.set()
        assert first.result(timeout=8)['status'] == 'accepted'
    s.cp.assert_called_once(); s.yc.assert_not_called()
