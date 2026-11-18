"""CRM appointment identities reuse canonical local clients without merging grants."""
from copy import deepcopy
from datetime import timedelta

import pytest

from identity_service import IdentityService
from link_manager import access_is_active
from scheduler import Scheduler
from storage.product_store import ProductStore
from storage.sql_documents import migrate_legacy
from test_scheduler import state, AT, CRM, record


PHONE = '+79990000101'
OTHER_PHONE = '+15550101002'


@pytest.fixture(params=['legacy', 'sql'])
def matched_state(state, tmp_path, request):
    if request.param == 'sql':
        migrate_legacy(tmp_path, activate=True)
        IdentityService(ProductStore(tmp_path / 'application.sqlite3')).import_legacy()
    return state


def apply(links, clients):
    return links.reconcile_appointments(1, clients, at=AT, branch_timezone='Europe/Moscow')


@pytest.mark.parametrize('crm_phone', ['8 (999) 000-01-01', '9990000101', PHONE])
def test_missing_id_reuses_token_person_and_preferences_in_own_branch(matched_state, crm_phone):
    links, _, events = matched_state
    token = links.create_link({'branch_id': 1, 'phone': PHONE, 'gate_access': False,
                               'mailing_enabled': False, 'email': 'local@example.invalid'})
    foreign = links.create_link({'branch_id': 2, 'phone': PHONE, 'gate_access': False})
    links.bind_identity('telegram', '501', token=token)
    links.bind_identity('max', '502', token=token)
    before = links.load_links()
    result = apply(links, {'101': {'id': 101, 'phone': crm_phone, 'email': 'crm@example.invalid'}})
    after = links.load_links(); client = after[token]
    assert set(after) == {token, foreign} and after[foreign] == before[foreign]
    assert str(client['id_ycl']) == '101' and client.get('person_id') == before[token].get('person_id')
    assert client['tg_id'] == '501' and client['max_id'] == '502'
    assert client['email'] == 'local@example.invalid' and client['email_override']
    assert client['mailing_enabled'] is False and access_is_active(client, AT)
    assert client['access_expires'] == '2026-11-05T23:59:59.999999+03:00'
    assert result['changed'] == 1 and len(events) == 1 and events[0]['token'] == token
    snapshot = deepcopy(after)
    assert apply(links, {'101': {'id': 101, 'phone': crm_phone}})['changed'] == 0
    assert links.load_links() == snapshot and len(events) == 1


@pytest.mark.parametrize('kind', ['permanent', 'finite', 'blocked', 'admin'])
def test_binding_id_preserves_manual_and_role_grants_without_notifications(matched_state, kind):
    links, _, events = matched_state
    data = {'branch_id': 1, 'phone': PHONE, 'gate_access': kind != 'blocked',
            'mailing_enabled': False, 'admin_blocked': kind == 'blocked'}
    expiry = AT + timedelta(days=3) if kind == 'finite' else None
    token = links.create_link(data, expires_at=expiry)
    if kind == 'admin':
        with links.store.transaction() as values:
            values[token].update(gate_access=False, access_source='admin_role', admin_role_active=True)
    before = links.load_links()[token]
    assert apply(links, {'101': {'id': 101, 'phone': PHONE}})['changed'] == 0
    client = links.load_links()[token]
    for field in ('gate_access', 'access_source', 'access_expires', 'admin_blocked', 'mailing_enabled'):
        assert client[field] == before[field]
    assert client.get('admin_role_active') == before.get('admin_role_active')
    assert str(client['id_ycl']) == '101' and not events
    assert apply(links, {})['changed'] == 0
    assert links.load_links()[token] == client and not events


def test_legacy_role_with_appointment_source_is_not_revoked_by_cancellation(matched_state):
    links, _, events = matched_state
    token = links.create_link({'branch_id': 1, 'phone': PHONE, 'id_ycl': 101,
                               'access_source': 'appointment', 'gate_access': True})
    with links.store.transaction() as values:
        values[token]['admin_role_active'] = True
    before = links.load_links()[token]
    assert apply(links, {})['changed'] == 0
    assert links.load_links()[token] == before and access_is_active(before, AT) and not events


@pytest.mark.parametrize('conflict_kind', ['many_local', 'other_id', 'many_crm', 'masked_phone', 'missing_phone'])
def test_ambiguous_phone_claim_never_duplicates_or_partially_mutates(matched_state, conflict_kind):
    links, _, events = matched_state
    clean = links.create_link({'branch_id': 1, 'phone': OTHER_PHONE, 'gate_access': False})
    token = links.create_link({'branch_id': 1, 'phone': PHONE, 'gate_access': False,
                               'id_ycl': 555 if conflict_kind == 'other_id' else None})
    clients = {'201': {'id': 201, 'phone': OTHER_PHONE}, '101': {'id': 101, 'phone': PHONE}}
    if conflict_kind == 'many_local':
        links.create_link({'branch_id': 1, 'phone': PHONE, 'gate_access': False})
    elif conflict_kind == 'many_crm':
        clients['102'] = {'id': 102, 'phone': PHONE}
    elif conflict_kind == 'masked_phone':
        clients['101']['phone'] = '+7 999 XXX-XX-01'
    elif conflict_kind == 'missing_phone':
        clients['101'].pop('phone')
    before = links.load_links()
    with pytest.raises(ValueError, match='identity conflict'):
        apply(links, clients)
    assert links.load_links() == before and not events
    assert links.load_links()[clean]['id_ycl'] is None
    assert token in links.load_links()


@pytest.mark.parametrize('guest_id', [None, 101])
def test_guest_is_not_extended_or_promoted_and_one_regular_crm_card_is_reused(matched_state, guest_id):
    links, _, events = matched_state
    guest = links.create_link({'branch_id': 1, 'phone': PHONE, 'is_temporary': True,
                               'id_ycl': guest_id, 'mailing_enabled': False},
                              expires_at=AT + timedelta(minutes=20))
    before = links.load_links()[guest]
    client = {'101': {'id': 101, 'phone': PHONE}}
    assert apply(links, client)['changed'] == 1
    after = links.load_links()
    assert len(after) == 2 and after[guest] == before
    regular = next(token for token, data in after.items() if not data['is_temporary'])
    assert events[0]['token'] == regular
    assert apply(links, client)['changed'] == 0 and len(links.load_links()) == 2
    assert links.load_links()[guest] == before
    apply(links, {})
    assert links.load_links()[guest] == before
    assert links.load_links()[regular]['gate_access'] is False


def test_existing_guest_and_regular_can_share_yc_id_without_duplicate_error(matched_state):
    links, _, events = matched_state
    guest = links.create_link({'branch_id': 1, 'phone': PHONE, 'is_temporary': True, 'id_ycl': 101},
                              expires_at=AT + timedelta(minutes=20))
    assert links.get_user_by_id_ycl(101, 1) is None
    regular = links.create_link({'branch_id': 1, 'phone': PHONE, 'id_ycl': 101, 'gate_access': False})
    before = links.load_links()[guest]
    apply(links, {'101': {'id': 101, 'phone': PHONE}})
    assert len(links.load_links()) == 2 and links.load_links()[guest] == before
    assert [event['token'] for event in events] == [regular]
    assert links.get_user_by_id_ycl(101, 1)[0] == regular
    assert len(links.get_all_users_by_id_ycl(101, 1)) == 2


def test_same_phone_in_another_branch_is_never_claimed(matched_state):
    links, _, _ = matched_state
    foreign = links.create_link({'branch_id': 2, 'phone': PHONE, 'gate_access': False})
    before = links.load_links()[foreign]
    apply(links, {'101': {'id': 101, 'phone': PHONE}})
    clients = links.load_links()
    assert len(clients) == 2 and clients[foreign] == before
    own = next(data for token, data in clients.items() if token != foreign)
    assert own['branch_id'] == 1 and own['id_ycl'] == '101'


def test_distinct_existing_crm_identities_are_not_merged_by_shared_phone(matched_state):
    links, _, _ = matched_state
    clients = {'101': {'id': 101, 'phone': PHONE}, '102': {'id': 102, 'phone': PHONE}}
    apply(links, clients)
    before = links.load_links()
    assert len(before) == 2 and {data['id_ycl'] for data in before.values()} == {'101', '102'}
    assert apply(links, clients)['changed'] == 0 and links.load_links() == before


def test_cached_schedule_notices_new_unlinked_local_clients_without_crm_read(state, monkeypatch):
    links, branches, _ = state
    crm = CRM({1: [record(101)]})
    scheduler = Scheduler(links, branches, crm)
    scheduler.run_once()
    reads = list(crm.calls)
    calls = []
    original = links.reconcile_appointments
    def reconcile(branch, *args, **kwargs):
        calls.append(str(branch))
        return original(branch, *args, **kwargs)
    monkeypatch.setattr(links, 'reconcile_appointments', reconcile)
    scheduler.activate_due(); assert not calls
    local = links.create_link({'branch_id': 1, 'phone': OTHER_PHONE, 'gate_access': False})
    scheduler.activate_due()
    assert calls == ['1'] and crm.calls == reads
    assert links.load_links()[local]['id_ycl'] is None
