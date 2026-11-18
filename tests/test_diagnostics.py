"""Doctor uses fictional configuration and temporary state; no real .env is read."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import zipfile

import pytest


def stored_credentials(state, branch='1', credentials=None):
    from cryptography.fernet import Fernet
    key=state/'integration.key'
    if not key.exists():key.write_bytes(Fernet.generate_key())
    encrypted=Fernet(key.read_bytes()).encrypt(json.dumps(credentials or {'user_token':'synthetic-stored-user-secret'}).encode()).decode()
    with sqlite3.connect(state/'application.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS identity_integrations(branch_id TEXT,kind TEXT,ciphertext TEXT,PRIMARY KEY(branch_id,kind))')
        db.execute('INSERT OR REPLACE INTO identity_integrations VALUES(?,"yclients",?)',(branch,encrypted))


@pytest.fixture
def diagnostic(tmp_path, monkeypatch):
    import storage

    state = tmp_path / 'private-state'
    state.mkdir()
    env_file = tmp_path / 'private-cursor.env'
    env_file.write_text('# fictional configuration\n', encoding='utf-8')
    config = SimpleNamespace(
        ENV_FILE=env_file, DATA_DIR=str(state), LOG_DIR=str(tmp_path / 'private-logs'),
        SECRET_KEY='synthetic-diagnostic-session-secret-only',
        BOT_ENABLED=True, BOT_TOKEN='synthetic-telegram-secret', BOT_MODE='polling',
        BOT_USERNAME='synthetic_telegram_name',
        MAX_BOT_ENABLED=True, MAX_BOT_TOKEN='synthetic-max-secret', MAX_BOT_MODE='polling',
        MAX_BOT_USERNAME='synthetic_max_name', MAX_CA_BUNDLE='',
        YCLIENTS_ENABLED=True, YC_PARTNER_TOKEN='synthetic-partner-secret',
        YC_USER_TOKEN='synthetic-fallback-secret', YC_COMPANY_ID='1',
        SCHEDULER_ENABLED=False, OUTBOUND_ENABLED=False,
        SIM800_ENABLED=False, SIM800_PORT='', GATE_DRY_RUN=True,
    )
    # Import only the public diagnostics source with a fake configuration module.
    # This remains isolated even when another test previously imported config.
    monkeypatch.setattr(storage, 'config', config, raising=False)
    monkeypatch.setitem(sys.modules, 'storage.config', config)
    monkeypatch.setitem(sys.modules, 'logger', SimpleNamespace(
        read_events=lambda **kwargs: [], redact=lambda item: item,
    ))
    for name in ('YC_USER_TOKEN_1', 'YC_USER_TOKEN_2', 'SYNTHETIC_BRANCH_CREDENTIAL'):
        monkeypatch.delenv(name, raising=False)
    source = Path(__file__).resolve().parents[1] / 'diagnostics.py'
    spec = importlib.util.spec_from_file_location('isolated_diagnostics', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'version', lambda name: 'test-version')

    branches = {
        '1': {'branch_id': 1, 'name': 'private branch name', 'enabled': True, 'buttons': []},
        '2': {'branch_id': 2, 'name': 'other private branch', 'enabled': False, 'buttons': []},
    }

    def write_state(name, value):
        (state / name).write_text(json.dumps(value), encoding='utf-8')

    write_state('branches.json', branches)
    write_state('admins.json', {'private-admin-name': {'password_hash': 'synthetic-password-hash'}})
    write_state('links.json', {'synthetic-link-secret': {'user_name': 'private client name'}})
    stored_credentials(state)
    return SimpleNamespace(module=module, config=config, state=state, branches=branches,
                           write_state=write_state, tmp_path=tmp_path)


def test_healthy_doctor_reports_counts_and_presence_not_live_validation(diagnostic):
    report = diagnostic.module.doctor()
    assert report['env_file_present'] is True
    assert (report['admin_count'], report['branch_count'], report['enabled_branch_count'],
            report['user_count']) == (1, 2, 1, 1)
    assert report['credentials']['telegram_token_present'] is True
    assert report['credentials']['max_token_present'] is True
    assert report['credentials']['yclients_partner_token_present'] is True
    assert report['credentials']['yclients_active_branch_tokens_present'] is True
    assert report['checks'] == {'network': 'not_run', 'physical_modem': 'not_run',
                                'credentials': 'presence_only', 'local_config': 'ok'}
    assert report['problems'] == report['warnings'] == []


def test_environment_only_configuration_is_a_warning(diagnostic):
    diagnostic.config.ENV_FILE.unlink()
    report = diagnostic.module.doctor()
    assert report['env_file_present'] is False
    assert report['warnings'] == ['env_file_missing']
    assert report['problems'] == []
    assert report['checks']['local_config'] == 'warning'


def test_missing_administrator_is_not_ready(diagnostic):
    (diagnostic.state / 'admins.json').unlink()
    report = diagnostic.module.doctor()
    assert report['admin_count'] == 0
    assert 'admins_missing' in report['problems']
    assert report['checks']['local_config'] == 'error'


@pytest.mark.parametrize('empty', [True, False])
def test_no_active_branch_is_not_ready(diagnostic, empty):
    diagnostic.branches['1']['enabled'] = False
    diagnostic.write_state('branches.json', {} if empty else diagnostic.branches)
    report = diagnostic.module.doctor()
    assert report['branch_count'] == (0 if empty else 2)
    assert report['enabled_branch_count'] == 0
    assert 'enabled_branches_missing' in report['problems']
    assert report['credentials']['yclients_active_branch_tokens_present'] is False
    assert report['checks']['local_config'] == 'error'


def test_global_crm_token_cannot_cover_another_active_branch(diagnostic):
    diagnostic.branches['2']['enabled'] = True
    diagnostic.write_state('branches.json', diagnostic.branches)
    report = diagnostic.module.doctor()
    assert report['credentials']['yclients_missing_token_branch_count'] == 1
    assert report['credentials']['yclients_active_branch_tokens_present'] is False
    assert report['problems'] == ['yclients_active_branch_token_missing']


def test_environment_references_do_not_replace_branch_database_credentials(diagnostic, monkeypatch):
    diagnostic.config.YC_USER_TOKEN = ''
    diagnostic.branches['1']['yclients_user_token_env'] = 'SYNTHETIC_BRANCH_CREDENTIAL'
    diagnostic.branches['2']['enabled'] = True
    diagnostic.write_state('branches.json', diagnostic.branches)
    monkeypatch.setenv('SYNTHETIC_BRANCH_CREDENTIAL', 'synthetic-explicit-secret')
    monkeypatch.setenv('YC_USER_TOKEN_2', 'synthetic-second-secret')
    report = diagnostic.module.doctor()
    assert report['credentials']['yclients_active_branch_tokens_present'] is False
    assert report['problems'] == ['yclients_active_branch_token_missing']
    stored_credentials(diagnostic.state,'2')
    assert diagnostic.module.doctor()['credentials']['yclients_active_branch_tokens_present'] is True


def test_obsolete_reference_is_ignored_when_database_token_exists(diagnostic):
    diagnostic.branches['1']['yclients_user_token_env'] = 'SYNTHETIC_BRANCH_CREDENTIAL'
    diagnostic.write_state('branches.json', diagnostic.branches)
    report = diagnostic.module.doctor()
    assert report['credentials']['yclients_active_branch_tokens_present'] is True
    assert report['problems'] == []


def test_obsolete_invalid_reference_no_longer_controls_connection(diagnostic):
    diagnostic.branches['1']['yclients_user_token_env'] = 'invalid-reference'
    diagnostic.write_state('branches.json', diagnostic.branches)
    report = diagnostic.module.doctor()
    assert report['credentials']['yclients_active_branch_references_valid'] is True
    assert report['credentials']['yclients_invalid_reference_branch_count'] == 0
    assert report['problems'] == []


def test_disabled_branch_does_not_require_a_valid_reference_or_token(diagnostic):
    diagnostic.branches['2']['yclients_user_token_env'] = 'invalid-reference'
    diagnostic.write_state('branches.json', diagnostic.branches)
    report = diagnostic.module.doctor()
    assert report['credentials']['yclients_invalid_reference_branch_count'] == 0
    assert report['credentials']['yclients_missing_token_branch_count'] == 0
    assert report['problems'] == []


def test_crm_requires_partner_token_but_disabled_integration_does_not(diagnostic):
    diagnostic.config.YC_PARTNER_TOKEN = ''
    report = diagnostic.module.doctor()
    assert report['credentials']['yclients_partner_token_present'] is False
    assert report['problems'] == ['yclients_partner_token_missing']
    diagnostic.config.YCLIENTS_ENABLED = False
    diagnostic.config.YC_USER_TOKEN = ''
    with sqlite3.connect(diagnostic.state/'application.sqlite3') as db:db.execute('DELETE FROM identity_integrations')
    report = diagnostic.module.doctor()
    assert report['credentials']['yclients_active_branch_tokens_present'] is False
    assert report['problems'] == []


@pytest.mark.parametrize('prefix,problem_prefix', [('BOT', 'telegram'), ('MAX_BOT', 'max')])
@pytest.mark.parametrize('enabled', [True, False])
def test_enabled_bots_require_token_and_polling(diagnostic, prefix, problem_prefix, enabled):
    setattr(diagnostic.config, prefix + '_ENABLED', enabled)
    setattr(diagnostic.config, prefix + '_TOKEN', '')
    setattr(diagnostic.config, prefix + '_MODE', 'webhook')
    report = diagnostic.module.doctor()
    assert report['credentials'][problem_prefix + '_token_present'] is False
    assert (problem_prefix + '_token_missing' in report['problems']) is enabled
    assert (problem_prefix + '_polling_mode_required' in report['problems']) is enabled


def test_missing_bot_usernames_are_optional_warnings(diagnostic):
    diagnostic.config.BOT_USERNAME = diagnostic.config.MAX_BOT_USERNAME = ''
    report = diagnostic.module.doctor()
    assert report['warnings'] == ['telegram_username_missing', 'max_username_missing']
    assert report['problems'] == []
    assert report['checks']['local_config'] == 'warning'


@pytest.mark.parametrize('dry_run', [True, False])
def test_enabled_modem_needs_port_even_when_gate_calls_are_simulated(diagnostic, dry_run):
    diagnostic.config.SIM800_ENABLED = True
    diagnostic.config.GATE_DRY_RUN = dry_run
    assert 'serial_port_missing' in diagnostic.module.doctor()['problems']
    diagnostic.config.SIM800_PORT = 'COM_TEST_ONLY'
    report = diagnostic.module.doctor()
    assert report['problems'] == []
    assert report['checks']['physical_modem'] == 'not_run'


def test_hardware_mode_cannot_claim_readiness_with_disabled_modem(diagnostic):
    diagnostic.config.GATE_DRY_RUN = False
    report = diagnostic.module.doctor()
    assert report['problems'] == ['sim800_disabled_for_hardware_mode']


@pytest.mark.parametrize('kind', ['missing', 'directory', 'file'])
def test_custom_max_ca_must_be_a_file_when_max_is_enabled(diagnostic, kind):
    path = diagnostic.tmp_path / 'private-ca.pem'
    if kind == 'directory':
        path.mkdir()
    elif kind == 'file':
        path.write_text('synthetic certificate, not used for TLS', encoding='utf-8')
    diagnostic.config.MAX_CA_BUNDLE = str(path)
    report = diagnostic.module.doctor()
    assert ('max_ca_bundle_missing' in report['problems']) is (kind != 'file')
    diagnostic.config.MAX_BOT_ENABLED = False
    assert 'max_ca_bundle_missing' not in diagnostic.module.doctor()['problems']


@pytest.mark.parametrize('name,problem', [
    ('branches.json', 'invalid_or_unreadable_branches'),
    ('links.json', 'invalid_or_unreadable_users'),
    ('admins.json', 'invalid_or_unreadable_admins'),
])
def test_corrupt_local_state_has_a_safe_report(diagnostic, name, problem):
    (diagnostic.state / name).write_text('private-invalid-json', encoding='utf-8')
    report = diagnostic.module.doctor()
    assert problem in report['problems']
    assert report['checks']['local_config'] == 'error'
    assert 'private-invalid-json' not in json.dumps(report)


def test_unreadable_runtime_does_not_leak_exception_paths(diagnostic, monkeypatch):
    def unavailable(*args):
        raise sqlite3.OperationalError('private-exception-path')
    monkeypatch.setattr(diagnostic.module, 'RuntimeState', unavailable)
    report = diagnostic.module.doctor()
    assert 'invalid_or_unreadable_notification_state' in report['problems']
    assert 'private-exception-path' not in json.dumps(report)


def test_support_bundle_contains_no_credentials_references_or_local_paths(diagnostic):
    diagnostic.branches['1']['yclients_user_token_env'] = 'SYNTHETIC_BRANCH_CREDENTIAL'
    diagnostic.write_state('branches.json', diagnostic.branches)
    output = diagnostic.module.support_bundle(diagnostic.tmp_path / 'support.zip')
    with zipfile.ZipFile(output) as bundle:
        assert set(bundle.namelist()) == {'diagnostics.json', 'events.jsonl', 'README.txt'}
        report = json.loads(bundle.read('diagnostics.json'))
    serialized = json.dumps(report)
    for sensitive in (
        str(diagnostic.tmp_path), 'synthetic-telegram-secret', 'synthetic-max-secret',
        'synthetic-partner-secret', 'synthetic-fallback-secret', 'synthetic-link-secret',
        'synthetic-password-hash', 'private-admin-name', 'private branch name',
        'private client name', 'SYNTHETIC_BRANCH_CREDENTIAL',
    ):
        assert sensitive not in serialized
    assert report['checks']['credentials'] == 'presence_only'
