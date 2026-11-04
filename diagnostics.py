"""Local diagnostics and a reviewable support bundle without credentials or payloads."""
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path
import json
import platform
import zipfile
from storage import config
from branch_manager import BranchManager
from link_manager import LinkManager
from runtime_state import RuntimeState
import logger


def doctor():
    packages = {}
    for name in ['Flask', 'requests', 'python-telegram-bot', 'pyserial', 'waitress', 'filelock']:
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = 'missing'
    problems = []
    try:
        branches = BranchManager().get_all_branches()
        users = LinkManager().load_links()
    except (ValueError, OSError):
        branches, users = [], {}
        problems.append('invalid_or_unreadable_local_state')
    if config.BOT_ENABLED and not config.BOT_TOKEN:
        problems.append('telegram_token_missing')
    if config.MAX_BOT_ENABLED and not config.MAX_BOT_TOKEN:
        problems.append('max_token_missing')
    if config.SIM800_ENABLED and not config.GATE_DRY_RUN and not config.SIM800_PORT:
        problems.append('serial_port_missing')
    return {
        'version': '0.1.0-alpha.1', 'python': platform.python_version(),
        'os': platform.system(), 'packages': packages,
        'branch_count': len(branches), 'user_count': len(users),
        'mode': {'telegram_enabled': config.BOT_ENABLED, 'max_enabled': config.MAX_BOT_ENABLED, 'yclients_enabled': config.YCLIENTS_ENABLED, 'yclients_read_only': True, 'scheduler_enabled': config.SCHEDULER_ENABLED, 'gate_dry_run': config.GATE_DRY_RUN, 'sim800_enabled': config.SIM800_ENABLED, 'notifications_enabled': config.OUTBOUND_ENABLED},
        'checks': {'network': 'not_run', 'physical_modem': 'not_run', 'local_config': 'error' if problems else 'ok'},
        'problems': problems,
        'notifications': RuntimeState(Path(config.DATA_DIR) / 'runtime.sqlite3').notification_counts(),
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
