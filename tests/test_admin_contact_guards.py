"""Offline checks for privileged contact edits and stable administrator profiles."""
import pytest

from storage.sql_documents import DocumentStore, migrate_legacy
from test_web_security import web, login, post
from test_new_branch_web import settings
from test_scheduler import state, AT


@pytest.fixture
def admin_contacts(web, tmp_path, monkeypatch):
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    services = web.module.product_services()
    services.identity.admin_email('super', 'owner@example.invalid')
    services.identity.attach_console_account('tenant-a', '+70000000303')
    web.links.update_user(web.token_a, {'email': 'owner@example.invalid'})
    return web, services


@pytest.mark.parametrize('email', ['redirect@example.invalid', ''])
def test_branch_admin_cannot_redirect_or_clear_super_email(admin_contacts, email):
    web, services = admin_contacts
    client = web.app.test_client()
    csrf = login(client, 'tenant-a')
    response = post(client, csrf, '/admin/api/update-user', {
        'token': web.token_a, 'email': email, 'user_name': 'Client',
    })
    assert response.status_code == 200
    assert web.links.load_links()[web.token_a]['email'] == email
    assert services.identity.admin_preferences('super')['email'] == 'owner@example.invalid'
    assert web.calls == []


def test_unchanged_super_email_does_not_block_other_client_edits(admin_contacts):
    web, services = admin_contacts
    client = web.app.test_client()
    csrf = login(client, 'tenant-a')
    response = post(client, csrf, '/admin/api/update-user', {
        'token': web.token_a, 'email': 'owner@example.invalid', 'user_name': 'Updated name',
    })
    assert response.status_code == 200
    current = web.links.load_links()[web.token_a]
    assert current['email'] == 'owner@example.invalid'
    assert current['user_name'] == 'Updated name'


def test_public_preferences_use_authenticated_actor_not_payload(admin_contacts):
    web, services = admin_contacts
    client = web.app.test_client()
    csrf = login(client, 'tenant-a')
    payload = {'enabled': True, 'email': 'redirect@example.invalid', 'channels': {}, 'actor_username': 'super'}
    response = post(client, csrf, '/preferences/' + web.token_a, payload)
    assert response.status_code == 200
    assert services.identity.admin_preferences('super')['email'] == 'owner@example.invalid'
    csrf = login(client, 'super')
    response = post(client, csrf, '/preferences/' + web.token_a, payload)
    assert response.status_code == 200
    assert services.identity.admin_preferences('super')['email'] == 'owner@example.invalid'
    assert web.calls == []


def test_connection_status_requires_login_and_uses_current_account(admin_contacts):
    web, services = admin_contacts
    client = web.app.test_client()
    assert client.get('/admin/api/identity/notification-settings').status_code == 401
    login(client, 'tenant-a')
    response = client.get('/admin/api/identity/notification-settings?username=super')
    assert response.status_code == 200
    assert response.get_json()['preferences']['person_id'] == services.identity.web_admin('tenant-a')['person_id']


def test_super_can_edit_own_contact_and_check_uses_shared_transaction(admin_contacts, monkeypatch):
    web, services = admin_contacts
    guard = services.identity.assert_client_email_change
    calls = []

    def checked(username, token, email):
        assert services.store.current_connection() is not None
        calls.append((username, token))
        return guard(username, token, email)

    monkeypatch.setattr(services.identity, 'assert_client_email_change', checked)
    client = web.app.test_client()
    csrf = login(client, 'super')
    assert post(client, csrf, '/admin/api/update-user', {
        'token': web.token_a, 'email': 'changed@example.invalid',
    }).status_code == 200
    assert calls == [('super', web.token_a)]
    assert web.links.load_links()[web.token_a]['email'] == 'changed@example.invalid'
    assert web.links.load_links()[web.token_a]['email_override'] is True
    assert services.identity.admin_preferences('super')['email'] == 'owner@example.invalid'


def test_same_phone_does_not_make_independent_client_a_super_profile(admin_contacts):
    web, services = admin_contacts
    token = web.links.create_link({'branch_id': 12, 'phone': '+70000000101',
                                   'user_name': 'Independent client', 'email': 'client@example.invalid'})
    assert web.links.load_links()[token]['person_id'] != web.links.load_links()[web.token_a]['person_id']
    client = web.app.test_client()
    csrf = login(client, 'tenant-a')
    assert post(client, csrf, '/admin/api/update-user', {
        'token': token, 'email': 'client-edited@example.invalid',
    }).status_code == 200
    assert web.links.load_links()[web.token_a]['email'] == 'owner@example.invalid'


@pytest.mark.parametrize('crm', [False, True])
def test_manual_branch_creation_does_not_copy_super_client(admin_contacts, crm):
    web, services = admin_contacts
    person = services.identity.web_admin('super')['person_id']
    before = {t for t, c in web.links.load_links().items() if c.get('person_id') == person}
    client = web.app.test_client()
    csrf = login(client, 'super')
    payload = settings(create=True, yclients_enabled=crm)
    if crm:
        payload['branch_id'] = 999
    response = post(client, csrf, '/admin/api/branch-settings', payload)
    assert response.status_code == 200
    identifier = response.json['settings']['branch_id']
    assert services.billing.subscription_view(identifier)['available']
    assert {t for t, c in web.links.load_links().items() if c.get('person_id') == person} == before


@pytest.mark.parametrize('email', ['profile@example.invalid', ''])
def test_crm_cannot_redirect_active_admin_email_without_local_override(state, email):
    links, _, _ = state
    links.reconcile_appointments(1, {1: {'id': 1, 'email': email}}, at=AT)
    token, _ = links.get_user_by_id_ycl(1, 1)
    with links.store.transaction() as records:
        records[token]['admin_role_active'] = True
        records[token].pop('email_override', None)
    links.reconcile_appointments(1, {1: {'id': 1, 'email': 'changed-in-crm@example.invalid',
                                         'name': 'Updated CRM name', 'phone': '+10000000002'}}, at=AT)
    client = links.load_links()[token]
    assert client['email'] == email
    assert client['user_name'] == 'Updated CRM name'
    assert client['phone'] == '+10000000002'
    with links.store.transaction() as records:
        records[token]['admin_role_active'] = False
    links.reconcile_appointments(1, {1: {'id': 1, 'email': 'ordinary-crm@example.invalid'}}, at=AT)
    assert links.load_links()[token]['email'] == 'ordinary-crm@example.invalid'
