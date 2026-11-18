"""Recovery of local admin state must be explicit, backed up and scope preserving."""
import json
from types import SimpleNamespace

from flask import Flask, session
import pytest

import admin
from admin_auth import AdminAuth
from storage.json_store import JsonStore


PASSWORD = 'test-only-strong-password'
NEW_PASSWORD = 'another-test-only-password'


@pytest.fixture
def local_admins(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.config, 'DATA_DIR', str(tmp_path))
    events = []
    monkeypatch.setattr(admin.logger, 'event', lambda *args, **kwargs: events.append((args, kwargs)))
    monkeypatch.setattr(admin, 'BranchManager', lambda: SimpleNamespace(get_branch=lambda branch: branch == 12))
    path = tmp_path / 'admins.json'
    return SimpleNamespace(path=path, auth=AdminAuth(str(path)), events=events)


def backups(state):
    return list(state.path.parent.glob('admins.json.recovery-*.bak'))


def test_login_is_username_not_display_name_and_list_contains_no_hash(local_admins, capsys):
    admin.add_admin('owner', PASSWORD, 'Суперадминистратор', is_super=True)
    assert local_admins.auth.verify_admin('owner', PASSWORD)
    assert not local_admins.auth.verify_admin('Суперадминистратор', PASSWORD)
    admin.main(['--list'])
    output = capsys.readouterr().out
    rows = json.loads(output)
    assert rows == [{'username': 'owner', 'name': 'Суперадминистратор', 'role': 'super',
                     'branch_id': None, 'disabled': False}]
    assert PASSWORD not in output and 'scrypt' not in output and 'password_hash' not in output


def test_existing_account_is_rejected_before_password_prompt(local_admins, monkeypatch):
    admin.add_admin('owner', PASSWORD, 'Owner', is_super=True)
    original = local_admins.path.read_bytes()
    monkeypatch.setattr(admin, 'getpass', lambda *_: pytest.fail('Duplicate account must not ask for a new password'))
    with pytest.raises(SystemExit, match='--reset-password'):
        admin.main(['--username', 'owner', '--name', 'Changed name', '--super'])
    assert local_admins.path.read_bytes() == original


@pytest.mark.parametrize('damaged', [b'', b'  \n', b'{broken', b'[]', b'null', b'\xff\xfe\x00'])
def test_normal_commands_never_replace_unreadable_state(local_admins, damaged):
    local_admins.path.write_bytes(damaged)
    with pytest.raises(admin.AdminStateError, match='--recover'):
        admin.add_admin('owner', PASSWORD, 'Owner', is_super=True)
    with pytest.raises(admin.AdminStateError, match='--recover'):
        admin.reset_password('owner', PASSWORD)
    assert local_admins.path.read_bytes() == damaged
    assert backups(local_admins) == []


def test_utf8_bom_is_not_damaged_and_existing_accounts_survive(local_admins):
    local_admins.path.write_text(json.dumps({'preserved': {'name': 'Другой администратор'}}), encoding='utf-8-sig')
    admin.add_admin('owner', PASSWORD, 'Owner', is_super=True)
    assert local_admins.auth.load_admins()['preserved'] == {'name': 'Другой администратор'}
    assert local_admins.auth.verify_admin('owner', PASSWORD)
    assert backups(local_admins) == []


@pytest.mark.parametrize('damaged', [b'', b'{broken', b'[]', b'\xff\xfe\x00'])
def test_explicit_recovery_backs_up_exact_damaged_bytes(local_admins, damaged):
    local_admins.path.write_bytes(damaged)
    backup = admin.recover_admin('owner', PASSWORD, 'Суперадминистратор', is_super=True)
    assert backup.read_bytes() == damaged
    assert backup.parent == local_admins.path.parent
    assert local_admins.auth.verify_admin('owner', PASSWORD)
    assert list(local_admins.auth.load_admins()) == ['owner']
    assert local_admins.events[-1][1]['details'] == {'damaged_state': True, 'backup_created': True}


def test_recovery_missing_file_creates_only_requested_account(local_admins):
    assert admin.recover_admin('owner', PASSWORD, 'Owner', is_super=True) is None
    assert local_admins.auth.verify_admin('owner', PASSWORD)
    assert backups(local_admins) == []


def test_recovery_preserves_other_readable_accounts_and_backs_up_valid_state(local_admins):
    admin.add_admin('owner', PASSWORD, 'Old owner', is_super=True)
    other = {'password_hash': 'opaque-legacy-hash', 'branch_id': 12, 'is_super': False, 'disabled': True}
    with local_admins.auth.store.transaction() as admins:
        admins['other'] = other
        admins['owner']['disabled'] = True
        admins['owner']['extra_preserved_field'] = 'opaque'
    original = local_admins.path.read_bytes()
    backup = admin.recover_admin('owner', NEW_PASSWORD, 'Recovered owner', is_super=True)
    records = local_admins.auth.load_admins()
    assert backup.read_bytes() == original
    assert records['other'] == other
    assert records['owner']['extra_preserved_field'] == 'opaque'
    assert records['owner']['name'] == 'Recovered owner'
    assert records['owner']['disabled'] is False
    assert records['owner']['session_version'] == 2
    assert local_admins.auth.verify_admin('owner', NEW_PASSWORD)
    assert not local_admins.auth.verify_admin('owner', PASSWORD)


def test_recovery_adds_owner_to_valid_state_without_erasing_malformed_other_entry(local_admins):
    JsonStore(local_admins.path).replace({'legacy-bad-record': 'preserved'})
    admin.recover_admin('owner', PASSWORD, 'Owner', is_super=True)
    assert local_admins.auth.load_admins()['legacy-bad-record'] == 'preserved'
    assert local_admins.auth.verify_admin('owner', PASSWORD)


def test_recovery_never_promotes_existing_branch_admin(local_admins):
    local_admins.auth.save_admins({'branch-admin':admin._admin_record(AdminAuth.hash_password(PASSWORD),'Branch admin',12,False)})
    original = local_admins.path.read_bytes()
    with pytest.raises(ValueError, match='different role or branch'):
        admin.recover_admin('branch-admin', NEW_PASSWORD, 'Owner', is_super=True)
    assert local_admins.path.read_bytes() == original
    assert backups(local_admins) == []


def test_new_branch_admin_uses_normalized_email(local_admins):
    with pytest.raises(ValueError):admin.add_admin('new-branch',PASSWORD,'New',branch_id=12)
    admin.add_admin('New+Admin@Example.Invalid',PASSWORD,'New',branch_id=12)
    assert local_admins.auth.verify_admin('NEW+ADMIN@EXAMPLE.INVALID',PASSWORD)
    assert 'new+admin@example.invalid' in local_admins.auth.load_admins()
    admin.reset_password('NEW+ADMIN@EXAMPLE.INVALID',NEW_PASSWORD)
    assert local_admins.auth.verify_admin('new+admin@example.invalid',NEW_PASSWORD)


def test_weak_recovery_password_keeps_damaged_original(local_admins):
    local_admins.path.write_bytes(b'{broken')
    with pytest.raises(ValueError):
        admin.recover_admin('owner', 'weak', 'Owner', is_super=True)
    assert local_admins.path.read_bytes() == b'{broken'
    assert backups(local_admins) == []


def test_failed_backup_aborts_recovery_before_any_state_change(local_admins, monkeypatch):
    local_admins.path.write_bytes(b'{broken')
    def fail_backup(**kwargs):
        raise OSError('synthetic backup failure')
    monkeypatch.setattr(admin.tempfile, 'mkstemp', fail_backup)
    with pytest.raises(OSError):
        admin.recover_admin('owner', PASSWORD, 'Owner', is_super=True)
    assert local_admins.path.read_bytes() == b'{broken'


def test_failed_replacement_leaves_original_and_completed_backup(local_admins, monkeypatch):
    local_admins.path.write_bytes(b'{broken')
    def fail_save(*args):
        raise OSError('synthetic write failure')
    monkeypatch.setattr(AdminAuth, 'save_admins', fail_save)
    with pytest.raises(OSError):
        admin.recover_admin('owner', PASSWORD, 'Owner', is_super=True)
    assert local_admins.path.read_bytes() == b'{broken'
    assert len(backups(local_admins)) == 1
    assert backups(local_admins)[0].read_bytes() == b'{broken'


def test_password_reset_preserves_role_status_and_name(local_admins):
    local_admins.auth.save_admins({'branch-admin':admin._admin_record(AdminAuth.hash_password(PASSWORD),'Branch admin',12,False)})
    with local_admins.auth.store.transaction() as admins:
        admins['branch-admin']['disabled'] = True
    before = local_admins.auth.load_admins()['branch-admin']
    admin.reset_password('branch-admin', NEW_PASSWORD)
    after = local_admins.auth.load_admins()['branch-admin']
    assert after['branch_id'] == before['branch_id'] == 12
    assert after['is_super'] == before['is_super'] is False
    assert after['disabled'] == before['disabled'] is True
    assert after['name'] == before['name'] == 'Branch admin'
    assert after['session_version'] == before['session_version'] + 1
    assert AdminAuth.verify_password(NEW_PASSWORD, after['password_hash'])
    assert not AdminAuth.verify_password(PASSWORD, after['password_hash'])


def test_password_reset_revokes_existing_web_session(local_admins):
    admin.add_admin('owner', PASSWORD, 'Owner', is_super=True)
    app = Flask(__name__)
    app.secret_key = 'test-only-flask-secret'
    with app.test_request_context():
        assert local_admins.auth.login_admin('owner', PASSWORD)
        assert local_admins.auth.is_logged_in()
        assert session.get('admin_username') == 'owner'
        admin.reset_password('owner', NEW_PASSWORD)
        assert not local_admins.auth.is_logged_in()
        assert 'admin_username' not in session
        assert local_admins.auth.login_admin('owner', NEW_PASSWORD)


def test_recovery_cli_prints_backup_and_correct_login_without_password(local_admins, monkeypatch, capsys):
    local_admins.path.write_bytes(b'')
    monkeypatch.setattr(admin, 'getpass', lambda *_: PASSWORD)
    admin.main(['--recover', '--username', 'owner', '--name', 'Суперадминистратор', '--super'])
    output = capsys.readouterr().out
    assert 'Original admins.json saved to:' in output
    assert 'Login: owner.' in output
    assert PASSWORD not in output
    assert local_admins.auth.verify_admin('owner', PASSWORD)


def test_cli_reports_corruption_before_password_prompt_without_content(local_admins, monkeypatch):
    local_admins.path.write_text('{sensitive-damaged-content', encoding='utf-8')
    monkeypatch.setattr(admin, 'getpass', lambda *_: pytest.fail('Malformed state should be reported before prompting'))
    with pytest.raises(SystemExit) as error:
        admin.main(['--username', 'owner', '--name', 'Owner', '--super'])
    assert '--recover' in str(error.value)
    assert 'sensitive-damaged-content' not in str(error.value)
