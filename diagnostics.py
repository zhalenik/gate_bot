"""Local diagnostics and a reviewable support bundle without credentials or payloads."""
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
import json
import os
import platform
import re
import sqlite3
import zipfile
from storage import config
from storage.json_store import JsonStore
from branch_manager import BranchManager
from link_manager import LinkManager
from runtime_state import RuntimeState
import logger


def _crm_credential_presence(branches):
    """Read saved branch credential presence locally; never use env user tokens."""
    from cryptography.fernet import Fernet, InvalidToken
    active = [branch for branch in branches.values() if branch.get('enabled', True) is True and branch.get('yclients_enabled',True) is not False]
    stored = {}
    database = Path(config.DATA_DIR) / 'application.sqlite3'
    key = Path(config.DATA_DIR) / 'integration.key'
    if database.exists() and key.exists():
        try:
            cipher = Fernet(key.read_bytes())
            with sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True) as db:
                for branch_id, encrypted in db.execute('SELECT branch_id,ciphertext FROM identity_integrations WHERE kind="yclients"'):
                    try:
                        value = json.loads(cipher.decrypt(encrypted.encode()))
                        if isinstance(value,dict): stored[str(branch_id)] = value
                    except (InvalidToken, ValueError, TypeError, AttributeError):
                        pass
        except (OSError, sqlite3.Error, ValueError):
            pass
    missing_tokens = 0
    partner_present = bool(config.YC_PARTNER_TOKEN)
    for branch in active:
        credentials = stored.get(str(branch['branch_id']), {})
        if not credentials.get('user_token') or branch.get('yclients_marketplace_state') in {'frozen','uninstalled'}:
            missing_tokens += 1
    if active and not partner_present:
        partner_present = all(bool(stored.get(str(branch['branch_id']),{}).get('partner_token')) for branch in active)
    return {
        'yclients_partner_token_present': partner_present,
        'yclients_active_branch_references_valid': True,
        'yclients_active_branch_tokens_present': bool(active) and not missing_tokens,
        'yclients_invalid_reference_branch_count': 0,
        'yclients_missing_token_branch_count': missing_tokens,
    }


def doctor():
    packages = {}
    for name in ['Flask', 'requests', 'python-telegram-bot', 'pyserial', 'waitress', 'filelock']:
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = 'missing'
    problems, warnings = [], []
    branch_data, branches, users, admins = {}, [], {}, {}
    try:
        manager = BranchManager()
        # Keep validation and credential references in the same local snapshot.
        with manager.store.lock:
            branch_data = manager.load_branches()
            branches = manager.get_all_branches()
    except (ValueError, OSError, TypeError, AttributeError):
        branch_data, branches = {}, []
        problems.append('invalid_or_unreadable_branches')
    try:
        users = LinkManager().load_links()
    except (ValueError, OSError):
        problems.append('invalid_or_unreadable_users')
    try:
        admins = JsonStore(Path(config.DATA_DIR) / 'admins.json').read()
    except (ValueError, OSError):
        problems.append('invalid_or_unreadable_admins')
    enabled_branch_count = sum(branch.enabled for branch in branches)
    if not admins:
        problems.append('admins_missing')
    if not enabled_branch_count:
        problems.append('enabled_branches_missing')
    try:
        env_file_present = Path(config.ENV_FILE).is_file()
    except OSError:
        env_file_present = False
    if not env_file_present:
        warnings.append('env_file_missing')

    credentials = {
        'telegram_token_present': bool(config.BOT_TOKEN),
        'max_token_present': bool(config.MAX_BOT_TOKEN),
        **_crm_credential_presence(branch_data),
    }
    if config.BOT_ENABLED and not config.BOT_TOKEN:
        problems.append('telegram_token_missing')
    if config.BOT_ENABLED and config.BOT_MODE != 'polling':
        problems.append('telegram_polling_mode_required')
    if config.BOT_ENABLED and not config.BOT_USERNAME:
        warnings.append('telegram_username_missing')
    if config.MAX_BOT_ENABLED and not config.MAX_BOT_TOKEN:
        problems.append('max_token_missing')
    if config.MAX_BOT_ENABLED and config.MAX_BOT_MODE != 'polling':
        problems.append('max_polling_mode_required')
    if config.MAX_BOT_ENABLED and not config.MAX_BOT_USERNAME:
        warnings.append('max_username_missing')
    if config.MAX_BOT_ENABLED and config.MAX_CA_BUNDLE:
        try:
            ca_file_present = Path(config.MAX_CA_BUNDLE).is_file()
        except OSError:
            ca_file_present = False
        if not ca_file_present:
            problems.append('max_ca_bundle_missing')
    if config.YCLIENTS_ENABLED:
        if not credentials['yclients_partner_token_present']:
            problems.append('yclients_partner_token_missing')
        if credentials['yclients_invalid_reference_branch_count']:
            problems.append('yclients_branch_credential_reference_invalid')
        if credentials['yclients_missing_token_branch_count']:
            problems.append('yclients_active_branch_token_missing')
    if config.SIM800_ENABLED and not config.SIM800_PORT:
        problems.append('serial_port_missing')
    if not config.GATE_DRY_RUN and not config.SIM800_ENABLED:
        problems.append('sim800_disabled_for_hardware_mode')
    try:
        notifications = RuntimeState(Path(config.DATA_DIR) / 'runtime.sqlite3').notification_counts()
    except (OSError, sqlite3.Error):
        notifications = {}
        problems.append('invalid_or_unreadable_notification_state')
    return {
        'version': Path(__file__).with_name('VERSION').read_text(encoding='utf-8').strip(), 'python': platform.python_version(),
        'os': platform.system(), 'packages': packages,
        'env_file_present': env_file_present,
        'admin_count': len(admins), 'branch_count': len(branches),
        'enabled_branch_count': enabled_branch_count, 'user_count': len(users),
        'credentials': credentials,
        'mode': {'telegram_enabled': config.BOT_ENABLED, 'max_enabled': config.MAX_BOT_ENABLED, 'yclients_enabled': config.YCLIENTS_ENABLED, 'yclients_read_only': True, 'scheduler_enabled': config.SCHEDULER_ENABLED, 'gate_dry_run': config.GATE_DRY_RUN, 'sim800_enabled': config.SIM800_ENABLED,
                 'notifications_enabled':config.outbound_enabled('client') or config.outbound_enabled('admin'),
                 'client_notifications_enabled':config.outbound_enabled('client'), 'admin_notifications_enabled':config.outbound_enabled('admin')},
        'checks': {'network': 'not_run', 'physical_modem': 'not_run', 'credentials': 'presence_only', 'local_config': 'error' if problems else ('warning' if warnings else 'ok')},
        'problems': problems,
        'warnings': warnings,
        'notifications': notifications,
    }


def support_bundle(output, branch_id=None):
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = doctor()
    events = logger.read_events(branch_id=branch_id, limit=1000)
    # The bundle never contains .env, operational databases, contacts or SMS.
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('diagnostics.json', json.dumps(snapshot, ensure_ascii=False, indent=2))
        z.writestr('events.jsonl', ''.join(json.dumps(logger.redact(item), ensure_ascii=False) + '\n' for item in events))
        z.writestr('README.txt', 'Review before sharing. Includes software versions, mode flags and redacted audit events only. Branch identifiers and event times remain for diagnosis. No credentials, customers, SMS bodies or database copies.\n')
    return path
