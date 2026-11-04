"""Local administrator provisioning. Passwords are read securely from the terminal."""
import argparse
from getpass import getpass
from pathlib import Path
import re
from admin_auth import AdminAuth
from branch_manager import BranchManager
from storage import config
import logger


def add_admin(username, password, name, branch_id=None, is_super=False):
    if not re.fullmatch(r'[A-Za-z0-9_.@-]{3,80}', username):
        raise ValueError('Username: 3–80 letters, digits, ., _, @ or -')
    if not isinstance(is_super, bool) or (not is_super and not BranchManager().get_branch(branch_id)):
        raise ValueError('A branch administrator needs an existing branch')
    if is_super and branch_id is not None:
        raise ValueError('Super administrator cannot have a branch scope')
    auth = AdminAuth(str(Path(config.DATA_DIR) / 'admins.json'))
    password_hash = auth.hash_password(password)
    with auth.store.transaction() as admins:
        if username in admins:
            raise ValueError('Administrator already exists')
        admins[username] = {'name': str(name), 'password_hash': password_hash, 'is_super': is_super, 'branch_id': int(branch_id) if branch_id is not None else None, 'disabled': False, 'session_version': 1}
    logger.event('ADMIN_PROVISIONED', branch_id=branch_id, actor=username, details={'is_super': is_super})
    return True


def main():
    parser = argparse.ArgumentParser(description='Create an administrator in private local state')
    parser.add_argument('--username', required=True)
    parser.add_argument('--name', required=True)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--super', dest='is_super', action='store_true')
    scope.add_argument('--branch', type=int)
    args = parser.parse_args()
    password = getpass('Password (at least 12 characters): ')
    if password != getpass('Repeat password: '):
        raise SystemExit('Passwords do not match')
    try:
        add_admin(args.username, password, args.name, args.branch, args.is_super)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    print('Administrator created. Open /admin/login on the local application.')


if __name__ == '__main__':
    main()
