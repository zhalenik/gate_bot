"""Authorization, tenant boundaries, transactional storage and notification state."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from pathlib import Path
import threading
from zoneinfo import ZoneInfo

import pytest

from access_service import AccessService
from branch_manager import BranchManager
from link_manager import LinkManager, access_is_active, normalize_phone, set_notify_hook
from runtime_state import RuntimeState
from storage import config
from storage.json_store import JsonStore
import logger


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
    links = LinkManager(tmp_path / "links.json")
    branches = BranchManager(tmp_path / "branches.json")
    branches.save_branches({str(i): {"branch_id": i, "name": f"Branch {i}",
                            "timezone": "Europe/Moscow", "enabled": True,
                            "buttons": [{"type": "gate", "phone": f"1000{i}", "label": "Gate"},
                                        {"type": "admin", "phone": f"2000{i}", "label": "Admin"}]}
                            for i in (1, 2)})
    calls = []

    def opener(phone, **kwargs):
        calls.append((phone, kwargs))
        return {"status": "success", "delivery": "queued", "command_id": str(len(calls)),
                "confirmed": False, "message": "Accepted"}

    service = AccessService(links, branches, opener=opener, cooldown_seconds=30)
    set_notify_hook(None)
    yield links, branches, service, calls
    set_notify_hook(None)


def create(links, branch=1, **data):
    return links.create_link({"branch_id": branch, "user_name": "Synthetic client",
                              "gate_access": True, **data})


def gate(branches, branch=1):
    return next(b for b in branches.get_branch(branch).buttons if b.type == "gate").id


def test_bearer_and_messenger_cannot_open_other_branch(core):
    links, branches, service, calls = core
    token = create(links)
    assert service.bind_token("max", 101, token)["status"] == "success"
    assert service.open_for_token(token, gate(branches, 2))["http_status"] == 403
    assert service.open_for_identity("max", 101, 2, gate(branches, 2))["http_status"] == 403
    assert service.open_for_identity("telegram", 101, 1, gate(branches))["http_status"] == 403
    assert not calls
    assert service.open_for_identity("max", 101, 1, gate(branches))["status"] == "success"
    assert calls == [("10001", {"branch_id": 1})]


def test_menu_has_only_bound_branches_and_no_gate_phone(core):
    links, branches, service, _ = core
    token = create(links)
    create(links, branch=2)
    service.bind_token("telegram", 101, token)
    menu = service.menu_for_identity("telegram", 101)
    assert [item["branch_id"] for item in menu] == ["1"]
    assert all("phone" not in b for b in menu[0]["buttons"] if b["type"] == "gate")
    assert any(b["phone"] == "20001" for b in menu[0]["buttons"] if b["type"] == "admin")


def test_disabled_branch_and_stale_button_fail_closed(core):
    links, branches, service, calls = core
    token = create(links)
    old_button = gate(branches)
    data = branches.load_branches()["1"]
    data["buttons"][0]["phone"] = "30001"
    assert branches.update_branch(1, data)
    assert service.open_for_token(token, old_button)["http_status"] == 403
    data["enabled"] = False
    branches.update_branch(1, data)
    assert service.open_for_token(token, gate(branches))["http_status"] == 403
    assert not calls


def test_expired_blocked_missing_or_invalid_permissions_never_dial(core):
    links, branches, service, calls = core
    tokens = [create(links, admin_blocked=True), create(links, gate_access=False), create(links, branch=9)]
    expired = links.create_link({"branch_id": 1}, expires_at="2000-01-01T00:00:00+03:00")
    malformed = create(links)
    with links.store.transaction() as data:
        data[malformed]["access_expires"] = "not-a-date"
    for token in tokens + [expired, malformed, "not-present", None]:
        assert service.open_for_token(token, gate(branches))["http_status"] == 403
    assert not calls


def test_blocking_permission_keeps_admin_contact_in_menu(core):
    links, _, service, calls = core
    token = create(links)
    service.bind_token("max", 101, token)
    links.toggle_user_access(token, False)
    menu = service.menu_for_identity("max", 101)
    assert not menu[0]["active"]
    assert [b["type"] for b in menu[0]["buttons"]] == ["admin"]
    assert not calls


@pytest.mark.parametrize("phone", [None, "", " ", "+", "abc", "123"])
def test_blank_or_invalid_phone_never_matches_blank_contacts(core, phone):
    links, _, service, _ = core
    token = create(links, phone="")
    assert service.bind_verified_phone("max", 101, phone)["status"] == "error"
    assert links.load_links()[token]["max_id"] is None


def test_verified_phone_binds_all_matching_branches_but_token_binds_one(core):
    links, _, service, _ = core
    first = create(links, phone="+10000000001")
    second = create(links, branch=2, phone="+10000000001")
    unrelated = create(links, branch=2, phone="+10000000002")
    assert service.bind_token("max", 101, first)["count"] == 1
    assert links.load_links()[second]["max_id"] is None
    assert service.bind_verified_phone("telegram", 202, "1 (000) 000-00-01")["count"] == 2
    state = links.load_links()
    assert state[first]["tg_id"] == state[second]["tg_id"] == "202"
    assert state[unrelated]["tg_id"] is None


def test_binding_conflict_rolls_back_every_candidate(core):
    links, _, service, _ = core
    first = create(links, phone="+10000000001")
    second = create(links, branch=2, phone="+10000000001")
    links.bind_identity("max", 999, token=second)
    before = links.load_links()
    assert service.bind_verified_phone("max", 101, "+10000000001")["status"] == "error"
    assert links.load_links() == before and before[first]["max_id"] is None


def test_phone_change_revokes_messenger_binding_but_formatting_change_does_not(core):
    links, _, _, _ = core
    token = create(links, phone="+10000000001")
    links.bind_identity("max", 101, token=token)
    links.bind_identity("telegram", 202, token=token)
    links.update_user(token, {"phone": "1 (000) 000-00-01", "user_name": "Edited"})
    assert links.load_links()[token]["max_id"] == "101"
    links.set_user_phone(token, "+10000000002")
    assert links.load_links()[token]["max_id"] is None
    assert links.load_links()[token]["tg_id"] is None
    with pytest.raises(ValueError):
        links.update_user(token, {"branch_id": 2})


def test_same_crm_client_number_in_two_branches_is_not_the_same_record(core):
    links, _, _, _ = core
    first = create(links, id_ycl=99)
    second = create(links, branch=2, id_ycl=99)
    assert links.get_user_by_id_ycl(99, 1)[0] == first
    assert links.get_user_by_id_ycl(99, 2)[0] == second
    create(links, id_ycl=99)
    with pytest.raises(ValueError, match="Duplicate"):
        links.get_user_by_id_ycl(99, 1)


def test_mailing_unsubscribe_never_changes_access(core):
    links, _, _, _ = core
    token = create(links, mailing_enabled=True)
    before = links.load_links()[token]
    links.set_mailing_enabled(token, False)
    after = links.load_links()[token]
    assert access_is_active(after)
    assert {k: v for k, v in before.items() if k != "mailing_enabled"} == {
           k: v for k, v in after.items() if k != "mailing_enabled"}


def test_parallel_web_and_bot_requests_share_persistent_cooldown(core):
    links, branches, service, calls = core
    token = create(links)
    service.bind_token("max", 101, token)
    barrier = threading.Barrier(4)

    def request(index):
        barrier.wait()
        if index % 2:
            return service.open_for_token(token, gate(branches))
        return service.open_for_identity("max", 101, 1, gate(branches))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(request, range(4)))
    assert [r["http_status"] for r in results].count(200) == 1
    assert [r["http_status"] for r in results].count(429) == 3
    assert len(calls) == 1
    restarted = AccessService(links, branches, opener=service.opener, cooldown_seconds=30)
    assert restarted.open_for_token(token, gate(branches))["http_status"] == 429


def test_rejected_queue_releases_claim_for_an_explicit_retry(core):
    links, branches, service, _ = core
    token = create(links)
    attempts = []

    def opener(*args, **kwargs):
        attempts.append(True)
        return {"status": "error", "message": "Queue unavailable"}

    service.opener = opener
    assert service.open_for_token(token, gate(branches))["http_status"] == 503
    assert service.open_for_token(token, gate(branches))["http_status"] == 503
    assert len(attempts) == 2


def test_old_claim_cannot_release_newer_cooldown(tmp_path, monkeypatch):
    state = RuntimeState(tmp_path / "runtime.sqlite3")
    monkeypatch.setattr("runtime_state.time.time", lambda: 100)
    old, _ = state.acquire_cooldown("test", 1)
    monkeypatch.setattr("runtime_state.time.time", lambda: 102)
    new, _ = state.acquire_cooldown("test", 20)
    state.release_cooldown("test", old)
    assert old != new and state.acquire_cooldown("test", 20)[0] is None


def test_json_transaction_rollback_and_corruption_do_not_overwrite_state(tmp_path):
    path = tmp_path / "state.json"
    store = JsonStore(path)
    store.replace({"preserved": 1})
    with pytest.raises(RuntimeError):
        with store.transaction() as data:
            data["preserved"] = 999
            raise RuntimeError("Abort")
    assert store.read() == {"preserved": 1}
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(ValueError):
        with store.transaction() as data:
            data["new"] = 2
    assert path.read_text() == "{broken"


def test_separate_store_instances_do_not_lose_concurrent_updates(tmp_path):
    path = tmp_path / "state.json"
    JsonStore(path).replace({"count": 0})

    def increment(_):
        independent = JsonStore(path)
        for _ in range(10):
            with independent.transaction() as data:
                data["count"] += 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(increment, range(4)))
    assert JsonStore(path).read()["count"] == 40


def test_legacy_naive_expiry_uses_business_timezone(monkeypatch):
    monkeypatch.setenv("GATEBOT_TIMEZONE", "Europe/Moscow")
    record = {"gate_access": True, "access_expires": "2026-11-05T23:59:59"}
    assert access_is_active(record, datetime(2026, 11, 5, 20, 59, tzinfo=ZoneInfo("UTC")))
    assert not access_is_active(record, datetime(2026, 11, 5, 21, 0, tzinfo=ZoneInfo("UTC")))


def test_transition_revision_does_not_suppress_regrant_notification(core, tmp_path):
    links, _, _, _ = core
    state = RuntimeState(tmp_path / "outbox.sqlite3")
    token = create(links, gate_access=False, mailing_enabled=True)
    links.bind_identity("max", 101, token=token)
    events = []
    set_notify_hook(events.append)
    links.set_expiry_at(token, "2099-01-01T23:59:59+03:00")
    links.toggle_user_access(token, False)
    links.set_expiry_at(token, "2099-01-01T23:59:59+03:00")
    assert [e["event"] for e in events] == ["granted", "revoked", "granted"]
    identifiers = [state.enqueue("max", event) for event in events]
    assert len(set(identifiers)) == 3
    assert state.enqueue("max", events[-1]) == identifiers[-1]
    assert state.notification_counts() == {"pending": 3}


def test_interrupted_notifications_remain_unknown_without_auto_resend(tmp_path):
    state = RuntimeState(tmp_path / "runtime.sqlite3")
    state.enqueue("max", {"token": "synthetic", "event": "granted", "max_id": "101"})
    assert state.next_notification("max") is not None
    state.recover_notifications()
    assert state.notification_counts() == {"unknown": 1}
    assert state.next_notification("max") is None


def test_audit_redacts_private_data_and_filters_exact_branch(core):
    logger.event("SYNTHETIC_EVENT", branch_id=1, actor="test", details={
        "token": "synthetic-private-link", "phone": "+10000000001", "email": "private@example.test",
        "nested": {"authorization": "Bearer synthetic-secret", "body": "private-text"}})
    logger.event("OTHER_EVENT", branch_id=11, details={"value": "public"})
    logger.event("SYSTEM_EVENT", details={"value": "system"})
    records = logger.read_events(branch_id=1)
    assert [r["event"] for r in records] == ["SYNTHETIC_EVENT"]
    serialized = json.dumps(records)
    assert all(private not in serialized for private in ("synthetic-private-link", "+10000000001", "private@example.test", "private-text"))
