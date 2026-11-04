from access_service import AccessService
from branch_manager import BranchManager
from link_manager import LinkManager


def setup_service(tmp_path):
    branches = BranchManager(tmp_path / 'branches.json')
    branches.add_branch({'branch_id': 1, 'name': 'Test', 'buttons': [{'type': 'gate', 'label': 'Gate', 'phone': '+70000000001'}]})
    links = LinkManager(tmp_path / 'links.json')
    token = links.create_link({'branch_id': 1, 'gate_access': True, 'phone': '+70000000002'})
    links.bind_identity('max', '11', token=token)
    calls = []
    svc = AccessService(links, branches, opener=lambda *args, **kw: calls.append(args) or {'status': 'success'})
    return svc, links, token, branches.get_branch(1).buttons[0].id, calls


def test_identity_reset_between_lookup_and_final_authorization(tmp_path, monkeypatch):
    svc, links, token, button, calls = setup_service(tmp_path)
    original = svc._identity_records
    def stale_snapshot(platform, identity):
        rows = original(platform, identity)
        links.update_user(token, {'phone': '+70000000003'})
        return rows
    monkeypatch.setattr(svc, '_identity_records', stale_snapshot)
    result = svc.open_for_identity('max', '11', '1', button)
    assert result['status'] == 'error'
    assert calls == []


def test_shutdown_rejects_new_physical_commands(tmp_path):
    svc, links, token, button, calls = setup_service(tmp_path)
    svc.stop_accepting()
    result = svc.open_for_token(token, button)
    assert result['http_status'] == 503
    assert calls == []
