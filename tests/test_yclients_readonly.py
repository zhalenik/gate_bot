"""The live CRM is never used by these policy and pagination regressions."""
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import requests

from branch_manager import BranchManager
from storage import config
from yclients_api import YClientsClient, YClientsError, YClientsReadOnlyError, eligible_clients, send_message


class Response:
    def __init__(self, payload=None, status=200):
        self.status_code, self.payload = status, payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = deque(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.popleft()
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def crm_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "YC_BASE_URL", "https://api.yclients.ru/api/v1")
    monkeypatch.setattr(config, "YCLIENTS_ENABLED", True)
    monkeypatch.setattr(config, "YC_PARTNER_TOKEN", "synthetic-partner-token")
    monkeypatch.setattr(config, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("YC_USER_TOKEN_1", "synthetic-user-token-1")
    monkeypatch.setenv("YC_USER_TOKEN_2", "synthetic-user-token-2")
    branches = BranchManager(tmp_path / "branches.json")
    branches.save_branches({str(i): {"branch_id": i, "buttons": []} for i in (1, 2)})
    return branches


def client(branches, responses):
    session = Session(responses)
    return YClientsClient(session=session, branches=branches, pace=lambda: None, credential_provider=lambda branch: {"user_token": "synthetic-user-token-" + str(branch)}), session


def page(ids, total=None, company=1):
    return Response({"success": True, "data": [{"id": i, "company_id": company} for i in ids],
                     **({"meta": {"total_count": total}} if total is not None else {})})


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
def test_every_non_get_method_is_blocked_before_transport(crm_config, method):
    api, session = client(crm_config, [])
    with pytest.raises(YClientsReadOnlyError):
        api.request(method, "records/1", 1)
    assert not session.calls


@pytest.mark.parametrize("path", ["records/2", "client/2/7", "staff/2", "records/1/",
                                  "../records/1", "records/1?delete=1", "company/1", "send_sms/1",
                                  "https://example.test/records/1", "records/1%2F.."])
def test_get_path_and_tenant_allowlist_cannot_be_bypassed(crm_config, path):
    api, session = client(crm_config, [])
    with pytest.raises(YClientsReadOnlyError):
        api.request("GET", path, 1)
    assert not session.calls


def test_sms_compatibility_function_cannot_write_to_crm():
    with pytest.raises(YClientsReadOnlyError):
        send_message(1, 1, "synthetic")


@pytest.mark.parametrize("base", ["http://api.yclients.ru/api/v1", "https://example.test/api/v1",
                                  "https://api.yclients.ru.evil.test/api/v1", "https://api.yclients.ru/api/v2",
                                  "https://api.yclients.ru/api/v1?token=x", "https://api.yclients.ru:444/api/v1",
                                  "https://user@api.yclients.ru/api/v1", "https://:synthetic@api.yclients.ru/api/v1"])
def test_credentials_only_go_to_explicit_official_https_api(crm_config, base):
    with pytest.raises(ValueError):
        YClientsClient(session=Session([]), base_url=base, branches=crm_config, pace=lambda: None)


def test_read_uses_headers_timeout_tls_and_no_redirects(crm_config):
    api, session = client(crm_config, [Response({"success": True, "data": []})])
    api.request("GET", "records/1", 1, {"count": 100})
    url, kwargs = session.calls[0]
    assert url == "https://api.yclients.ru/api/v1/records/1"
    assert kwargs["allow_redirects"] is False and kwargs["timeout"] == (5, 20)
    assert kwargs.get("verify", True) is True
    assert kwargs["headers"]["Authorization"] == "Bearer synthetic-partner-token, User synthetic-user-token-1"
    assert "token" not in url and kwargs["params"] == {"count": 100}


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 500])
def test_non_success_status_never_follows_or_reads_sensitive_body(crm_config, status):
    api, session = client(crm_config, [Response(ValueError("PRIVATE BODY"), status)])
    with pytest.raises(YClientsError) as error:
        api.request("GET", "records/1", 1)
    assert str(status) in str(error.value) and "PRIVATE" not in str(error.value)
    assert len(session.calls) == 1


def test_connection_error_does_not_echo_tokens(crm_config):
    api, _ = client(crm_config, [requests.ConnectionError("synthetic-private-auth-header")])
    with pytest.raises(YClientsError) as error:
        api.request("GET", "records/1", 1)
    assert "synthetic-private" not in str(error.value)


def test_legacy_env_user_tokens_are_ignored_even_for_matching_branch(crm_config, monkeypatch):
    monkeypatch.setenv("YC_COMPANY_ID", "1")
    monkeypatch.setenv("YC_USER_TOKEN", "synthetic-fallback")
    monkeypatch.setenv("YC_USER_TOKEN_1", "synthetic-branch-fallback")
    session=Session([])
    api=YClientsClient(session=session,branches=crm_config,pace=lambda:None)
    for branch in (1,2):
        with pytest.raises(YClientsError,match="Missing credentials"):
            api.request("GET","records/"+str(branch),branch)
    assert session.calls==[]


def test_disabled_integration_has_no_transport_call(crm_config, monkeypatch):
    monkeypatch.setattr(config, "YCLIENTS_ENABLED", False)
    api, session = client(crm_config, [])
    with pytest.raises(YClientsError, match="disabled"):
        api.request("GET", "records/1", 1)
    assert not session.calls


def test_all_pages_are_collected_before_snapshot_is_returned(crm_config):
    api, session = client(crm_config, [page([1, 2], 3), page([3], 3)])
    records = api.records_for_day(1, "2026-11-05", page_size=2)
    assert [r["id"] for r in records] == [1, 2, 3]
    assert [kwargs["params"]["page"] for _, kwargs in session.calls] == [1, 2]
    assert all(kwargs["params"]["with_deleted"] == 0 for _, kwargs in session.calls)


def test_missing_totals_require_a_short_page(crm_config):
    api, session = client(crm_config, [page([1, 2]), page([])])
    assert len(api.records_for_day(1, "2026-11-05", page_size=2)) == 2
    assert len(session.calls) == 2


@pytest.mark.parametrize("responses", [
    [page([1, 2], 3), page([], 3)],
    [page([1, 2], 3), page([], 1)],
    [page([1, 2], 3), page([])],
    [page([1, 2]), page([], 2)],
    [page([1, 2], 3), page([2], 3)],
    [page([1, 2], 1)],
    [page([1], 1, company=2)],
])
def test_incomplete_inconsistent_or_cross_branch_snapshot_is_rejected(crm_config, responses):
    api, _ = client(crm_config, responses)
    with pytest.raises(YClientsError):
        api.records_for_day(1, "2026-11-05", page_size=2)


@pytest.mark.parametrize("total", [-1, 1.5, True, "-1", "NaN", "1.5"])
def test_bad_total_count_never_looks_like_complete_snapshot(crm_config, total):
    api, _ = client(crm_config, [page([], total)])
    with pytest.raises(YClientsError):
        api.records_for_day(1, "2026-11-05")


def test_eligible_clients_does_not_fabricate_missing_contact_fields():
    at = datetime(2026, 11, 5, 12, tzinfo=ZoneInfo("Europe/Moscow"))
    records = [{"id": 1, "company_id": 1, "datetime": "2026-11-05T13:00:00+03:00", "client": {"id": 9}}]
    assert eligible_clients(records, 1, at, "Europe/Moscow") == {
        "9": {"id": 9, "_access_expires": "2026-11-05T21:00:00+03:00",
              "_notice_until": at.replace(hour=14).timestamp()}}


@pytest.mark.parametrize("data", [{"id": 8, "company_id": 1}, {"id": 7, "company_id": 2}])
def test_client_response_cannot_substitute_crm_identity_or_branch(crm_config, data):
    api, _ = client(crm_config, [Response({"success": True, "data": data})])
    with pytest.raises(YClientsError):
        api.get_client(1, 7)


def test_staff_directory_is_tenant_scoped_and_excludes_dismissed(crm_config):
    rows=[{'id':12,'company_id':1,'phone':'15550100933','name':'Office','email':'office@example.invalid'},
          {'id':13,'company_id':1,'is_fired':True}, {'id':14,'is_deleted':True}]
    api,session=client(crm_config,[Response({'success':True,'data':rows}),Response({'success':True,'data':[{'id':1,'company_id':2}]})])
    assert api.staff_for_branch(1)==[{'id':'12','phone':'+15550100933','name':'Office','email':'office@example.invalid'}]
    assert session.calls[0][0].endswith('/company/1/staff')
    with pytest.raises(YClientsReadOnlyError):
        api.request('GET','company/2/staff',1)
    with pytest.raises(YClientsError):
        api.staff_for_branch(1)
