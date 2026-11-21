"""Offline regressions: branch registration, notification defaults and contact isolation."""
from registration_samples import agreements
import json
import re

import pytest

from identity_service import ADMIN_EVENTS, GLOBAL_ADMIN_EVENTS, DEFAULT_ADMIN_EVENTS, IdentityService
from product_notifications import ProductNotifications
from storage.sql_documents import read_document, write_document
from test_identity_product import identities, PASSWORD, PHONE, accept


@pytest.mark.parametrize('crm', [True, False])
def test_new_branch_never_copies_superadmin_client(identities, crm):
    state = identities
    owner = state.service.web_admin('owner')['person_id']
    original = {token for token, c in state.links.load_links().items() if c['person_id'] == owner}
    assert original == set()
    notices = ProductNotifications(state.service, state.links, state.branches, state.billing)
    state.service.event_sink = notices.event
    ticket = state.service.issue_bind_ticket('owner', 'max')
    state.service.bind_ticket('max', '88009', ticket)
    state.service.save_admin_preferences('owner', {'events': {'branch_registered': {'max': True}}})
    registration = state.service.register_branch('99' if crm else None, 'New branch', 'New admin', PHONE, yclients_enabled=crm)
    otp = state.service.request_otp(registration['registration_token'], registration=True, documents=agreements(state.service))
    code = re.search(r'\b[0-9]{6}\b', state.sent[-1][2]).group()
    result = state.service.complete_registration(registration['registration_token'], otp['challenge_id'], code, 'new@example.invalid', PASSWORD, documents=agreements(state.service))
    state.service.import_legacy()
    state.service.attach_console_account('owner', '+15550100999')
    assert {token for token, c in state.links.load_links().items() if c['person_id'] == owner} == original
    # Global management scope remains independent of branch client records.
    assert state.service.list_admins('owner', result['branch_id'])
    expected=set(ADMIN_EVENTS) - GLOBAL_ADMIN_EVENTS
    if not crm:
        expected.remove('marketplace_disconnected')
    assert set(state.service.admin_preferences('new@example.invalid')['events']) == expected
    # Even a repeated event from the source cannot duplicate the inbox or external effect.
    state.service.branch_registered(result['branch_id'], 'manual', result['person_id'])
    with state.product.connect() as db:
        assert [r['person_id'] for r in db.execute("SELECT person_id FROM notification_inbox WHERE event='branch_registered'")] == [owner]
        assert [r['channel'] for r in db.execute("SELECT channel FROM message_outbox WHERE event='branch_registered'")] == ['max']
    with pytest.raises(PermissionError):
        state.service.save_admin_preferences(result['username'], {'events': {'branch_registered': {'max': True}}})


def test_invited_admin_defaults_and_delivery_match(identities):
    state = identities
    accept(state)
    username = 'new-admin@example.invalid'
    ticket = state.service.issue_bind_ticket(username, 'telegram')
    state.service.bind_ticket('telegram', '88001', ticket)
    ticket = state.service.issue_bind_ticket(username, 'max')
    state.service.bind_ticket('max', '88002', ticket)
    prefs = state.service.admin_preferences(username)
    for event, channels in prefs['events'].items():
        assert channels == {'email': False, 'telegram': event in DEFAULT_ADMIN_EVENTS, 'max': False}
        contacts = [r for r in state.service.contacts_for_admin_events('11', event) if r['username'] == username]
        assert [r['channel'] for r in contacts] == (['telegram'] if event in DEFAULT_ADMIN_EVENTS else [])
    assert state.service.contacts_for_admin_events('12', 'quota_warning') == []


def test_defaults_migration_preserves_existing_effective_choices_once(identities):
    state = identities
    owner = state.service.web_admin('owner')['person_id']
    north = state.service.web_admin('north')['person_id']
    saved = {'events': {'quota_warning': {'email': True, 'telegram': False, 'max': False}}}
    state.service.save_admin_preferences('north', saved)
    with state.product.connect() as db:
        db.execute('DELETE FROM identity_migrations WHERE name="admin_notification_defaults_v2"')
        db.execute('DELETE FROM notification_preferences WHERE person_id=? AND audience="admin"', (owner,))
    upgraded = IdentityService(state.product)
    assert all(all(channels.values()) for channels in upgraded.admin_preferences('owner')['events'].values())
    assert upgraded.admin_preferences('north')['events']['quota_warning'] == saved['events']['quota_warning']
    upgraded.save_admin_preferences('owner', {'events': {}})
    assert not any(any(c.values()) for c in IdentityService(state.product).admin_preferences('owner')['events'].values())
    with state.product.connect() as db:
        assert db.execute('SELECT count(*) FROM notification_preferences WHERE audience="admin" AND person_id IN (?,?)', (owner, north)).fetchone()[0] == 2


def test_client_contact_and_confirmation_do_not_change_admin_identity(identities):
    state = identities
    accept(state)
    username = 'new-admin@example.invalid'
    person_id = state.service.web_admin(username)['person_id']
    with state.product.connect() as db:
        original = dict(db.execute('SELECT email,email_verified FROM persons WHERE id=?', (person_id,)).fetchone())
    prefs = state.service.save_prefs_for_token('example-client', {'enabled': True, 'email': 'contact@example.invalid', 'channels': {'email': True}}, actor_username=username)
    assert prefs['email'] == state.links.load_links()['example-client']['email'] == 'contact@example.invalid'
    assert not prefs['email_verified']
    state.service.verify_email_request('example-client')
    assert state.sent[-1][1] == 'contact@example.invalid'
    verification = state.sent[-1][2].rsplit('/', 1)[1]
    state.service.verify_email(verification)
    assert state.service.prefs_for_token('example-client')['email_verified']
    with state.product.connect() as db:
        assert dict(db.execute('SELECT email,email_verified FROM persons WHERE id=?', (person_id,)).fetchone()) == original
    state.links.update_user('example-client', {'email': 'edited@example.invalid'})
    assert state.service.prefs_for_token('example-client')['email'] == 'edited@example.invalid'
    assert not state.service.prefs_for_token('example-client')['email_verified']
    assert state.auth.verify_admin(username, PASSWORD)


def test_outdated_client_confirmation_does_not_verify_replacement_email(identities):
    state = identities
    state.service.save_prefs_for_token('example-client', {'enabled': True, 'email': 'first@example.invalid', 'channels': {'email': True}})
    state.service.verify_email_request('example-client')
    verification = state.sent[-1][2].rsplit('/', 1)[1]
    state.links.update_user('example-client', {'email': 'second@example.invalid'})
    with pytest.raises(ValueError):
        state.service.verify_email(verification)
    assert not state.service.prefs_for_token('example-client')['email_verified']


def test_legacy_contact_fallback_becomes_visible_without_replacing_explicit_empty(identities):
    state = identities
    person = state.links.load_links()['example-client']['person_id']
    with state.product.connect() as db:
        db.execute('UPDATE persons SET email=? WHERE id=?', ('legacy@example.invalid', person))
        links = read_document(db, 'links.json')
        links['example-client'].pop('email', None)
        write_document(db, 'links.json', links)
    state.service.import_legacy()
    assert state.links.load_links()['example-client']['email'] == 'legacy@example.invalid'
    state.links.update_user('example-client', {'email': ''})
    state.service.import_legacy()
    assert state.links.load_links()['example-client']['email'] == ''


@pytest.mark.parametrize('legacy_document', [False, True])
def test_client_email_verification_uses_sql_identity_without_changing_person_email(identities, legacy_document):
    state = identities
    token = state.links.create_link({'branch_id': 11, 'phone': '+15550100777',
                                    'user_name': 'New client', 'email': 'initial@example.invalid'})
    initial = state.service.prefs_for_token(token)
    person_id = initial['person_id']
    with state.product.connect() as db:
        original = dict(db.execute('SELECT email,email_verified FROM persons WHERE id=?', (person_id,)).fetchone())
    state.service.save_prefs_for_token(token, {'enabled': True, 'email': 'branch-contact@example.invalid',
                                             'channels': {'email': True}})
    if legacy_document:
        # Simulate an older incomplete document. SQL retains the established
        # identity; intentionally bypass the normal writer's index enrichment.
        with state.product.connect() as db:
            links = read_document(db, 'links.json')
            del links[token]['person_id']
            db.execute('UPDATE documents SET payload=? WHERE name="links.json"', (json.dumps(links),))
        assert 'person_id' not in state.links.load_links()[token]
    state.service.verify_email_request(token)
    assert state.sent[-1][1] == 'branch-contact@example.invalid'
    verification = state.sent[-1][2].rsplit('/', 1)[1]
    state.service.verify_email(verification)
    assert state.service.prefs_for_token(token)['email_verified'] is True
    with state.product.connect() as db:
        assert dict(db.execute('SELECT email,email_verified FROM persons WHERE id=?', (person_id,)).fetchone()) == original
        assert db.execute('SELECT count(*) FROM notification_preferences WHERE person_id=? AND audience="client" AND branch_id<>"11"',
                          (person_id,)).fetchone()[0] == 0
