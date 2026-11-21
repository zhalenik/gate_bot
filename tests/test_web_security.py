"""Offline regression tests: tenant boundaries, authorization, CSRF and truthful UI.

All state is temporary. The modem is injected as an in-memory spy and notification
transports remain disabled; this suite never calls the CRM or a messenger.
"""
import hashlib
import importlib
import os
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
from functools import lru_cache
from types import SimpleNamespace

import pytest

TEST_PASSWORD = "test-only-password-1234"


@lru_cache(maxsize=1)
def _password_hash():
    from admin_auth import AdminAuth
    return AdminAuth.hash_password(TEST_PASSWORD)


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEBOT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GATEBOT_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SECRET_KEY", "test-only-session-key-" * 4)
    monkeypatch.setenv("GATEBOT_ENV", "development")
    monkeypatch.setenv("OUTBOUND_ENABLED", "false")
    from storage import config
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(config, "OUTBOUND_ENABLED", False)
    monkeypatch.setattr(config, "BOT_USERNAME", "example_gate_bot")
    monkeypatch.setattr(config, "MAX_BOT_USERNAME", "example_gate_bot")
    monkeypatch.setattr(config, "BASE_URL", "http://localhost:5000")
    module = importlib.import_module("app")
    from access_service import AccessService
    from admin_auth import AdminAuth
    from branch_manager import BranchManager
    from link_manager import LinkManager
    links = LinkManager(tmp_path / "links.json")
    branches = BranchManager(tmp_path / "branches.json")
    for branch_id in (12, 123):
        branches.add_branch({"branch_id": branch_id, "name": f"Branch {branch_id}",
                             "timezone": "Europe/Moscow", "enabled": True,
                             "buttons": [{"type": "gate", "label": "Въезд", "phone": f"+70000000{branch_id:03}"},
                                         {"type": "admin", "label": "Администратор", "phone": "+70000000999"}]})
    from storage.json_store import JsonStore
    auth = module.admin_auth
    monkeypatch.setattr(auth, "store", JsonStore(tmp_path / "admins.json"))
    monkeypatch.setattr(auth, "admins_file", str(tmp_path / "admins.json"))
    monkeypatch.setattr(auth, "branch_exists", lambda value: branches.get_branch(value) is not None)
    auth.save_admins({
        "tenant-a": {"password_hash": _password_hash(), "branch_id": 12, "is_super": False},
        "tenant-b": {"password_hash": _password_hash(), "branch_id": 123, "is_super": False},
        "super": {"password_hash": _password_hash(), "branch_id": None, "is_super": True},
        "unassigned": {"password_hash": _password_hash(), "branch_id": None, "is_super": False},
    })
    token_a = links.create_link({"branch_id": 12, "user_name": "Client A", "phone": "+70000000101", "mailing_enabled": True})
    token_b = links.create_link({"branch_id": 123, "user_name": "Client B", "phone": "+70000000202", "mailing_enabled": True})
    calls, audit = [], []
    def opener(phone, **kwargs):
        calls.append((phone, kwargs))
        return {"status": "success", "message": "Command accepted", "delivery": "dry_run", "confirmed": False}
    service = AccessService(links, branches, opener=opener, cooldown_seconds=0)
    monkeypatch.setattr(module, "link_manager", links)
    monkeypatch.setattr(module, "branch_manager", branches)
    monkeypatch.setattr(module, "access_service", service)
    monkeypatch.setattr(module.logger, "log_admin_action", lambda *args, **kwargs: audit.append((args, kwargs)))
    monkeypatch.setattr(module.logger, "log_user_action", lambda *args, **kwargs: audit.append((args, kwargs)))
    monkeypatch.setattr(module.logger, "log_error", lambda *args, **kwargs: audit.append((args, kwargs)))
    monkeypatch.setattr(module.logger, "event", lambda *args, **kwargs: audit.append((args, kwargs)))
    module._login_attempts.clear()
    module.app.config.update(TESTING=True, SECRET_KEY="test-only-session-key-" * 4, SESSION_COOKIE_SECURE=False)
    return SimpleNamespace(module=module, app=module.app, auth=auth, links=links, branches=branches,
                           token_a=token_a, token_b=token_b, calls=calls, audit=audit)


def csrf(client, page="/admin/login"):
    response = client.get(page)
    assert response.status_code == 200
    with client.session_transaction() as session:
        return session["csrf_token"]


def login(client, username="tenant-a"):
    token = csrf(client)
    response = client.post("/admin/login", data={"username": username, "password": TEST_PASSWORD, "csrf_token": token}, follow_redirects=True)
    assert response.status_code == 200
    return csrf(client, "/admin/users")


def post(client, csrf_token, endpoint, data):
    return client.post(endpoint, json=data, headers={"X-CSRF-Token": csrf_token})


def test_fresh_import_does_not_start_threads_network_or_serial(web, tmp_path):
    environment = dict(os.environ)
    environment.update(GATEBOT_DATA_DIR=str(tmp_path / "fresh-import"),
                       GATEBOT_LOG_DIR=str(tmp_path / "fresh-import-logs"),
                       SECRET_KEY="test-only-root-session-key-1234567890",
                       GATEBOT_ENV="development", SCHEDULER_ENABLED="false",
                       BOT_ENABLED="false", MAX_BOT_ENABLED="false", SIM800_ENABLED="false")
    code = """
import socket, threading, serial
def forbidden(*args, **kwargs):
    raise AssertionError('Import attempted an external side effect')
socket.socket.connect = forbidden
threading.Thread.start = forbidden
serial.Serial = forbidden
import app
assert app.app.test_client().get('/healthz').json == {'status': 'ok', 'component': 'web'}
"""
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            cwd=Path(web.module.__file__).parent, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_login_requires_csrf_and_api_returns_json_401(web):
    client = web.app.test_client()
    assert client.post("/admin/login", data={"username": "tenant-a", "password": TEST_PASSWORD}).status_code == 403
    response = client.get("/admin/api/users")
    assert response.status_code == 401
    assert response.is_json
    response = client.post("/admin/api/toggle-access", json={"token": web.token_a, "enable": True})
    assert response.status_code == 401


def test_unassigned_admin_cannot_log_in_or_list_all_tenants(web):
    client = web.app.test_client()
    token = csrf(client)
    result = client.post("/admin/login", data={"username": "unassigned", "password": TEST_PASSWORD, "csrf_token": token})
    assert result.status_code == 401
    with client.session_transaction() as session:
        session["admin_logged_in"] = True
        session["admin_username"] = "unassigned"
    assert client.get("/admin/api/users").status_code == 401


def test_read_scope_is_exact_and_super_can_read_all(web):
    client = web.app.test_client()
    login(client)
    assert [row["branch_id"] for row in client.get("/admin/api/users").json["users"]] == [12]
    assert client.get("/admin/api/branches").json == {"branches": [{
        "id": 12, "name": "Branch 12", "yclients_enabled": True, "sms_notifications_enabled": False,
    }]}
    csv = client.get("/admin/api/export-csv").text
    assert "Client A" in csv
    assert "Client B" not in csv
    super_client = web.app.test_client()
    login(super_client, "super")
    assert len(super_client.get("/admin/api/users").json["users"]) == 2


@pytest.mark.parametrize("endpoint,payload", [
    ("toggle-access", {"enable": False}),
    ("set-expires", {"days": 2}),
    ("update-user", {"user_name": "Changed"}),
    ("send-link", {"phone": "+70000000333"}),
    ("set-mailing", {"enabled": False}),
    ("delete-user", {}),
])
def test_foreign_token_mutations_are_rejected_without_changes(web, endpoint, payload):
    client = web.app.test_client()
    csrf_token = login(client)
    before = web.links.load_links()
    response = post(client, csrf_token, "/admin/api/" + endpoint, {"token": web.token_b, **payload})
    assert response.status_code == 404
    assert web.links.load_links() == before
    assert web.calls == []


@pytest.mark.parametrize("endpoint", ["create-link", "create-temp-link"])
def test_cannot_create_foreign_branch_grant(web, endpoint):
    client = web.app.test_client()
    csrf_token = login(client)
    before = web.links.load_links()
    response = post(client, csrf_token, "/admin/api/" + endpoint,
                    {"branch_id": 123, "user_name": "Foreign guest", "send_link": False})
    assert response.status_code == 403
    assert web.links.load_links() == before


def test_mutation_requires_csrf_and_boolean_must_be_boolean(web):
    client = web.app.test_client()
    csrf_token = login(client)
    assert client.post("/admin/api/toggle-access", json={"token": web.token_a, "enable": False}).status_code == 403
    assert post(client, csrf_token, "/admin/api/toggle-access", {"token": web.token_a, "enable": "false"}).status_code == 400
    assert post(client, csrf_token, "/admin/api/set-mailing", {"token": web.token_a, "enabled": "false"}).status_code == 400
    assert web.links.load_links()[web.token_a]["gate_access"] is True


@pytest.mark.parametrize("days", [True, -1, 1.5, "1", 366])
def test_bad_expiry_does_not_grant_access(web, days):
    client = web.app.test_client()
    csrf_token = login(client)
    before = web.links.load_links()
    assert post(client, csrf_token, "/admin/api/set-expires", {"token": web.token_a, "days": days}).status_code == 400
    assert web.links.load_links() == before


def test_old_get_and_arbitrary_phone_cannot_dial(web):
    client = web.app.test_client()
    assert client.get("/open-gate?phone=+70000000001").status_code == 405
    csrf_token = csrf(client, "/opn/" + web.token_a)
    button_id = web.branches.get_branch(12).buttons[0].id
    response = post(client, csrf_token, "/open-gate", {"token": web.token_a, "button_id": button_id, "phone": "+70000000001"})
    assert response.status_code == 400
    assert web.calls == []


def test_gate_authorization_is_rechecked_on_every_post(web):
    client = web.app.test_client()
    csrf_token = csrf(client, "/opn/" + web.token_a)
    own_button = web.branches.get_branch(12).buttons[0].id
    foreign_button = web.branches.get_branch(123).buttons[0].id
    assert post(client, csrf_token, "/open-gate", {"token": web.token_a, "button_id": foreign_button}).status_code == 403
    assert web.calls == []
    response = post(client, csrf_token, "/open-gate", {"token": web.token_a, "button_id": own_button})
    assert response.status_code == 200
    assert response.json["confirmed"] is False
    assert len(web.calls) == 1
    web.links.toggle_user_access(web.token_a, False)
    assert post(client, csrf_token, "/open-gate", {"token": web.token_a, "button_id": own_button}).status_code == 403
    assert len(web.calls) == 1


def test_access_page_hides_gate_phone_escapes_name_and_uses_real_bot_links(web):
    web.links.update_user(web.token_a, {"user_name": '<img src=x onerror="alert(1)">'})
    client = web.app.test_client()
    response = client.get("/opn/" + web.token_a)
    assert response.status_code == 200
    assert '<img src=x onerror=' not in response.text
    assert '&lt;img src=x' in response.text
    assert web.branches.get_branch(12).buttons[0].phone not in response.text
    assert "https://max.ru/example_gate_bot?start=" in response.text
    assert "https://t.me/example_gate_bot?start=" in response.text
    web.branches.delete_branch(12)
    assert client.get("/opn/" + web.token_a).status_code == 403


def test_expired_known_pass_keeps_only_its_branch_admin_contact(web):
    from timeutil import now
    other = branch_settings(web, 123)
    other["buttons"][1]["phone"] = "+70000000888"
    web.branches.update_settings(123, {key: value for key, value in other.items() if key != "branch_id"})
    web.links.set_expiry_at(web.token_a, now() - timedelta(seconds=1))
    response = web.app.test_client().get("/opn/" + web.token_a)
    assert response.status_code == 403
    assert 'href="tel:+70000000999"' in response.text
    assert "+70000000888" not in response.text
    assert "Branch 123" not in response.text
    assert web.branches.get_branch(12).buttons[0].phone not in response.text
    assert web.branches.get_branch(123).buttons[0].phone not in response.text
    assert "data-button-id" not in response.text
    assert web.calls == []


@pytest.mark.parametrize("token", ["not-a-real-token", "invalid.token"])
def test_unknown_pass_has_generic_denial_without_contacts(web, token):
    response = web.app.test_client().get("/opn/" + token)
    assert response.status_code == 403
    assert "tel:" not in response.text
    assert "Branch 12" not in response.text
    assert web.calls == []


def test_expired_pass_never_uses_another_branch_contact_if_own_branch_was_deleted(web):
    from timeutil import now
    web.links.set_expiry_at(web.token_a, now() - timedelta(seconds=1))
    web.branches.delete_branch(12)
    response = web.app.test_client().get("/opn/" + web.token_a)
    assert response.status_code == 403
    assert "tel:" not in response.text
    assert "Branch 123" not in response.text
    assert web.calls == []


def test_temporary_pass_expires_after_branch_duration(web):
    from timeutil import now, parse_datetime
    client = web.app.test_client()
    csrf_token = login(client)
    web.branches.update_settings(12, {'pass_duration_hours': 5})
    earliest = now() + timedelta(hours=5)
    response = post(client, csrf_token, "/admin/api/create-temp-link", {"branch_id": 12, "phone": "+70000000444", "send_link": False})
    assert response.status_code == 200
    user = web.links.load_links()[response.json["token"]]
    expiry = parse_datetime(user["access_expires"])
    assert earliest <= expiry <= now() + timedelta(hours=5)
    assert user["is_temporary"] is True


def test_unsubscribe_is_post_only_mutation_with_csrf(web):
    client = web.app.test_client()
    page = "/unsubscribe?token=" + web.token_a
    csrf_token = csrf(client, page)
    assert web.links.load_links()[web.token_a]["mailing_enabled"] is True
    assert client.post(page).status_code == 403
    assert client.post(page, data={"csrf_token": csrf_token}).status_code == 200
    assert web.links.load_links()[web.token_a]["mailing_enabled"] is False
    assert web.links.load_links()[web.token_b]["mailing_enabled"] is True
    assert web.links.verify_token(web.token_a)


def test_deleted_admin_session_is_revoked(web):
    client = web.app.test_client()
    login(client)
    with web.auth.store.transaction() as admins:
        del admins["tenant-a"]
    assert client.get("/admin/api/users").status_code == 401


def test_password_change_revokes_other_sessions_and_rejects_weak_password(web):
    first, second = web.app.test_client(), web.app.test_client()
    first_csrf = login(first)
    login(second)
    result = post(first, first_csrf, "/admin/api/change-password", {"old_password": TEST_PASSWORD, "new_password": "short"})
    assert result.status_code == 400
    result = post(first, first_csrf, "/admin/api/change-password", {"old_password": TEST_PASSWORD, "new_password": "new-test-password-1234"})
    assert result.status_code == 200
    assert first.get("/admin/api/users").status_code == 200
    assert second.get("/admin/api/users").status_code == 401
    assert web.auth.load_admins()["tenant-a"]["password_hash"].startswith("scrypt:")


def test_legacy_password_is_upgraded_at_login(web):
    with web.auth.store.transaction() as admins:
        admins["tenant-a"]["password_hash"] = hashlib.sha256(TEST_PASSWORD.encode()).hexdigest()
    client = web.app.test_client()
    login(client)
    assert web.auth.load_admins()["tenant-a"]["password_hash"].startswith("scrypt:")


def test_logs_and_archive_are_super_only(web, monkeypatch):
    from timeutil import now
    events = [{"ts": now().isoformat(), "level": "INFO", "event": "OPEN", "branch_id": str(value), "details": {"message": f"branch {value}"}} for value in (12, 123)]
    monkeypatch.setattr(web.module.logger, "read_events", lambda **kwargs: events)
    client = web.app.test_client()
    login(client)
    assert client.get("/admin/logs").status_code == 403
    assert client.get("/admin/api/logs").status_code == 403
    assert client.get("/admin/api/logs-zip").status_code == 403
    login(client, 'super')
    response = client.get("/admin/api/logs")
    assert response.status_code == 200
    assert len(response.json["logs"]) == 2
    assert client.get("/admin/api/logs?from=../../storage").status_code == 400


def test_raw_webhooks_and_registration_are_honestly_disabled(web):
    client = web.app.test_client()
    before = web.links.load_links()
    for path in ("/webhook/yclients", "/webhook/yclients/debug", "/yclients/register"):
        assert client.post(path, json={"client_name": "Untrusted", "branch_id": 12}).status_code == 503
    assert web.links.load_links() == before
    assert web.calls == []


def test_failed_notification_is_not_reported_as_sent(web):
    client = web.app.test_client()
    csrf_token = login(client)
    response = post(client, csrf_token, "/admin/api/send-link", {"token": web.token_a})
    assert response.status_code == 503
    assert response.json["status"] == "error"
    response = post(client, csrf_token, "/admin/api/create-link", {"branch_id": 12, "user_name": "Another", "phone": "+70000000444", "send_link": True})
    assert response.status_code == 200
    assert response.json["status"] == "partial"
    assert response.json["created"] is True
    assert response.json["delivery"]["status"] == "disabled"


def test_spreadsheet_formula_name_is_neutralized(web):
    web.links.update_user(web.token_a, {"user_name": '=HYPERLINK("https://example.invalid")'})
    client = web.app.test_client()
    login(client)
    data = client.get("/admin/api/export-csv").text
    assert "'=HYPERLINK" in data


def test_admin_cannot_assign_messenger_identity(web):
    client = web.app.test_client()
    csrf_token = login(client)
    response = post(client, csrf_token, "/admin/api/update-user", {"token": web.token_a, "tg_id": "42"})
    assert response.status_code == 400
    assert web.links.load_links()[web.token_a]["tg_id"] is None


def branch_settings(web, branch_id=12):
    data = web.module._public_branch_settings(web.branches.get_branch(branch_id))
    for key in ('id_origin', 'hardware_routes_verified'):
        data.pop(key, None)
    # These regressions cover the legacy writable contract. Stable-ID round
    # trips and reordered/deleted QR gates have their own route tests.
    for button in data['buttons']:
        button.pop('stable_id', None)
    return data


def test_settings_are_scoped_and_preserve_private_credentials(web):
    with web.branches.store.transaction() as branches:
        branches["12"]["yclients_user_token_env"] = "TENANT_A_PRIVATE_TOKEN_REF"
    client = web.app.test_client()
    csrf_token = login(client)
    response = client.get("/admin/api/branch-settings")
    assert response.status_code == 200
    assert response.json["settings"]["branch_id"] == 12
    assert "yclients_user_token_env" not in response.text
    assert "TENANT_A_PRIVATE_TOKEN_REF" not in response.text
    assert client.get("/admin/api/branch-settings?branch_id=123").status_code == 403
    data = branch_settings(web)
    data["name"] = "Updated Branch A"
    result = post(client, csrf_token, "/admin/api/branch-settings", data)
    assert result.status_code == 200
    assert web.branches.get_branch(12).name == "Updated Branch A"
    assert web.branches.load_branches()["12"]["yclients_user_token_env"] == "TENANT_A_PRIVATE_TOKEN_REF"
    assert web.branches.get_branch(123).name == "Branch 123"


def test_branch_admin_cannot_change_foreign_settings_or_create_tenant(web):
    client = web.app.test_client()
    csrf_token = login(client)
    before = web.branches.load_branches()
    data = branch_settings(web, 123)
    data["name"] = "Forbidden"
    assert post(client, csrf_token, "/admin/api/branch-settings", data).status_code == 403
    data = branch_settings(web)
    data.update(branch_id=321, create=True)
    assert post(client, csrf_token, "/admin/api/branch-settings", data).status_code == 403
    assert web.branches.load_branches() == before


def test_super_can_create_branch_without_touching_crm(web):
    client = web.app.test_client()
    csrf_token = login(client, "super")
    data = branch_settings(web)
    data.update(branch_id=321, create=True, name="New branch", enabled=False)
    result = post(client, csrf_token, "/admin/api/branch-settings", data)
    assert result.status_code == 200
    assert web.branches.get_branch(321).enabled is False
    assert web.calls == []
    assert post(client, csrf_token, "/admin/api/branch-settings", data).status_code == 409
    subscription = web.module.product_services().billing.subscription_view('321')
    assert subscription['status'] == 'trial' and subscription['unlimited']
    from billing_service import epoch, month_at
    assert epoch(subscription['ends_at']) == month_at(epoch(subscription['period_starts_at']), 2, 'Europe/Moscow')
    data.update(create=False, name='Renamed branch')
    assert post(client, csrf_token, '/admin/api/branch-settings', data).status_code == 200
    assert web.module.product_services().billing.subscription_view('321')['ends_at'] == subscription['ends_at']


def test_sql_branch_creation_rolls_back_if_trial_activation_fails(web, monkeypatch):
    from storage.sql_documents import migrate_legacy
    migrate_legacy(Path(web.links.links_file).parent, activate=True)
    client=web.app.test_client();csrf_token=login(client,'super')
    data=branch_settings(web);data.update(branch_id=321,create=True,name='Atomic branch')
    def reject(*args,**kwargs):raise ValueError('failed activation')
    monkeypatch.setattr(web.module.product_services().billing,'activate_trial',reject)
    assert post(client,csrf_token,'/admin/api/branch-settings',data).status_code==400
    assert web.branches.get_branch(321) is None


def test_sql_new_branch_keeps_global_super_scope_without_copying_client(web):
    from storage.sql_documents import migrate_legacy
    from link_manager import access_is_active
    migrate_legacy(Path(web.links.links_file).parent, activate=True)
    products=web.module.product_services()
    products.identity.attach_console_account('super','+70000000333')
    admin=products.identity.web_admin('super')
    before={token for token,r in web.links.load_links().items() if r.get('person_id')==admin['person_id']}
    client=web.app.test_client();csrf_token=login(client,'super')
    data=branch_settings(web);data.update(branch_id=321,create=True,name='New scoped branch')
    assert post(client,csrf_token,'/admin/api/branch-settings',data).status_code==200
    records=[r for r in web.links.load_links().values() if str(r.get('branch_id'))=='321' and r.get('person_id')==admin['person_id']]
    assert records==[]
    current=web.links.load_links()
    assert {token for token,r in current.items() if r.get('person_id')==admin['person_id']}==before
    assert all(access_is_active(current[token]) for token in before)
    assert client.get('/admin/api/identity/admins?branch_id=321').status_code==200


@pytest.mark.parametrize("changes", [
    {"logo": "javascript:alert(1)"},
    {"logo": "https://user:password@example.invalid/image.png"},
    {"timezone": "../private"},
    {"enabled": "false"},
    {"yclients_user_token_env": "NOT_ALLOWED"},
])
def test_invalid_settings_are_rejected_atomically(web, changes):
    client = web.app.test_client()
    csrf_token = login(client)
    before = web.branches.load_branches()
    assert post(client, csrf_token, "/admin/api/branch-settings", {**branch_settings(web), **changes}).status_code == 400
    assert web.branches.load_branches() == before


def test_old_gate_button_cannot_open_changed_target(web):
    client = web.app.test_client()
    csrf_token = login(client)
    old_id = web.branches.get_branch(12).buttons[0].id
    data = branch_settings(web)
    data["buttons"][0]["phone"] = "+70000000666"
    assert post(client, csrf_token, "/admin/api/branch-settings", data).status_code == 200
    result = post(client, csrf_token, "/open-gate", {"token": web.token_a, "button_id": old_id})
    assert result.status_code == 403
    assert web.calls == []
    data["enabled"] = False
    assert post(client, csrf_token, "/admin/api/branch-settings", data).status_code == 200
    assert client.get("/opn/" + web.token_a).status_code == 403


def test_all_admin_templates_render_with_csrf_and_post_logout(web):
    client = web.app.test_client()
    login(client)
    for path in ("/admin/users", "/admin/add-user", "/admin/temp-link", "/admin/change-password", "/admin/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert 'name="csrf-token"' in response.text
        assert '<a href="/admin/logout"' not in response.text
        assert 'action="/admin/logout"' in response.text
    assert client.get("/admin/logs").status_code == 403
