"""Deadline behavior independent of slow CRM polling; all clocks/transports are fake."""
from datetime import timedelta
from unittest.mock import Mock

import pytest

from test_scheduler import state, record, CRM, AT
from test_product_integration import product
from scheduler import Scheduler
from link_manager import set_notify_hook
from storage import config
from yclients_api import YClientsError, eligible_clients


def clock(monkeypatch, at):
    monkeypatch.setattr('pass_notifications.now', lambda: at)
    monkeypatch.setattr('scheduler.now', lambda tz=None: at)
    monkeypatch.setattr('link_manager.now', lambda tz=None: at)


def test_future_deadline_fires_without_another_crm_poll_and_survives_restart(state, monkeypatch):
    links, branches, events = state
    crm = CRM({1: [record(1, datetime=(AT + timedelta(hours=2, minutes=7)).isoformat())]})
    scheduler = Scheduler(links, branches, crm)
    scheduler.run_once()
    assert not events
    calls = list(crm.calls)
    scheduler = Scheduler(links, branches, crm)
    clock(monkeypatch, AT + timedelta(minutes=6, seconds=29))
    assert scheduler.activate_due()['1']['changed'] == 0
    clock(monkeypatch, AT + timedelta(minutes=6, seconds=30))
    assert scheduler.activate_due()['1']['changed'] == 1
    assert scheduler.activate_due()['1']['changed'] == 0
    assert len(events) == 1 and events[0]['event'] == 'granted'
    assert crm.calls == calls


@pytest.mark.parametrize('unavailable', ['cancelled', 'failed', 'frozen', 'disabled', 'stale'])
def test_cancelled_or_untrusted_schedule_cannot_activate_future_grants(state, monkeypatch, unavailable):
    links, branches, events = state
    crm = CRM({1: [record(1, datetime=(AT + timedelta(hours=2, minutes=7)).isoformat())]})
    scheduler = Scheduler(links, branches, crm)
    scheduler.run_once()
    if unavailable in {'cancelled', 'failed'}:
        crm.by_branch[1] = [] if unavailable == 'cancelled' else YClientsError('offline')
        scheduler.run_once()
    elif unavailable in {'frozen', 'disabled'}:
        with branches.store.transaction() as values:
            values['1'].update({'yclients_marketplace_state': 'frozen'} if unavailable == 'frozen' else {'enabled': False})
    clock(monkeypatch, AT + timedelta(minutes=31 if unavailable == 'stale' else 7))
    scheduler.activate_due()
    assert not events and not links.load_links()


def test_early_morning_visit_grants_previous_evening_until_branch_expiry(state, monkeypatch):
    links, branches, events = state
    at = AT.replace(hour=22, minute=30)
    visit = (AT + timedelta(days=1)).replace(hour=0, minute=30)
    clock(monkeypatch, at)
    crm = CRM({1: [record(1, datetime=visit.isoformat())]})
    result = Scheduler(links, branches, crm).run_once()
    assert result['1']['changed'] == 1
    assert events[0]['access_expires'] == (visit+timedelta(hours=8)).isoformat()
    assert (1, visit.date()) in crm.calls


def test_multiple_visits_keep_latest_expiry_independent_of_record_order():
    at = AT.replace(hour=23)
    records = [record(1, datetime=(AT + timedelta(days=1)).replace(hour=0).isoformat()), record(1)]
    for ordered in (records, list(reversed(records))):
        assert eligible_clients(ordered, 1, at, 'Europe/Moscow')['1']['_access_expires'].startswith('2026-11-06')


def test_access_and_personal_link_are_committed_and_sent_before_two_hour_deadline(product, monkeypatch):
    p = product
    clock(monkeypatch, AT)
    monkeypatch.setattr(config, 'OUTBOUND_ENABLED', True)
    with p.links.store.transaction() as links:
        links[p.token].update(gate_access=False, id_ycl='1', access_source='appointment')
    out = p.services.notifications
    out.sender = Mock(return_value={'status': 'accepted'})
    set_notify_hook(out.access_transition)
    item = record(1, branch=10, datetime=(AT + timedelta(hours=2, minutes=7)).isoformat())
    item['client']['phone'] = p.links.load_links()[p.token]['phone']
    scheduler = Scheduler(p.links, p.branches, CRM({10: [item]}))
    scheduler.run_once()
    clock(monkeypatch, AT + timedelta(minutes=6, seconds=30))
    scheduler.activate_due()
    out.process_once()
    out.process_once()
    messages = [call for call in out.sender.call_args_list if call.args[3]['event'] == 'access_granted']
    assert len(messages) == 1 and '/opn/' + p.token in messages[0].args[2]
    with out.store.connect() as db:
        rows = db.execute('SELECT state FROM message_outbox WHERE event="access_granted"').fetchall()
    assert [r['state'] for r in rows] == ['accepted']


def test_outbox_failure_rolls_back_timer_grant_and_retries_from_same_snapshot(product, monkeypatch):
    p = product
    clock(monkeypatch, AT)
    item = record(1, branch=10, datetime=(AT + timedelta(hours=2, minutes=7)).isoformat())
    scheduler = Scheduler(p.links, p.branches, CRM({10: [item]}))
    scheduler.run_once()
    clock(monkeypatch, AT + timedelta(minutes=7))
    def failed(_):
        raise OSError('synthetic outbox failure')
    set_notify_hook(failed)
    assert scheduler.activate_due()['10']['error_type'] == 'OSError'
    assert not p.links.get_user_by_id_ycl(1, 10)
    events = []
    set_notify_hook(events.append)
    scheduler.activate_due()
    assert len(events) == 1 and events[0]['gate_access']
