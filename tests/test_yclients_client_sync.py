"""Client-ID lookup HTTP/service regressions; directory reads are synthetic."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from storage import config
from storage.sql_documents import read_document, write_document
from test_messenger_settings_web import messenger_web
from test_web_security import web, login, post
from yclients_api import YClientsError


ONE = '/admin/api/yclients/client-id'
BULK = '/admin/api/yclients/client-ids'
PHONE = '+79990000101'


class Directory:
    def __init__(self, store):
        self.store, self.calls, self.rows, self.effect = store, [], None, None

    def find_clients_by_phone(self, branch_id, phone):
        # A real GET can take seconds; it must never hold the shared writer lock.
        assert self.store.current_connection() is None
        self.calls.append((str(branch_id), phone))
        if self.effect:
            self.effect()
        if isinstance(self.rows, Exception):
            raise self.rows
        if callable(self.rows):
            return self.rows(str(branch_id), phone)
        return deepcopy(self.rows) if self.rows is not None else [
            {'id': 901, 'phone': phone, 'company_id': int(branch_id), 'name': 'CRM contact'}]


@pytest.fixture
def sync_web(messenger_web, monkeypatch):
    web, identity, outgoing = messenger_web
    monkeypatch.setattr(config, 'YCLIENTS_ENABLED', True)
    identity.attach_console_account('tenant-a', web.links.load_links()[web.token_a]['phone'], client_token=web.token_a)
    services = web.module.product_services()
    directory = Directory(identity.product)
    services.yclients_client_sync.crm = directory
    return SimpleNamespace(web=web, identity=identity, services=services,
                           directory=directory, outgoing=outgoing)


def refresh(client, csrf, token):
    return post(client, csrf, ONE, {'token': token})


def test_single_refresh_changes_only_id_and_preserves_profile_grant_and_preferences(sync_web):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    s.web.links.update_user(s.web.token_a, {'email': 'local@example.invalid'})
    s.web.links.bind_identity('telegram', '501', token=s.web.token_a)
    s.web.links.bind_identity('max', '502', token=s.web.token_a)
    with s.identity.product.connect() as db:
        db.execute('INSERT OR REPLACE INTO notification_preferences VALUES(?,"client","12",?)',
                   (s.web.links.load_links()[s.web.token_a]['person_id'], '{"channels":{"email":true,"max":false}}'))
        preferences = [tuple(row) for row in db.execute('SELECT * FROM notification_preferences')]
    before = s.web.links.load_links()
    response = refresh(client, csrf, s.web.token_a)
    assert response.status_code == 200 and response.json['result']['status'] == 'updated'
    assert response.json['result']['id_ycl'] == 901
    after = s.web.links.load_links()
    assert after[s.web.token_a] == {**before[s.web.token_a], 'id_ycl': 901}
    assert after[s.web.token_b] == before[s.web.token_b] and len(after) == len(before)
    assert refresh(client, csrf, s.web.token_a).json['result']['status'] == 'unchanged'
    with s.identity.product.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM notification_preferences')] == preferences
    assert s.directory.calls == [('12', before[s.web.token_a]['phone'])] * 2
    s.outgoing.assert_not_called(); assert s.web.calls == []


def test_login_csrf_primary_branch_and_historical_memberships_cannot_expand_scope(sync_web):
    s = sync_web; client = s.web.app.test_client()
    assert client.post(ONE, json={'token': s.web.token_a}).status_code in {401, 403}
    assert client.get(BULK + '?branch_id=12').status_code == 401
    csrf = login(client)
    assert client.post(ONE, json={'token': s.web.token_a}).status_code == 403
    assert client.post(BULK, json={'branch_id': '12'}).status_code == 403
    person = s.identity.web_admin('tenant-a')['person_id']
    with s.identity.product.connect() as db:
        db.execute('INSERT OR REPLACE INTO branch_memberships VALUES(?,"123","active",0)', (person,))
    assert refresh(client, csrf, s.web.token_b).status_code == 403
    assert client.get(BULK + '?branch_id=123').status_code == 403
    assert post(client, csrf, BULK, {'branch_id': '123'}).status_code == 403
    assert not s.directory.calls
    s.identity.save_integration('super', '123', 'yclients', {'user_token': 'offline-second-branch'})
    csrf = login(client, 'super')
    assert refresh(client, csrf, s.web.token_b).json['result']['status'] == 'updated'
    assert s.directory.calls[-1][0] == '123'


@pytest.mark.parametrize('payload', [None, [], 'bad', {}, {'token': []}, {'token': 12},
                                     {'token': ''}, {'token': 'x' * 129},
                                     {'token': 'missing'}, {'token': 'missing', 'id_ycl': 123},
                                     {'token': 'missing', 'phone': PHONE}, {'token': 'missing', 'branch_id': '12'}])
def test_single_refresh_rejects_malformed_or_forged_identity_payload(sync_web, payload):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    assert post(client, csrf, ONE, payload).status_code == 400
    assert not s.directory.calls


@pytest.mark.parametrize('payload', [None, [], 'bad', {'branch_id': {}}, {'branch_id': True},
                                     {'branch_id': ''}, {'branch_id': '12', 'token': 'other'}])
def test_batch_rejects_malformed_scope_payload(sync_web, payload):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    assert post(client, csrf, BULK, payload).status_code == 400
    assert not s.directory.calls


@pytest.mark.parametrize('state', ['standalone', 'disabled', 'missing_credentials', 'frozen', 'uninstalled'])
def test_unavailable_crm_never_performs_a_lookup_or_removes_old_id(sync_web, monkeypatch, state):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    s.web.links.update_user(s.web.token_a, {'id_ycl': 77})
    if state == 'disabled':
        monkeypatch.setattr(config, 'YCLIENTS_ENABLED', False)
    else:
        with s.identity.product.connect() as db:
            if state == 'missing_credentials':
                db.execute('DELETE FROM identity_integrations WHERE branch_id="12" AND kind="yclients"')
            else:
                branches = read_document(db, 'branches.json')
                branches['12'].update({'yclients_enabled': False} if state == 'standalone'
                                      else {'yclients_marketplace_state': state})
                write_document(db, 'branches.json', branches)
    response = refresh(client, csrf, s.web.token_a)
    if state == 'standalone':
        assert response.status_code == 403
        assert client.get(BULK).status_code == 403
        assert post(client, csrf, BULK, {}).status_code == 403
    else:
        assert response.status_code == 200 and response.json['result']['status'] == 'unavailable'
        assert client.get(BULK).json['sync']['can_start'] is False
        assert post(client, csrf, BULK, {}).status_code == 400
    assert s.web.links.load_links()[s.web.token_a]['id_ycl'] == 77 and not s.directory.calls


@pytest.mark.parametrize('kind,expected', [('none', 'not_found'), ('different_phone', 'not_found'),
    ('ambiguous', 'ambiguous'), ('exception', 'error'), ('incomplete', 'error'), ('masked', 'error'),
    ('cross_branch', 'error'), ('invalid_id', 'error'), ('bad_collection', 'error')])
def test_nonunique_unreadable_or_untrusted_results_keep_the_previous_id(sync_web, kind, expected):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    s.web.links.update_user(s.web.token_a, {'id_ycl': 77})
    phone = s.web.links.load_links()[s.web.token_a]['phone']
    rows = [{'id': 901, 'phone': phone, 'company_id': 12}]
    if kind == 'none': rows = []
    elif kind == 'different_phone': rows[0]['phone'] = '+15550100999'
    elif kind == 'ambiguous': rows.append({'id': 902, 'phone': phone, 'company_id': 12})
    elif kind in {'exception', 'incomplete'}:
        rows = RuntimeError('private upstream credential') if kind == 'exception' else YClientsError('Incomplete private page')
    elif kind == 'masked': rows[0]['phone'] = '+7 000 XXX-XX-01'
    elif kind == 'cross_branch': rows[0]['company_id'] = 123
    elif kind == 'invalid_id': rows[0]['id'] = True
    else: rows = {'private': 'raw collection'}
    s.directory.rows = rows
    before = s.web.links.load_links()
    response = refresh(client, csrf, s.web.token_a)
    assert response.status_code == 200 and response.json['result']['status'] == expected
    assert response.json['result']['id_ycl'] == 77 and s.web.links.load_links() == before
    assert 'private' not in response.get_data(as_text=True)


@pytest.mark.parametrize('same_phone', [True, False])
def test_conflicting_regular_client_is_reported_without_merging(sync_web, same_phone):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    phone = s.web.links.load_links()[s.web.token_a]['phone']
    s.web.links.create_link({'branch_id': 12, 'phone': phone if same_phone else '+15550100888',
                             'id_ycl': None if same_phone else 901})
    before = s.web.links.load_links()
    result = refresh(client, csrf, s.web.token_a)
    assert result.status_code == 200 and result.json['result']['status'] == 'conflict'
    assert s.web.links.load_links() == before


def test_guest_and_canonical_client_can_share_id_without_changing_guest_grant(sync_web):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    phone = s.web.links.load_links()[s.web.token_a]['phone']
    guest = s.web.links.create_link({'branch_id': 12, 'phone': phone, 'is_temporary': True,
                                     'mailing_enabled': False}, expires_days=1)
    before = s.web.links.load_links()
    assert refresh(client, csrf, guest).json['result']['status'] == 'updated'
    assert refresh(client, csrf, s.web.token_a).json['result']['status'] == 'updated'
    after = s.web.links.load_links()
    assert after[guest] == {**before[guest], 'id_ycl': 901}
    assert after[s.web.token_a] == {**before[s.web.token_a], 'id_ycl': 901}


@pytest.mark.parametrize('change', ['phone', 'id', 'role', 'branch_removed'])
def test_concurrent_identity_or_scope_changes_prevent_stale_writes(sync_web, change):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    s.web.links.update_user(s.web.token_a, {'id_ycl': 77})
    def changed():
        if change == 'phone': s.web.links.update_user(s.web.token_a, {'phone': '+15550100998'})
        elif change == 'id': s.web.links.update_user(s.web.token_a, {'id_ycl': 909})
        elif change == 'role':
            with s.identity.product.connect() as db:
                admins = read_document(db, 'admins.json'); admins['tenant-a']['disabled'] = True
                write_document(db, 'admins.json', admins)
        else:
            from branch_lifecycle import BranchLifecycleService
            lifecycle = BranchLifecycleService(s.identity); preview = lifecycle.preview('super', '12')
            lifecycle.delete('super', '12', confirmation='12', revision=preview['revision'])
    s.directory.effect = changed
    response = refresh(client, csrf, s.web.token_a)
    if change == 'role': assert response.status_code == 403
    elif change == 'branch_removed':
        assert response.status_code in {400, 403}
        assert s.web.token_a not in s.web.links.load_links() and s.web.branches.get_branch(12) is None
    else:
        assert response.status_code == 200 and response.json['result']['status'] == 'stale'
    if change != 'branch_removed':
        assert s.web.links.load_links()[s.web.token_a]['id_ycl'] == {'phone': None, 'id': 909, 'role': 77}[change]
    assert len(s.directory.calls) == 1


def test_invalid_saved_phone_is_reported_without_directory_request(sync_web):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    with s.web.links.store.transaction() as values:
        values[s.web.token_a].update(phone='masked **', id_ycl=77)
    result = refresh(client, csrf, s.web.token_a)
    assert result.json['result']['status'] == 'error' and result.json['result']['id_ycl'] == 77
    assert not s.directory.calls


def test_legacy_phone_format_is_normalized_for_bulk_lookup_and_compare(sync_web):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    with s.web.links.store.transaction() as values:
        values[s.web.token_a].update(phone='8 (999) 000-01-01', id_ycl=77)
    started = post(client, csrf, BULK, {}).json['sync']
    assert started['total'] == 1
    s.services.yclients_client_jobs.process_once(limit=10)
    status = client.get(BULK).json['sync']
    assert status['state'] == 'complete' and status['errors'] == 0 and status['updated'] == 1
    assert s.directory.calls == [('12', PHONE)]
    assert s.web.links.load_links()[s.web.token_a]['id_ycl'] == 901


def test_batch_api_runs_scoped_snapshot_and_reports_real_outcomes(sync_web):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    s.web.links.update_user(s.web.token_a, {'id_ycl': 77})
    missing = s.web.links.create_link({'branch_id': 12, 'phone': '+15550100801', 'id_ycl': 801})
    ambiguous = s.web.links.create_link({'branch_id': 12, 'phone': '+15550100802', 'id_ycl': 802})
    def rows(branch, phone):
        if phone.endswith('801'): return []
        if phone.endswith('802'): return [{'id': 902, 'phone': phone}, {'id': 903, 'phone': phone}]
        return [{'id': 901, 'phone': phone, 'company_id': int(branch)}]
    s.directory.rows = rows
    foreign = deepcopy(s.web.links.load_links()[s.web.token_b])
    assert client.get(BULK).json['sync']['state'] == 'idle'
    first = post(client, csrf, BULK, {}).json['sync']
    again = post(client, csrf, BULK, {}).json['sync']
    assert first['id'] == again['id'] and first['total'] == 3 and first['processed'] == 0
    assert not s.directory.calls
    s.services.yclients_client_jobs.process_once(limit=10)
    result = client.get(BULK).json['sync']
    assert result['state'] == 'complete' and result['processed'] == 3
    assert result['updated'] == 1 and result['not_found'] == 1 and result['ambiguous'] == 1 and result['errors'] == 0
    current = s.web.links.load_links()
    assert current[s.web.token_b] == foreign and current[missing]['id_ycl'] == 801 and current[ambiguous]['id_ycl'] == 802
    assert len(s.directory.calls) == 3 and all(branch == '12' for branch, _ in s.directory.calls)


def test_batch_with_pending_automatic_lookup_has_no_false_stale_errors(sync_web):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    token = s.web.links.create_link({'branch_id': 12, 'phone': '+15550100306', 'mailing_enabled': False})
    s.directory.rows = lambda branch, phone: [{'id': int(phone[-4:]), 'phone': phone, 'company_id': int(branch)}]
    started = post(client, csrf, BULK, {}).json['sync']
    assert started['total'] == 2
    s.services.yclients_client_jobs.process_once(limit=20)
    result = client.get(BULK).json['sync']
    assert result['state'] == 'complete' and result['processed'] == 2 and result['errors'] == 0
    assert result['updated'] + result['unchanged'] == 2
    assert s.web.links.load_links()[token]['id_ycl'] == 306


@pytest.mark.parametrize('explicit,expected', [('omit', None), ('old', None), ('new', 88)])
def test_manual_phone_change_does_not_carry_stale_id_from_edit_form(sync_web, explicit, expected):
    s = sync_web; client = s.web.app.test_client(); csrf = login(client)
    s.web.links.update_user(s.web.token_a, {'phone': PHONE, 'id_ycl': 77})
    body = {'token': s.web.token_a, 'phone': '8 (999) 000-01-02'}
    if explicit != 'omit': body['id_ycl'] = '77' if explicit == 'old' else 88
    result = post(client, csrf, '/admin/api/update-user', body)
    assert result.status_code == 200
    current = s.web.links.load_links()[s.web.token_a]
    assert current['phone'] == '+79990000102'
    assert current['id_ycl'] is None if expected is None else str(current['id_ycl']) == str(expected)
    assert not s.directory.calls


def test_phone_format_only_change_keeps_yc_identity(sync_web):
    s = sync_web
    s.web.links.update_user(s.web.token_a, {'phone': PHONE, 'id_ycl': 77})
    s.web.links.update_user(s.web.token_a, {'phone': '8 (999) 000-01-01'})
    assert s.web.links.load_links()[s.web.token_a]['id_ycl'] == 77 and not s.directory.calls


def test_bad_yclients_url_does_not_prevent_opening_admin_or_settings(sync_web, monkeypatch):
    from unittest.mock import Mock
    s = sync_web
    monkeypatch.setattr(config, 'YC_BASE_URL', 'https://invalid.example')
    monkeypatch.setattr(s.web.module, '_product_cache', {})
    get = Mock(side_effect=AssertionError('An invalid endpoint must never be requested'))
    monkeypatch.setattr('requests.Session.get', get)
    client = s.web.app.test_client(); csrf = login(client, 'super')
    assert client.get('/admin/system-settings').status_code == 200
    assert client.get('/admin/api/users').status_code == 200
    before = s.web.links.load_links()
    result = refresh(client, csrf, s.web.token_a)
    assert result.status_code == 200 and result.json['result']['status'] == 'error'
    assert s.web.links.load_links() == before
    get.assert_not_called()
    s.web.module.product_services().yclients_client_sync.crm = s.directory
    assert refresh(client, csrf, s.web.token_a).json['result']['status'] == 'updated'
