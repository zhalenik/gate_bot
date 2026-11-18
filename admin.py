"""Local administrator provisioning and explicit recovery of private state.

Normal commands never discard a damaged file. Recovery is a separate local
operation and first saves the original bytes next to admins.json.
"""
import argparse
from datetime import datetime, timezone
from getpass import getpass
import json
import os
from pathlib import Path
import re
import tempfile
from admin_auth import AdminAuth
from branch_manager import BranchManager
from storage import config
import logger


class AdminStateError(ValueError):
    """A local state error with instructions, without printing file contents."""


def _auth():
    return AdminAuth(str(Path(config.DATA_DIR) / 'admins.json'))


def _validate_username(username):
    if isinstance(username,str) and '@' in username:
        from identity_service import valid_email
        valid_email(username)
        return
    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{3,80}', username):
        raise ValueError('Username: 3–80 letters, digits, ., _, @ or -')


def _validate_scope(branch_id, is_super):
    if not isinstance(is_super, bool) or (not is_super and not BranchManager().get_branch(branch_id)):
        raise ValueError('A branch administrator needs an existing branch')
    if is_super and branch_id is not None:
        raise ValueError('Super administrator cannot have a branch scope')


def _read_admins(auth):
    try:
        return auth.load_admins()
    except (ValueError, UnicodeError):
        raise AdminStateError(
            'admins.json is empty or malformed. It has not been changed. '
            'Stop GateBot, then use admin.py --recover --username owner '
            '--name "Administrator" --super to save a backup and recover access. '
            'Use your intended login and role if different.'
        ) from None


def _admin_record(password_hash, name, branch_id, is_super):
    return {'name': str(name), 'password_hash': password_hash,
            'is_super': is_super, 'branch_id': int(branch_id) if branch_id is not None else None,
            'disabled': False, 'session_version': 1}


def _replace_password(admin, password_hash):
    admin['password_hash'] = password_hash
    version = admin.get('session_version', 0)
    admin['session_version'] = version + 1 if type(version) is int and version >= 0 else 1


def add_admin(username, password, name, branch_id=None, is_super=False, phone=None):
    if not is_super:
        from identity_service import valid_email
        username=valid_email(username)
        if not username:raise ValueError('A new branch administrator needs an email login')
    _validate_username(username)
    _validate_scope(branch_id, is_super)
    auth = _auth()
    password_hash = auth.hash_password(password)
    with auth.store.lock:
        admins = _read_admins(auth)
        if AdminAuth.resolve_username(admins,username) in admins:
            raise ValueError('Administrator already exists. Use --reset-password --username LOGIN to change its password.')
        admins[username] = _admin_record(password_hash, name, branch_id, is_super)
        if phone:
            from identity_service import valid_phone
            admins[username]['phone'] = valid_phone(phone)
        auth.save_admins(admins)
    if phone:
        link_person(username, phone)
    logger.event('ADMIN_PROVISIONED', branch_id=branch_id, actor=username, details={'is_super': is_super})
    return True


def link_person(username, phone, client_token=None):
    """Explicit console trust; never infer an administrator from a matching name."""
    from identity_service import IdentityService, valid_phone
    from storage.sql_documents import is_sql_active
    phone = valid_phone(phone)
    auth = _auth()
    if is_sql_active(Path(config.DATA_DIR)):
        return IdentityService(auth.store.product()).attach_console_account(username, phone, client_token=client_token)
    with auth.store.transaction() as admins:
        if not isinstance(admins.get(username), dict):
            raise ValueError('Administrator does not exist')
        if client_token:
            raise ValueError('Start GateBot once to migrate state before linking a particular client')
        admins[username]['phone'] = phone
        admins[username]['session_version'] = int(admins[username].get('session_version', 0)) + 1
    return None


def list_admins():
    """Only account labels and scopes; never return password hashes."""
    result = []
    for username, admin in _read_admins(_auth()).items():
        if not isinstance(admin, dict):
            result.append({'username': username, 'role': 'invalid'})
            continue
        result.append({'username': username, 'name': admin.get('name', ''),
                       'role': 'super' if admin.get('is_super') is True else 'branch',
                       'branch_id': admin.get('branch_id'), 'disabled': bool(admin.get('disabled', False))})
    return result


def reset_password(username, password):
    """Local password reset; preserve role, display name and disabled status."""
    _validate_username(username)
    auth = _auth()
    password_hash = auth.hash_password(password)
    with auth.store.lock:
        admins = _read_admins(auth)
        username=AdminAuth.resolve_username(admins,username)
        admin = admins.get(username)
        if not isinstance(admin, dict):
            raise ValueError('Administrator does not exist or its record is invalid. Use --list to check logins.')
        _replace_password(admin, password_hash)
        auth.save_admins(admins)
        branch_id = admin.get('branch_id')
    logger.event('ADMIN_PASSWORD_RESET', branch_id=branch_id, actor=username)


def _backup_state(path, original=None):
    if original is None and not path.exists():
        return None
    original = path.read_bytes() if original is None else original
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    fd, filename = tempfile.mkstemp(prefix=f'{path.name}.recovery-{stamp}-', suffix='.bak', dir=path.parent)
    with os.fdopen(fd, 'wb') as backup:
        backup.write(original)
        backup.flush()
        os.fsync(backup.fileno())
    return Path(filename)


def recover_admin(username, password, name, branch_id=None, is_super=False):
    """Explicit local recovery; back up bytes and retain all readable accounts.

An existing account must already have the requested scope. Recovery cannot turn
a branch administrator into a superadministrator as a side effect of a reset.
"""
    _validate_username(username)
    _validate_scope(branch_id, is_super)
    auth = _auth()
    password_hash = auth.hash_password(password)
    damaged = False
    with auth.store.lock:
        try:
            admins = auth.load_admins()
        except (ValueError, UnicodeError):
            admins = {}
            damaged = True
        username=AdminAuth.resolve_username(admins,username)
        if username in admins:
            existing = admins[username]
            if not isinstance(existing, dict):
                raise ValueError('This account has an invalid record. Choose a new recovery login; other accounts will be preserved.')
            same_scope = (existing.get('is_super') is True) == is_super
            if not is_super:
                same_scope = same_scope and str(existing.get('branch_id')) == str(branch_id)
            if not same_scope:
                raise ValueError('Existing administrator has a different role or branch. Use --reset-password to preserve its scope.')
            recovered = dict(existing)
            recovered.update(name=str(name), disabled=False)
            _replace_password(recovered, password_hash)
        else:
            if not is_super:
                from identity_service import valid_email
                username=valid_email(username)
                if not username:raise ValueError('A new branch administrator needs an email login')
            recovered = _admin_record(password_hash, name, branch_id, is_super)
        from storage.sql_documents import is_sql_active
        current_bytes = json.dumps(admins,ensure_ascii=False,indent=2).encode('utf-8') if is_sql_active(auth.store.path.parent) else None
        backup = _backup_state(auth.store.path,current_bytes)
        admins[username] = recovered
        auth.save_admins(admins)
    logger.event('ADMIN_RECOVERED', branch_id=branch_id, actor=username,
                 details={'damaged_state': damaged, 'backup_created': backup is not None})
    return backup


def _main(argv=None):
    parser = argparse.ArgumentParser(description='Create, list or recover local administrator accounts')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--list', action='store_true', help='List logins, display names and roles without hashes')
    action.add_argument('--reset-password', action='store_true', help='Reset one password; preserve role and disabled status')
    action.add_argument('--recover', action='store_true', help='Back up admins.json; create or re-enable the requested account')
    action.add_argument('--link-person', action='store_true', help='Attach an existing account to its verified phone')
    parser.add_argument('--username', help='Login used on /admin/login; distinct from --name')
    parser.add_argument('--name', help='Display name, not the login; Cyrillic is supported')
    parser.add_argument('--phone', help='Personal telephone number with country code; required for new accounts')
    parser.add_argument('--client-token', help='Explicitly attach to an existing client (local console only)')
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--super', dest='is_super', action='store_true')
    scope.add_argument('--branch', type=int)
    args = parser.parse_args(argv)
    if args.list:
        if args.username or args.name is not None or args.is_super or args.branch is not None or args.phone or args.client_token:
            parser.error('--list cannot be combined with account changes')
    elif not args.username:
        parser.error('--username is required')
    elif args.link_person:
        if not args.phone or args.name is not None or args.is_super or args.branch is not None:
            parser.error('--link-person requires --username and --phone only (optional --client-token)')
    elif args.reset_password:
        if args.name is not None or args.is_super or args.branch is not None:
            parser.error('--reset-password does not change --name, --super or --branch')
    elif args.name is None or (not args.is_super and args.branch is None):
        parser.error('Creation/recovery requires --name and either --super or --branch')
    try:
        if args.list:
            print(json.dumps(list_admins(), ensure_ascii=False, indent=2))
            return
        _validate_username(args.username)
        if args.link_person:
            link_person(args.username, args.phone, args.client_token)
            print('Account linked to its phone; previous sessions invalidated.')
            return
        if not args.reset_password:
            _validate_scope(args.branch, args.is_super)
        if not args.recover:
            admins = _read_admins(_auth())
            if args.reset_password and not isinstance(admins.get(args.username), dict):
                raise ValueError('Administrator does not exist or its record is invalid. Use --list to check logins.')
            if not args.reset_password and args.username in admins:
                raise ValueError('Administrator already exists. Use --reset-password --username LOGIN to change its password.')
        if not args.reset_password and not args.recover and not args.phone:
            raise ValueError('New administrators require --phone with a personal telephone number and country code')
        password = getpass('Password (at least 12 characters): ')
        if password != getpass('Repeat password: '):
            raise ValueError('Passwords do not match')
        if args.reset_password:
            reset_password(args.username, password)
            message = 'Password reset; existing sessions invalidated. Account role/status unchanged.'
        elif args.recover:
            backup = recover_admin(args.username, password, args.name, args.branch, args.is_super)
            if args.phone:
                link_person(args.username, args.phone, args.client_token)
            if backup:
                print(f'Original admins.json saved to: {backup}')
            message = 'Administrator recovered. Other readable accounts were preserved.'
        else:
            add_admin(args.username, password, args.name, args.branch, args.is_super, args.phone)
            message = 'Administrator created.'
    except (ValueError, UnicodeError) as error:
        raise SystemExit(str(error)) from None
    except OSError:
        raise SystemExit('Cannot read or write local administrator state. Check the data directory and file permissions.') from None
    print(message)
    print(f'Login: {args.username}. Open /admin/login; enter this login, not the display name.')


def main(argv=None):
    from contextlib import nullcontext
    from filelock import FileLock, Timeout
    from storage.sql_documents import is_sql_active
    # JSON-era runtime must stop before console writes or the migration boundary.
    # Once SQL is active, ordinary console mutations join normal transactions.
    guard=nullcontext() if is_sql_active(config.DATA_DIR) else FileLock(str(Path(config.DATA_DIR)/'runtime.lock'),timeout=0)
    try:
        with guard:
            return _main(argv)
    except Timeout:
        raise SystemExit('Stop GateBot before changing legacy administrator state, then run this command again.') from None


if __name__ == '__main__':
    main()
