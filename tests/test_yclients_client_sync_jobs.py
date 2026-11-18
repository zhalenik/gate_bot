"""Offline durable identity queue: all writers, scopes, races and restart recovery."""
import json
import threading
import time
from types import SimpleNamespace

import pytest

from branch_lifecycle import BranchLifecycleService
from branch_manager import BranchManager
from identity_service import IdentityService
from link_manager import LinkManager, normalize_phone
from storage.json_store import JsonStore
from storage.product_store import ProductStore
from storage.sql_documents import migrate_legacy, read_document, write_document
from yclients_client_sync_jobs import YClientsClientSyncJobs


class FakeSync:
    def __init__(self, identity):
        self.identity = identity
        self.calls = []
        self.ready = True
        self.callback = None

    def readiness(self, *_):
        return {'ready': self.ready, 'message': 'Offline fixture readiness'}

    def refresh_snapshot(self, branch_id, token, phone, expected_id, actor=None):
        assert self.identity.product.current_connection() is None
        self.calls.append((branch_id, token, phone, expected_id, actor))
        if self.callback:
            return self.callback(branch_id, token, phone, expected_id, actor)
        with self.identity.product.connect() as db:
            links = read_document(db, 'links.json')
            client = links.get(token)
            if (not client or str(client['branch_id']) != branch_id or
                    normalize_phone(client['phone']) != phone or client.get('id_ycl') != expected_id):
                return {'status': 'stale'}
            if expected_id:
                return {'status': 'unchanged', 'id_ycl': expected_id}
            client['id_ycl'] = 1000 + len(self.calls)
            write_document(db, 'links.json', links)
            return {'status': 'updated', 'id_ycl': client['id_ycl']}


@pytest.fixture
def queued(tmp_path):
    branches = {str(branch): {'branch_id': branch, 'name': str(branch), 'buttons': [],
                              'yclients_enabled': branch != 13} for branch in (11, 12, 13)}
    admins = {'owner': {'name': 'Owner', 'is_super': True, 'branch_id': None},
              'north': {'name': 'North', 'is_super': False, 'branch_id': 11}}
    links = {'historical': {'branch_id': 11, 'user_name': 'Historical', 'phone': '+15550100100',
                            'id_ycl': None, 'gate_access': True, 'mailing_enabled': False}}
    for name, data in [('branches.json', branches), ('admins.json', admins), ('links.json', links)]:
        JsonStore(tmp_path / name).replace(data)
    migrate_legacy(tmp_path, activate=True)
    product = ProductStore(tmp_path / 'application.sqlite3')
    identity = IdentityService(product)
    identity.import_legacy()
    fake = FakeSync(identity)
    clock = [time.time() + 10]
    jobs = YClientsClientSyncJobs(identity, fake, clock=lambda: clock[0])
    state = SimpleNamespace(product=product, identity=identity, fake=fake, jobs=jobs, clock=clock,
                            links=LinkManager(tmp_path / 'links.json'), branches=BranchManager(tmp_path / 'branches.json'))
    yield state
    jobs.stop(timeout=5)


def rows(state):
    with state.product.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM yclients_client_sync_jobs ORDER BY created,id')]


def add(state, *, phone='+15550100101', branch=11, identifier=None):
    return state.links.create_link({'branch_id': branch, 'phone': phone, 'user_name': 'Synthetic', 'id_ycl': identifier})


def write_client(state, token, **updates):
    with state.product.connect() as db:
        links = read_document(db, 'links.json')
        links[token].update(updates)
        write_document(db, 'links.json', links)


def test_auto_queue_is_transactional_and_does_not_import_historic_clients(queued):
    s = queued
    assert rows(s) == [] and s.jobs._thread is None and s.fake.calls == []
    local = add(s)
    add(s, phone='+15550100102', identifier=42)  # CRM/planner already supplied identity.
    add(s, phone='+15550100103', branch=13)
    with s.product.connect() as db:
        links = read_document(db, 'links.json')
        links['masked'] = {'branch_id': 11, 'phone': '+1 (555) *** 0104', 'id_ycl': None}
        write_document(db, 'links.json', links)
    assert [row['client_token'] for row in rows(s)] == [local]
    with pytest.raises(RuntimeError):
        with s.product.connect() as db:
            links = read_document(db, 'links.json')
            links['rolled-back'] = {'branch_id': 11, 'phone': '+15550100105'}
            write_document(db, 'links.json', links)
            raise RuntimeError('rollback fixture')
    assert 'rolled-back' not in s.links.load_links()
    assert [row['client_token'] for row in rows(s)] == [local]
    s.identity.import_legacy()
    assert len(rows(s)) == 1
    assert s.jobs.process_once() == 1
    assert rows(s)[0]['outcome'] == 'updated'
    assert s.links.load_links()[local]['id_ycl']


@pytest.mark.parametrize('writer', ['console', 'link'])
def test_writer_bootstraps_queue_without_application_services(queued, writer):
    s = queued
    with s.product.connect() as db:
        db.execute('DROP TABLE yclients_client_sync_jobs')
        db.execute('DROP TABLE yclients_client_sync_batches')
    if writer == 'console':
        s.identity.attach_console_account('north', '+15550100201')
    else:
        add(s)
    assert len(rows(s)) == 1
    assert rows(s)[0]['state'] == 'queued'
    assert not s.fake.calls and s.jobs._thread is None


def test_console_phone_change_requeues_all_existing_branch_cards_but_same_phone_does_not(queued):
    s = queued
    person = s.identity.attach_console_account('north', '+15550100201')
    assert s.jobs.process_once() == 1
    token = next(token for token, client in s.links.load_links().items() if client.get('person_id') == person)
    original = s.links.load_links()[token]
    with s.product.connect() as db:
        links = read_document(db, 'links.json')
        links['console-sibling'] = dict(original, branch_id=12, id_ycl=202, admin_role_active=False)
        write_document(db, 'links.json', links)
    before = s.links.load_links()
    s.identity.attach_console_account('north', '+1 (555) 010-0201')
    assert s.links.load_links()[token]['id_ycl'] == original['id_ycl']
    assert s.links.load_links()['console-sibling']['id_ycl'] == 202
    assert s.jobs.process_once() == 0
    assert len(s.fake.calls) == 1

    assert s.identity.attach_console_account('north', '+15550100209') == person
    after = s.links.load_links()
    assert set(after) == set(before)
    assert after['historical'] == before['historical']
    for key in (token, 'console-sibling'):
        assert after[key]['phone'] == '+15550100209' and after[key]['id_ycl'] is None
        assert after[key]['person_id'] == before[key]['person_id']
        assert after[key]['admin_role_active'] == before[key]['admin_role_active']
    pending = [row for row in rows(s) if row['state'] == 'queued']
    assert {row['client_token'] for row in pending} == {token, 'console-sibling'}
    assert all(row['phone'] == '+15550100209' and json.loads(row['expected_id']) is None for row in pending)
    assert s.jobs.process_once() == 2
    assert {(call[0], call[2]) for call in s.fake.calls[1:]} == {('11', '+15550100209'), ('12', '+15550100209')}
    assert all(s.links.load_links()[key]['id_ycl'] not in (None, original['id_ycl'], 202)
               for key in (token, 'console-sibling'))


def test_phone_change_supersedes_inflight_claim_without_losing_new_work(queued):
    s = queued
    token = add(s)
    def change_while_reading(*_):
        s.links.update_user(token, {'phone': '+15550100202'})
        return {'status': 'not_found'}
    s.fake.callback = change_while_reading
    assert s.jobs.process_once(limit=1) == 0
    current = rows(s)[0]
    assert current['state'] == 'queued' and current['generation'] == 2 and current['claim'] is None
    assert current['phone'] == '+15550100202'
    s.fake.callback = None
    assert s.jobs.process_once(limit=1) == 1
    assert s.fake.calls[-1][2] == '+15550100202'
    assert rows(s)[0]['state'] == 'done'


def test_unavailable_auto_retries_with_backoff_but_missing_match_is_terminal(queued):
    s = queued
    token = add(s)
    s.fake.callback = lambda *_: {'status': 'unavailable'}
    assert s.jobs.process_once() == 1
    assert rows(s)[0]['available_at'] == s.clock[0] + 60
    assert s.jobs.process_once() == 0
    s.clock[0] += 60
    assert s.jobs.process_once() == 1
    assert rows(s)[0]['available_at'] == s.clock[0] + 120
    s.fake.callback = lambda *_: {'status': 'not_found'}
    s.clock[0] += 120
    assert s.jobs.process_once() == 1
    s.clock[0] += 10000
    assert s.jobs.process_once() == 0
    assert rows(s)[0]['outcome'] == 'not_found'
    s.links.update_user(token, {'phone': '+15550100203'})
    assert rows(s)[0]['state'] == 'queued'


def test_bulk_coalesces_pending_auto_work_and_enforces_scope(queued):
    s = queued
    token = add(s)
    known = add(s, phone='+15550100204', identifier=42)
    for username, branch in [('north', '12'), ('owner', '13')]:
        with pytest.raises(PermissionError):
            s.jobs.start_batch(username, branch)
        with pytest.raises(PermissionError):
            s.jobs.batch_status(username, branch)
    batch = s.jobs.start_batch('north', '11')
    assert batch['state'] == 'queued' and batch['total'] == 3
    assert batch['id'] == s.jobs.start_batch('owner', '11')['id']
    assert len(rows(s)) == 3 and all(row['batch_id'] == batch['id'] for row in rows(s))
    assert not s.fake.calls
    assert s.jobs.process_once(limit=10) == 3
    state = s.jobs.batch_status('north', '11')
    assert state['state'] == 'complete' and state['processed'] == 3
    assert (state['updated'], state['unchanged'], state['errors']) == (2, 1, 0)
    assert {call[1] for call in s.fake.calls} == {'historical', token, known}


def test_bulk_outcomes_are_counted_and_readiness_does_not_trigger_reads(queued):
    s = queued
    tokens = ['historical'] + [add(s, phone='+1555010030' + str(i), identifier=10 + i) for i in range(5)]
    statuses = dict(zip(tokens, ['updated', 'unchanged', 'not_found', 'ambiguous', 'conflict', 'error']))
    s.fake.callback = lambda branch, token, *_: {'status': statuses[token]}
    assert s.jobs.batch_status('north', 11)['state'] == 'idle'
    s.fake.ready = False
    assert not s.jobs.batch_status('north', 11)['can_start']
    with pytest.raises(ValueError):
        s.jobs.start_batch('north', 11)
    assert not s.fake.calls
    s.fake.ready = True
    s.jobs.start_batch('north', 11)
    assert s.jobs.process_once(limit=10) == 6
    state = s.jobs.batch_status('north', 11)
    assert state['state'] == 'complete'
    assert all(state[key] == 1 for key in ('updated', 'unchanged', 'not_found', 'ambiguous', 'conflicts', 'errors'))


@pytest.mark.parametrize('manual_change', [False, True])
def test_running_auto_advances_batch_snapshot_only_for_its_own_verified_id(queued, manual_change):
    s = queued
    token = add(s)
    def start_bulk_during_auto(branch, claimed, phone, expected_id, actor):
        assert claimed == token and actor is None
        s.jobs.start_batch('north', 11)
        write_client(s, token, id_ycl=222 if manual_change else 111)
        return {'status': 'updated', 'id_ycl': 111}
    s.fake.callback = start_bulk_during_auto
    assert s.jobs.process_once(limit=1) == 1
    item = next(row for row in rows(s) if row['batch_id'] and row['client_token'] == token)
    assert json.loads(item['expected_id']) == (None if manual_change else 111)
    s.fake.callback = None
    assert s.jobs.process_once(limit=10) == 2
    state = s.jobs.batch_status('north', 11)
    assert state['errors'] == int(manual_change)
    assert s.links.load_links()[token]['id_ycl'] == (222 if manual_change else 111)


def test_revoked_actor_stops_batch_before_external_read(queued):
    s = queued
    add(s)
    s.jobs.start_batch('north', 11)
    with s.product.connect() as db:
        admins = read_document(db, 'admins.json')
        admins['north']['disabled'] = True
        write_document(db, 'admins.json', admins)
    assert s.jobs.process_once(limit=10) == 1
    assert s.fake.calls == []
    state = s.jobs.batch_status('owner', 11)
    assert state['state'] == 'failed' and state['processed'] == state['total'] == state['errors'] == 2


def test_deleted_client_and_branch_cannot_leave_pending_work(queued):
    s = queued
    token = add(s)
    s.links.delete_link(token)
    assert rows(s) == []
    token = add(s)
    s.jobs.start_batch('north', 11)
    s.links.delete_link(token)
    assert s.jobs.process_once(limit=10) == 1
    assert s.jobs.batch_status('owner', 11)['processed'] == 2
    other = add(s, branch=12)
    service = BranchLifecycleService(s.identity)
    preview = service.preview('owner', 11)
    service.delete('owner', 11, confirmation='11', revision=preview['revision'])
    assert [row['client_token'] for row in rows(s)] == [other]
    with s.product.connect() as db:
        assert db.execute('SELECT count(*) FROM yclients_client_sync_batches').fetchone()[0] == 0
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


def test_shared_person_phone_change_invalidates_only_sibling_crm_id(queued):
    s = queued
    source = s.links.load_links()['historical']
    with s.product.connect() as db:
        links = read_document(db, 'links.json')
        links['historical']['id_ycl'] = 101
        links['sibling'] = dict(source, branch_id=12, id_ycl=202)
        write_document(db, 'links.json', links)
    # A CRM update already knows the source identity. Its sibling belongs to a
    # different branch and must look up its own ID after propagated phone change.
    write_client(s, 'historical', phone='+15550100401')
    links = s.links.load_links()
    assert links['historical']['id_ycl'] == 101
    assert links['sibling']['id_ycl'] is None
    assert links['sibling']['phone'] == '+15550100401'
    assert [(row['client_token'], row['branch_id']) for row in rows(s)] == [('sibling', '12')]


def test_only_runtime_start_recovers_claims_and_worker_stops(queued):
    s = queued
    add(s)
    with s.product.connect() as db:
        db.execute('UPDATE yclients_client_sync_jobs SET state="running",claim="old-process"')
    recovered = YClientsClientSyncJobs(s.identity, s.fake, clock=lambda: s.clock[0])
    assert recovered._thread is None and rows(s)[0]['state'] == 'running'
    completed = threading.Event()
    s.fake.callback = lambda *_: (completed.set() or {'status': 'not_found'})
    try:
        recovered.start()
        assert completed.wait(5)
    finally:
        assert recovered.stop(timeout=5)
    assert len(s.fake.calls) == 1 and rows(s)[0]['state'] == 'done'
    with s.product.connect():
        with pytest.raises(RuntimeError):
            recovered.process_once()
