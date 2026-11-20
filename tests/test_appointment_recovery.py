"""Small SQL regressions for startup verification and missed appointment grants."""
from copy import deepcopy
from datetime import datetime, timedelta

import pytest

from identity_service import IdentityService
from link_manager import access_is_active, set_notify_hook
from scheduler import Scheduler
from storage import config
from storage.product_store import ProductStore
from storage.sql_documents import migrate_legacy
from test_appointment_timer import clock
from test_scheduler import AT, CRM, record, state
from yclients_api import YClientsError


@pytest.fixture
def sql_state(state, tmp_path):
    migrate_legacy(tmp_path, activate=True)
    IdentityService(ProductStore(tmp_path / 'application.sqlite3')).import_legacy()
    return state


def appointment(client_id, hour=13, **kwargs):
    item = record(client_id, hour, **kwargs)
    item['client']['phone'] = f'+1555000{client_id:04d}'
    return item


class CalendarCRM(CRM):
    def records_for_day(self, branch, day):
        return [item for item in super().records_for_day(branch, day)
                if datetime.fromisoformat(item['datetime']).date() == day]


def expected_reads():
    return [(branch, day) for branch in (1, 2)
            for day in (AT.date(), AT.date() + timedelta(days=1))]


@pytest.mark.parametrize('reconnect_immediately',[False,True])
def test_uninstall_during_poll_discards_snapshot_and_cached_deadlines(sql_state,monkeypatch,reconnect_immediately):
    from yclients_marketplace import MarketplaceService
    links,branches,events=sql_state
    with branches.store.transaction() as data:
        data['1'].update(yclients_connection_mode='marketplace',yclients_marketplace_state='active')
    scheduler=Scheduler(links,branches,CalendarCRM({1:[appointment(31,hour=15)]}))
    scheduler.run_once()
    assert not links.load_links()
    marketplace=MarketplaceService(IdentityService(scheduler.store),application_id='42',partner_token='offline-partner')

    class InterruptedPoll(CalendarCRM):
        def records_for_day(self,branch,day):
            result=super().records_for_day(branch,day)
            if branch==1 and day>AT.date():
                marketplace.disconnect({'partner_token':'offline-partner','application_id':42,'event':'uninstall','salon_id':1})
                if reconnect_immediately:
                    with branches.store.transaction() as data:
                        data['1']['yclients_marketplace_state']='active'
            return result

    scheduler.crm=InterruptedPoll({1:[appointment(32,hour=13)]})
    scheduler.run_once()
    clock(monkeypatch,AT+timedelta(hours=1))
    scheduler.activate_due()
    assert not links.load_links() and not events
    with scheduler.store.connect() as db:
        assert db.execute('SELECT active FROM appointment_schedule WHERE branch_id="1"').fetchone()[0]==0


def test_start_requires_fresh_crm_before_cached_deadline_and_polls_before_wait(sql_state, monkeypatch):
    links, branches, events = sql_state
    crm = CalendarCRM({1: [appointment(1, datetime=(AT + timedelta(hours=2, minutes=7)).isoformat())]})
    Scheduler(links, branches, crm).run_once()
    assert not links.load_links() and not events
    clock(monkeypatch, AT + timedelta(minutes=7))
    crm.by_branch[1] = []  # Cancelled while GateBot was stopped.
    crm.calls.clear()
    scheduler = Scheduler(links, branches, crm)
    started = []

    class ControlledThread:
        def __init__(self, target, name, daemon):
            self.target, self.name, self.alive = target, name, False

        def is_alive(self):
            return self.alive

        def start(self):
            with scheduler.store.connect() as db:
                assert db.execute('SELECT count(*) FROM appointment_schedule WHERE active=1').fetchone()[0] == 0
            self.alive = True
            started.append(self.name)

        def join(self, timeout=None):
            self.alive = False

    monkeypatch.setattr('scheduler.threading.Thread', ControlledThread)
    monkeypatch.setattr(config, 'SCHEDULER_ENABLED', True)
    scheduler.start()
    assert started == ['gatebot-scheduler', 'gatebot-access-timer']
    assert scheduler.activate_due() == {} and not events and not crm.calls

    class OneCycle:
        stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            assert crm.calls == expected_reads()  # No initial interval before GET.
            self.stopped = True
            return True

    scheduler._stop = OneCycle()
    scheduler._run()
    assert not links.load_links() and not events
    with scheduler.store.connect() as db:
        assert db.execute('SELECT count(*) FROM appointment_schedule WHERE active=1').fetchone()[0] == 2
    scheduler.start()  # Already running: do not invalidate a verified schedule again.
    with scheduler.store.connect() as db:
        assert db.execute('SELECT count(*) FROM appointment_schedule WHERE active=1').fetchone()[0] == 2
    scheduler.stop()


@pytest.mark.parametrize('existing', [False, True], ids=['missing-client', 'inactive-pass'])
def test_every_poll_catches_up_past_and_next_two_hours_without_repeat_notifications(sql_state, existing):
    links, branches, events = sql_state
    saved_token = None
    if existing:
        saved_token = links.create_link({'branch_id': 1, 'id_ycl': 1,
            'phone': appointment(1)['client']['phone'], 'gate_access': False,
            'access_source': 'appointment', 'mailing_enabled': False})
    blocked = links.create_link({'branch_id': 1, 'id_ycl': 7,
        'phone': appointment(7)['client']['phone'], 'gate_access': False, 'admin_blocked': True})
    manual = links.create_link({'branch_id': 1, 'id_ycl': 8,
        'phone': appointment(8)['client']['phone'], 'gate_access': True, 'access_source': 'manual'})
    records = [appointment(1, 8), appointment(2, 12), appointment(3, 14),
               appointment(4, datetime=AT.replace(hour=14, minute=1).isoformat()),
               appointment(5, deleted=True), appointment(6, attendance=-1),
               appointment(7), appointment(8)]
    crm = CalendarCRM({1: records})
    scheduler = Scheduler(links, branches, crm)
    assert scheduler.run_once()['1']['changed'] == 3
    assert crm.calls == expected_reads()
    for client_id in (1, 2, 3):
        token, client = links.get_user_by_id_ycl(client_id, 1)
        assert access_is_active(client, AT)
        assert client['access_expires'] == '2026-11-05T23:59:59.999999+03:00'
        if client_id == 1 and existing:
            assert token == saved_token and client['mailing_enabled'] is False
    for client_id in (4, 5, 6):
        assert links.get_user_by_id_ycl(client_id, 1) is None
    assert not access_is_active(links.load_links()[blocked], AT)
    assert links.load_links()[manual]['access_source'] == 'manual'
    assert links.load_links()[manual]['access_expires'] is None
    before = deepcopy(links.load_links())
    assert scheduler.run_once()['1']['changed'] == 0
    assert crm.calls == expected_reads() * 2 and links.load_links() == before
    assert len(events) == 3
    # A newly added past visit must also be discovered by the next real poll.
    crm.by_branch[1].append(appointment(9, 9))
    assert scheduler.run_once()['1']['changed'] == 1
    assert crm.calls == expected_reads() * 3
    assert access_is_active(links.get_user_by_id_ycl(9, 1)[1], AT) and len(events) == 4


def test_network_recovery_grants_missed_visit_even_after_visit_time(sql_state, monkeypatch):
    links, branches, events = sql_state
    saved = links.create_link({'branch_id': 1, 'id_ycl': 99,
        'phone': appointment(99)['client']['phone'], 'gate_access': True,
        'access_source': 'appointment'}, expires_at='2026-11-05T23:59:59.999999+03:00')
    before = deepcopy(links.load_links()[saved])
    crm = CalendarCRM({1: YClientsError('synthetic network failure')})
    scheduler = Scheduler(links, branches, crm)
    assert scheduler.run_once()['1']['status'] == 'error'
    assert links.load_links()[saved] == before and not events
    later = AT.replace(hour=16)
    clock(monkeypatch, later)
    crm.by_branch[1] = [appointment(1, 13), appointment(99, 11)]
    assert scheduler.run_once()['1']['changed'] == 1
    client = links.get_user_by_id_ycl(1, 1)[1]
    assert access_is_active(client, later)
    assert client['access_expires'] == '2026-11-05T23:59:59.999999+03:00'
    assert access_is_active(links.load_links()[saved], later)
    assert scheduler.run_once()['1']['changed'] == 0 and len(events) == 1


def test_local_outbox_failure_rolls_back_and_next_poll_recovers_once(sql_state, monkeypatch):
    links, branches, events = sql_state
    crm = CalendarCRM({1: [appointment(1, 13)]})
    scheduler = Scheduler(links, branches, crm)
    with scheduler.store.connect() as db:
        db.execute('CREATE TABLE recovery_test_outbox(token TEXT PRIMARY KEY)')

    def failed_delivery(event):
        with scheduler.store.connect() as db:
            db.execute('INSERT INTO recovery_test_outbox VALUES(?)', (event['token'],))
        raise OSError('synthetic outbox failure')

    set_notify_hook(failed_delivery)
    assert scheduler.run_once()['1']['status'] == 'error'
    assert links.get_user_by_id_ycl(1, 1) is None
    with scheduler.store.connect() as db:
        assert db.execute('SELECT count(*) FROM recovery_test_outbox').fetchone()[0] == 0

    def deliver(event):
        with scheduler.store.connect() as db:
            db.execute('INSERT INTO recovery_test_outbox VALUES(?)', (event['token'],))
        events.append(event)

    clock(monkeypatch, AT.replace(hour=14))
    set_notify_hook(deliver)
    assert scheduler.run_once()['1']['changed'] == 1
    token, client = links.get_user_by_id_ycl(1, 1)
    assert access_is_active(client, AT.replace(hour=14))
    assert client['access_expires'] == '2026-11-05T23:59:59.999999+03:00'
    assert scheduler.run_once()['1']['changed'] == 0 and len(events) == 1
    with scheduler.store.connect() as db:
        assert [row['token'] for row in db.execute('SELECT token FROM recovery_test_outbox')] == [token]
