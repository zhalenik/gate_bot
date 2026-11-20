"""Local provisioning after verified YCLIENTS Marketplace consent and identity.

The caller must validate the signed profile, the installation and the user's
membership in the selected YCLIENTS company before invoking these helpers.
They neither contact YCLIENTS nor treat a bare company ID as an ownership proof.
"""
import hashlib
import re

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
        raise ValueError('Укажите имя, email и телефон администратора филиала')
    return email, phone, name.strip()[:200]


def _existing_account(identity, db, admins, branch_id, email, phone):
    """Match both contacts without reviving roles or merging another account."""
    matches = []
    for username, admin in admins.items():
        if admin.get('is_super'):
            if AdminAuth.resolve_username(admins, email) == username:
                raise PermissionError('Этот логин уже используется. Укажите другой email.')
            continue
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


def installer_key(signed_profile):
    """Stable identifier from the signed redirect, independent of local form edits."""
    user_id = signed_profile.get('user_id')
    if user_id:
        return 'yc-user:' + user_id
    # Older signed salon profiles have contacts but no YC user ID.
    contacts = str(signed_profile.get('email', '')).lower() + '|' + str(signed_profile.get('phone', ''))
    return 'yc-contacts:' + hashlib.sha256(contacts.encode()).hexdigest()


def reconnect_profile(db, branch_id, signed):
    for username, admin in read_document(db, 'admins.json').items():
        if (str(admin.get('branch_id')) == branch_id and not admin.get('is_super')
                and admin.get('marketplace_installer') == installer_key(signed)):
            person = db.execute('SELECT phone FROM persons WHERE id=?', (admin.get('person_id'),)).fetchone()
            if person:
                return {**signed, 'name': admin.get('name'), 'phone': person['phone'], 'email': username}
    return signed


def provision_marketplace(identity, db, branch_id, profile, company, *, billing=None, password=None, signed_profile=None):
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
            identity.activate_registration_trial_if_unused(db, billing, branch_id, branch)
        return {'username': username, 'person_id': person_id, 'branch_id': branch_id,
                'created': False, 'password_setup_required': bool(admin.get('marketplace_password_setup_required'))}
    AdminAuth.validate_new_password(password)
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
                     'password_hash': AdminAuth.hash_password(password),
                     'branch_id': int(branch_id), 'is_super': False, 'disabled': False,
                     'session_version': 1}
    if signed_profile is not None:
        admins[email]['marketplace_installer'] = installer_key(signed_profile)
    write_document(db, 'branches.json', branches)
    write_document(db, 'admins.json', admins)
    db.execute('INSERT INTO web_accounts(username,person_id) VALUES(?,?)', (email, person_id))
    identity._membership(db, person_id, branch_id)
    if billing is None:
        from billing_service import BillingService
        billing = BillingService(identity.product)
    billing.activate_trial(branch_id, timezone='Europe/Moscow', at=identity.clock(), db=db)
    identity.branch_registered(branch_id, 'marketplace', person_id)
    if identity.welcome_sink:
        identity.welcome_sink(person_id, branch_id, email)
    return {'username': email, 'person_id': person_id, 'branch_id': branch_id,
            'created': True, 'password_setup_required': False}
