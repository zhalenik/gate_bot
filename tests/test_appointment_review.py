"""Independent offline regressions for local appointment activation review."""
from datetime import timedelta

from test_scheduler import state,record,CRM,AT,create
from test_appointment_timer import clock
from scheduler import Scheduler
from link_manager import access_is_active


def test_timer_activates_known_appointment_after_short_manual_grant_expires(state,monkeypatch):
    links,branches,events=state
    token=create(links,1,gate_access=True,access_source='manual')
    links.set_expiry_at(token,AT+timedelta(minutes=2));events.clear()
    scheduler=Scheduler(links,branches,CRM({1:[record(1)]}))
    scheduler.run_once()
    assert links.load_links()[token]['access_source']=='manual' and not events
    clock(monkeypatch,AT+timedelta(minutes=2,seconds=1))
    assert scheduler.activate_due()['1']['changed']==1
    current=links.load_links()[token]
    assert current['access_source']=='appointment' and access_is_active(current,AT+timedelta(minutes=2,seconds=1))
    assert current['access_expires'].startswith('2026-11-05T23:59:59') and len(events)==1


def test_one_branch_reconcile_failure_does_not_block_other_known_deadlines(state,monkeypatch):
    links,branches,events=state
    future=(AT+timedelta(hours=2,minutes=7)).isoformat()
    scheduler=Scheduler(links,branches,CRM({1:[record(1,datetime=future)],2:[record(2,branch=2,datetime=future)]}))
    scheduler.run_once()
    original=links.reconcile_appointments
    def reconcile(branch,*args,**kwargs):
        if str(branch)=='1':raise ValueError('Synthetic branch-local inconsistency')
        return original(branch,*args,**kwargs)
    monkeypatch.setattr(links,'reconcile_appointments',reconcile)
    clock(monkeypatch,AT+timedelta(minutes=7))
    result=scheduler.activate_due()
    assert result['1']['error_type']=='ValueError'
    assert result['2']['changed']==1
    assert links.get_user_by_id_ycl(2,2) is not None
    assert any(str(event['branch_id'])=='2' and event['event']=='granted' for event in events)


def test_stop_during_crm_read_does_not_publish_late_grants_or_schedule(state,monkeypatch):
    links,branches,events=state
    crm=CRM({1:[record(1)],2:[record(2,branch=2)]})
    scheduler=Scheduler(links,branches,crm)
    original=crm.records_for_day
    def read(branch,day):
        records=original(branch,day)
        if day==AT.date()+timedelta(days=1):
            scheduler.stop()
        return records
    monkeypatch.setattr(crm,'records_for_day',read)
    assert scheduler.run_once()=={}
    assert scheduler.activate_due()=={}
    assert not links.load_links() and not events
    assert crm.calls==[(1,AT.date()),(1,AT.date()+timedelta(days=1))]
    with scheduler.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM appointment_schedule').fetchone()[0]==0
