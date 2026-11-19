"""Local provisioning after verified YCLIENTS Marketplace consent and identity.

The caller must validate the signed profile, the installation and the user's
membership in the selected YCLIENTS company before invoking these helpers.
They neither contact YCLIENTS nor treat a bare company ID as an ownership proof.
"""
import re
import secrets

from admin_auth import AdminAuth
from identity_service import valid_email, valid_phone
from storage.sql_documents import read_document, write_document


def _profile(profile):
    if not isinstance(profile, dict):
        raise ValueError('YCLIENTS не передал данные пользователя')
    email = valid_email(profile.get('email'))
    phone = valid_phone(profile.get('phone'))
    name = profile.get('name')
    if not email or not isinstance(name, str) or not name.strip():
        raise ValueError('YCLIENTS должен передать имя, email и телефон пользователя')
    return email, phone, name.strip()[:200]


def _existing_account(identity, db, admins, branch_id, email, phone):
    """Match both contacts without reviving roles or merging another account."""
    matches = []
    for username, admin in admins.items():
        account = db.execute('SELECT person_id FROM web_accounts WHERE username=?', (username,)).fetchone()
        person_id = account['person_id'] if account else admin.get('person_id')
        person = db.execute('SELECT email,phone FROM persons WHERE id=?', (person_id,)).fetchone() if person_id else None
        try:
            account_email = valid_email(username if '@' in username else person['email'] if person else '')
        except ValueError:
            account_email = ''
        try:
            account_phone = valid_phone(person['phone'] if person else admin.get('phone'))
        except ValueError:
            account_phone = ''
        if account_email != email and account_phone != phone:
            continue
        if (account_email != email or account_phone != phone or admin.get('is_super')
                or admin.get('disabled') or str(admin.get('branch_id')) != branch_id):
            raise PermissionError('Контакты уже связаны с другим аккаунтом GateBot. Войдите в свой кабинет')
        resolved = identity._admin(db, username)
        if branch_id not in resolved['branch_ids'] or not person_id:
            raise PermissionError('Права администратора филиала отозваны')
        matches.append((username, person_id, admin))
    if len(matches) > 1:
        raise PermissionError('Найдено несколько аккаунтов. Войдите в свой кабинет GateBot')
    return matches[0] if matches else None


def validate_marketplace_target(identity, db, branch_id, profile):
    """Read-only preflight before the installation request; never grants access."""
    if isinstance(branch_id, bool) or not re.fullmatch(r'[1-9][0-9]{0,14}', str(branch_id)):
        raise ValueError('Некорректный идентификатор филиала YCLIENTS')
    branch_id = str(branch_id)
    email, phone, name = _profile(profile)
    branches = read_document(db, 'branches.json')
    admins = read_document(db, 'admins.json')
    branch = branches.get(branch_id)
    existing = _existing_account(identity, db, admins, branch_id, email, phone)
    if branch is not None:
        mode = branch.get('yclients_connection_mode') or branch.get('yclients_connection_source') or 'direct'
        if branch.get('yclients_enabled', True) is False or mode != 'marketplace':
            raise PermissionError('Филиал уже зарегистрирован другим способом. Войдите в его кабинет GateBot')
        if not existing:
            raise PermissionError('Филиал уже зарегистрирован. Запросите приглашение у его администратора')
    elif existing:
        # An orphaned role must not recreate a deleted branch.
        raise PermissionError('Филиал аккаунта больше не существует')
    return {**profile, 'email': email, 'phone': phone, 'name': name}


def provision_marketplace(identity, db, branch_id, profile, company, *, billing=None):
    """Create one branch/admin/client atomically, or reuse its existing owner.

    ``db`` is the caller's active ProductStore transaction. Existing account
    passwords, restrictions, notification preferences and billing are preserved.
    """
    profile = validate_marketplace_target(identity, db, branch_id, profile)
    branch_id = str(branch_id)
    email, phone, name = profile['email'], profile['phone'], profile['name']
    branches = read_document(db, 'branches.json')
    admins = read_document(db, 'admins.json')
    branch = branches.get(branch_id)
    existing = _existing_account(identity, db, admins, branch_id, email, phone)
    if branch is not None:
        username, person_id, admin = existing
        needs_activation = branch.get('onboarding_status') == 'pending_verification'
        if needs_activation:
            branch.update(enabled=True, onboarding_status='active')
            write_document(db, 'branches.json', branches)
            if billing is None:
                from billing_service import BillingService
                billing = BillingService(identity.product)
            # An operator may have granted a tariff before the old review was
            # completed. Activation must preserve that term, including expired
            # or cancelled terms, rather than replacing it with a free trial.
            explicit_term = db.execute('SELECT 1 FROM billing_terms WHERE branch_id=? AND source<>"legacy" LIMIT 1',
                                       (branch_id,)).fetchone()
            if not explicit_term:
                billing.activate_trial(branch_id, timezone=branch.get('timezone', 'Europe/Moscow'),
                                       at=identity.clock(), db=db)
        return {'username': username, 'person_id': person_id, 'branch_id': branch_id,
                'created': False, 'password_setup_required': bool(admin.get('marketplace_password_setup_required'))}
    company = company if isinstance(company, dict) else {}
    branch_name = next((value.strip()[:200] for value in
                        (company.get('title'), company.get('name'), profile.get('salon_name'))
                        if isinstance(value, str) and value.strip()), 'Филиал YCLIENTS ' + branch_id)
    person_id = identity._person(db, name, phone, verified=True)
    db.execute('UPDATE persons SET email=? WHERE id=?', (email, person_id))
    branches[branch_id] = {'branch_id': int(branch_id), 'name': branch_name, 'timezone': 'Europe/Moscow',
                          'enabled': True, 'yclients_enabled': True, 'id_origin': 'yclients',
                          'onboarding_status': 'active', 'hardware_routes_verified': False, 'buttons': [],
                          'yclients_connection_mode': 'marketplace'}
    admins[email] = {'name': name, 'phone': phone, 'person_id': person_id,
                     'password_hash': AdminAuth.hash_password(secrets.token_urlsafe(48)),
                     'branch_id': int(branch_id), 'is_super': False, 'disabled': False,
                     'session_version': 1, 'marketplace_password_setup_required': True}
    write_document(db, 'branches.json', branches)
    write_document(db, 'admins.json', admins)
    db.execute('INSERT INTO web_accounts(username,person_id) VALUES(?,?)', (email, person_id))
    identity._membership(db, person_id, branch_id)
    if billing is None:
        from billing_service import BillingService
        billing = BillingService(identity.product)
    billing.activate_trial(branch_id, timezone='Europe/Moscow', at=identity.clock(), db=db)
    if identity.welcome_sink:
        identity.welcome_sink(person_id, branch_id, email)
    return {'username': email, 'person_id': person_id, 'branch_id': branch_id,
            'created': True, 'password_setup_required': True}


def set_marketplace_password(identity, db, username, password, confirm):
    """Set the first password; caller supplies a browser-bound, CSRF-safe proof."""
    AdminAuth.validate_new_password(password)
    if not isinstance(confirm, str) or password != confirm:
        raise ValueError('Пароли не совпадают')
    identity._admin(db, username)
    admins = read_document(db, 'admins.json')
    admin = admins[username]
    if admin.get('is_super') or admin.get('marketplace_password_setup_required') is not True:
        raise PermissionError('Первичная настройка пароля недоступна')
    admin['password_hash'] = AdminAuth.hash_password(password)
    admin['session_version'] = int(admin.get('session_version', 0)) + 1
    admin.pop('marketplace_password_setup_required', None)
    write_document(db, 'admins.json', admins)
    return {'username': username, 'branch_id': str(admin['branch_id']), 'password_setup_required': False}
