"""Business-rule and failure-isolation tests for read-only reconciliation."""
from copy import deepcopy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from branch_manager import BranchManager
from link_manager import LinkManager, access_is_active, set_notify_hook
from scheduler import Scheduler
from storage import config
from yclients_api import YClientsError, eligible_clients


AT = datetime(2026, 11, 5, 12, tzinfo=ZoneInfo("Europe/Moscow"))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(config, "ACCESS_BEFORE_VISIT_HOURS", 2)
    monkeypatch.setattr("scheduler.now", lambda tz=None: AT.astimezone(ZoneInfo(tz or "Europe/Moscow")))
    monkeypatch.setattr("link_manager.now", lambda tz=None: AT.astimezone(ZoneInfo(tz or "Europe/Moscow")))
    links = LinkManager(tmp_path / "links.json")
    branches = BranchManager(tmp_path / "branches.json")
    branches.save_branches({str(i): {"branch_id": i, "timezone": "Europe/Moscow", "buttons": []} for i in (1, 2)})
    events = []
    set_notify_hook(events.append)
    yield links, branches, events
    set_notify_hook(None)


def record(client_id, hour=13, branch=1, **kwargs):
    return {"id": client_id, "company_id": branch,
            "datetime": f"2026-11-05T{hour:02d}:00:00+03:00",
            "client": {"id": client_id, "name": "Synthetic", "phone": "+10000000001"}, **kwargs}


@pytest.mark.parametrize('email', ['local@example.invalid', ''])
def test_crm_keeps_explicit_local_email_and_clear_without_changing_access_rules(state, email):
    links, branches, events = state
    links.reconcile_appointments(1, {1: {'id': 1, 'email': 'crm@example.invalid'}}, at=AT)
    token, _ = links.get_user_by_id_ycl(1, 1)
    links.update_user(token, {'email': email})
    links.toggle_user_access(token, False)
    links.reconcile_appointments(1, {1: {
        'id': 1, 'name': 'Updated by CRM', 'phone': '+10000000002',
        'email': 'changed-in-crm@example.invalid',
    }}, at=AT)
    contact = links.load_links()[token]
    assert contact['email'] == email
    assert contact['email_override'] is True
    assert contact['user_name'] == 'Updated by CRM'
    assert contact['phone'] == '+10000000002'
    assert contact['admin_blocked'] is True
    assert contact['gate_access'] is False


def test_crm_email_without_local_override_still_updates_and_clears(state):
    links, branches, events = state
    for email in ('crm@example.invalid', 'changed@example.invalid', ''):
        links.reconcile_appointments(1, {1: {'id': 1, 'email': email}}, at=AT)
        token, contact = links.get_user_by_id_ycl(1, 1)
        assert contact['email'] == email
        assert not contact.get('email_override')
    links.reconcile_appointments(1, {1: {'id': 1}}, at=AT)
    assert links.load_links()[token]['email'] == ''


@pytest.mark.parametrize('email,expected', [('local@example.invalid', 'local@example.invalid'), ('', 'crm@example.invalid')])
def test_only_nonempty_email_on_manual_creation_overrides_crm(state, email, expected):
    links, branches, events = state
    token = links.create_link({'id_ycl': 1, 'branch_id': 1, 'email': email})
    links.reconcile_appointments(1, {1: {'id': 1, 'email': 'crm@example.invalid'}}, at=AT)
    contact = links.load_links()[token]
    assert contact['email'] == expected
    assert contact['email_override'] is bool(email)


class CRM:
    def __init__(self, by_branch):
        self.by_branch, self.calls = by_branch, []

    def records_for_day(self, branch, day):
        self.calls.append((branch, day))
        value = self.by_branch.get(branch, [])
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


@pytest.mark.parametrize('fail_read',[False,True])
def test_branch_deleted_during_crm_read_is_not_recreated(state,fail_read):
    links,branches,events=state
    class DeletingCRM:
        def records_for_day(self,branch,day):
            if branch==1:
                branches.delete_branch(1)
                if fail_read:raise YClientsError('synthetic transport failure')
                return [record(99)]
            return []
    notices=[]
    scheduler=Scheduler(links,branches,DeletingCRM(),event_sink=lambda *args:notices.append(args))
    scheduler.run_once()
    assert branches.get_branch(1) is None
    assert not any(str(client.get('branch_id'))=='1' for client in links.load_links().values())
    with scheduler.store.connect() as db:
        assert db.execute('SELECT count(*) FROM appointment_schedule WHERE branch_id="1"').fetchone()[0]==0
    assert not any(str(args[1])=='1' for args in notices)


def create(links, client_id, branch=1, **kwargs):
    return links.create_link({"branch_id": branch, "id_ycl": client_id, **kwargs})


def test_access_starts_two_hours_before_and_ends_after_configured_duration(state):
    links, _, _ = state
    clients = eligible_clients([record(1, 14), record(2, 15), record(3, 8)], 1, AT, "Europe/Moscow")
    assert set(clients) == {"1", "3"}
    links.reconcile_appointments(1, clients, at=AT, branch_timezone="Europe/Moscow")
    entries = links.load_links()
    assert len(entries) == 2
    assert {data["id_ycl"]:data["access_expires"] for data in entries.values()} == {
        "1":"2026-11-05T22:00:00+03:00", "3":"2026-11-05T16:00:00+03:00"}
    assert all(access_is_active(data, AT) for data in entries.values())
    assert all(not access_is_active(data, AT + timedelta(days=1)) for data in entries.values())


@pytest.mark.parametrize("flags", [{"deleted": True}, {"deleted": 1}, {"deleted": "1"},
                                  {"attendance": -1}, {"attendance": "-1"}])
def test_deleted_or_no_show_records_never_grant_access(state, flags):
    assert eligible_clients([record(1, **flags)], 1, AT, "Europe/Moscow") == {}


def test_local_calendar_day_is_not_utc_day(state):
    local = datetime(2026, 11, 5, 0, 30, tzinfo=ZoneInfo("Asia/Vladivostok"))
    records = [record(1, datetime="2026-11-04T16:00:00Z"),
               record(2, datetime="2026-11-05T16:00:00Z")]
    assert set(eligible_clients(records, 1, local, "Asia/Vladivostok")) == {"1"}
    links, _, _ = state
    links.reconcile_appointments(1, {"1": {"id": 1}}, at=local, branch_timezone="Asia/Vladivostok")
    assert next(iter(links.load_links().values()))["access_expires"] == "2026-11-05T08:30:00+10:00"


def test_manual_unlimited_and_admin_block_take_precedence_over_crm(state):
    links, _, _ = state
    permanent = create(links, 1, gate_access=True, access_source="manual")
    blocked = create(links, 2, gate_access=False, admin_blocked=True, access_source="manual")
    clients = eligible_clients([record(1), record(2), record(3)], 1, AT, "Europe/Moscow")
    links.reconcile_appointments(1, clients, at=AT, branch_timezone="Europe/Moscow")
    current = links.load_links()
    assert current[permanent]["access_expires"] is None
    assert current[permanent]["access_source"] == "manual" and access_is_active(current[permanent], AT)
    assert current[blocked]["admin_blocked"] and not current[blocked]["gate_access"]
    assert links.get_user_by_id_ycl(3, 1)[1]["access_source"] == "appointment"


def test_cancellation_revokes_only_appointment_access_in_same_branch(state):
    links, _, _ = state
    links.reconcile_appointments(1, {1: {"id": 1}}, at=AT, branch_timezone="Europe/Moscow")
    links.reconcile_appointments(2, {1: {"id": 1}}, at=AT, branch_timezone="Europe/Moscow")
    manual = create(links, 99, access_source="manual", gate_access=True)
    links.reconcile_appointments(1, {}, at=AT, branch_timezone="Europe/Moscow")
    assert not links.get_user_by_id_ycl(1, 1)[1]["gate_access"]
    assert links.get_user_by_id_ycl(1, 2)[1]["gate_access"]
    assert links.load_links()[manual]["gate_access"]


def test_repeated_snapshot_is_idempotent_and_mailing_opt_out_does_not_block_access(state):
    links, _, events = state
    token = create(links, 1, gate_access=False, access_source="appointment", mailing_enabled=False)
    eligible = {"1": {"id": 1}}
    links.reconcile_appointments(1, eligible, at=AT, branch_timezone="Europe/Moscow")
    first = links.load_links()
    result = links.reconcile_appointments(1, eligible, at=AT, branch_timezone="Europe/Moscow")
    assert result["changed"] == 0 and links.load_links() == first
    assert first[token]["gate_access"] and not first[token]["mailing_enabled"]
    assert len(events) == 1 and events[0]["mailing_enabled"] is False


def test_crm_contact_change_resets_bindings_but_omitted_fields_are_preserved(state):
    links, _, _ = state
    token = create(links, 1, phone="+10000000001", email="synthetic@example.test", user_name="Before")
    links.bind_identity("max", 101, token=token)
    links.bind_identity("telegram", 202, token=token)
    links.reconcile_appointments(1, {1: {"id": 1}}, at=AT, branch_timezone="Europe/Moscow")
    current = links.load_links()[token]
    assert current["phone"] == "+10000000001" and current["max_id"] == "101"
    links.reconcile_appointments(1, {1: {"id": 1, "name": "Changed", "phone": "+10000000002"}}, at=AT, branch_timezone="Europe/Moscow")
    current = links.load_links()[token]
    assert current["phone"] == "+10000000002" and current["user_name"] == "Changed"
    assert current["tg_id"] is None and current["max_id"] is None
    assert current["email"] == "synthetic@example.test"


def test_incomplete_crm_snapshot_preserves_grants_and_other_branch_still_syncs(state):
    links, branches, _ = state
    links.reconcile_appointments(1, {1: {"id": 1}}, at=AT, branch_timezone="Europe/Moscow")
    before = deepcopy(links.get_user_by_id_ycl(1, 1)[1])
    crm = CRM({1: YClientsError("Incomplete pagination"), 2: [record(2, branch=2)]})
    result = Scheduler(links, branches, crm).run_once()
    assert result["1"]["status"] == "error" and result["2"]["status"] == "success"
    assert links.get_user_by_id_ycl(1, 1)[1] == before
    assert links.get_user_by_id_ycl(2, 2)[1]["gate_access"]


def test_bad_date_or_cross_branch_payload_does_not_revoke_valid_grant(state):
    links, branches, _ = state
    links.reconcile_appointments(1, {1: {"id": 1}}, at=AT, branch_timezone="Europe/Moscow")
    before = deepcopy(links.get_user_by_id_ycl(1, 1)[1])
    for bad in [record(9, datetime="bad-date"), record(9, branch=2)]:
        result = Scheduler(links, branches, CRM({1: [bad]})).run_once()
        assert result["1"]["status"] == "error"
        assert links.get_user_by_id_ycl(1, 1)[1] == before


def test_duplicate_crm_identity_aborts_transaction_without_partial_changes(state):
    links, _, _ = state
    create(links, 1)
    create(links, 1)
    before = links.load_links()
    with pytest.raises(ValueError, match="Duplicate"):
        links.reconcile_appointments(1, {1: {"id": 1}, 2: {"id": 2}}, at=AT, branch_timezone="Europe/Moscow")
    assert links.load_links() == before


def test_only_explicitly_temporary_expired_users_are_deleted(state):
    links, _, _ = state
    named_guest = create(links, 1, user_name="Гость", gate_access=True)
    temp = links.create_link({"branch_id": 1, "is_temporary": True}, expires_at=AT - timedelta(seconds=1))
    links.cleanup_expired_accesses()
    assert links.cleanup_guest_users() == 1
    assert temp not in links.load_links() and named_guest in links.load_links()


def test_disabled_branch_does_not_call_crm(state):
    links, branches, _ = state
    data = branches.load_branches()["1"]
    data["enabled"] = False
    branches.update_branch(1, data)
    crm = CRM({})
    result = Scheduler(links, branches, crm).run_once()
    assert "1" not in result and crm.calls == [(2, AT.date()), (2, AT.date() + timedelta(days=1))]


def test_malformed_temporary_expiry_is_preserved_without_stopping_other_branches(state):
    links, branches, _ = state
    token = create(links, None, is_temporary=True)
    with links.store.transaction() as data:
        data[token]["access_expires"] = "malformed-legacy-value"
    result = Scheduler(links, branches, CRM({2: [record(2, branch=2)]})).run_once()
    assert token in links.load_links()
    assert result["2"]["status"] == "success"
    assert links.get_user_by_id_ycl(2, 2)[1]["gate_access"]


def test_staff_sync_reuses_branch_card_preserves_access_and_manual_role(state):
    links,branches,events=state
    token=links.create_link({'branch_id':1,'phone':'+15550100301','gate_access':False,'admin_blocked':True})
    crm=CRM({})
    rows=[{'id':'21','name':'Same','phone':'+15550100301','email':''},
          {'id':'22','name':'New','phone':'+15550100302','email':''},
          {'id':'23','name':'No phone','phone':'','email':''}]
    crm.staff_for_branch=lambda branch:rows if branch==1 else []
    scheduler=Scheduler(links,branches,crm)
    status=scheduler.run_once()
    assert status['1']['staff']=={'created':1,'matched':1,'skipped':1}
    cards=links.load_links()
    assert len(cards)==2 and cards[token]['client_role']=='employee' and not access_is_active(cards[token])
    assert all(not access_is_active(card) and card.get('id_ycl') is None for card in cards.values())
    with links.store.transaction() as data:data[token].update(role_override=True,client_role='client')
    assert scheduler.run_once()['1']['staff']['created']==0
    assert links.get_link(token)['client_role']=='client'
    crm.staff_for_branch=lambda branch:(_ for _ in ()).throw(YClientsError('Staff access denied'))
    assert scheduler.run_once()['1']['status']=='success'
    assert len(links.load_links())==2
