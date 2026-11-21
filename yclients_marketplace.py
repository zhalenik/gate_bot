"""YCLIENTS marketplace consent/installation; separate from the GET-only CRM reader.

No network on import or GET redirects. The browser completes marketplace consent
with a CSRF-protected POST; installation uses the dedicated marketplace API.
"""
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import time

import requests

from storage import config
from storage.sql_documents import read_document, write_document


class MarketplaceError(ValueError):
    pass


def positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r'[1-9][0-9]{0,14}', str(value)):
        raise MarketplaceError('Некорректный идентификатор YCLIENTS')
    return str(value)


def parse_redirect(args, partner_token):
    single = args.getlist('salon_id')
    many = args.getlist('salon_ids[]')
    if single and many or len(single) > 1:
        raise MarketplaceError('Неоднозначный список филиалов')
    ids = single or many
    if not ids or len(ids) > 50:
        raise MarketplaceError('Перейдите в GateBot из карточки приложения YCLIENTS')
    ids = list(dict.fromkeys(positive_id(v) for v in ids))
    profile = {}
    encoded, signature = args.get('user_data'), args.get('user_data_sign')
    if encoded is not None or signature is not None:
        if len(args.getlist('user_data')) != 1 or len(args.getlist('user_data_sign')) != 1:
            raise MarketplaceError('Неоднозначные данные пользователя')
        if not partner_token or not isinstance(encoded, str) or len(encoded) > 12000 or not isinstance(signature, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', signature):
            raise MarketplaceError('Данные пользователя YCLIENTS не подтверждены')
        try:
            decoded = base64.b64decode(encoded, validate=True)
            if not hmac.compare_digest(hmac.new(partner_token.encode(), decoded, hashlib.sha256).hexdigest(), signature.lower()):
                raise MarketplaceError('Подпись YCLIENTS не совпадает')
            data = json.loads(decoded.decode('utf-8'))
            if not isinstance(data, dict):
                raise MarketplaceError('Некорректные данные пользователя')
        except (UnicodeError, binascii.Error, json.JSONDecodeError):
            raise MarketplaceError('Некорректные данные пользователя') from None
        for key in ('name', 'email', 'phone', 'salon_name'):
            if isinstance(data.get(key), str):
                profile[key] = data[key][:254]
        identifiers = {positive_id(data[key]) for key in ('user_id', 'id') if key in data}
        if len(identifiers) > 1:
            raise MarketplaceError('Неоднозначный идентификатор пользователя YCLIENTS')
        if identifiers:
            profile['user_id'] = identifiers.pop()
        if 'salon_id' in data:
            profile['salon_id'] = positive_id(data['salon_id'])
    return ids, profile


class MarketplaceAPI:
    ROOT = 'https://api.yclients.ru'

    def __init__(self, partner_token, application_id, *, session=None, installation_enabled=False):
        self.partner_token = partner_token
        self.application_id = positive_id(application_id)
        self.session = session or requests.Session()
        self.installation_enabled = installation_enabled

    def _request(self, method, path, payload=None, *, consent=None):
        if not self.partner_token:
            raise MarketplaceError('Не настроен партнёрский ключ YCLIENTS')
        try:
            response = self.session.request(method, self.ROOT + path,
                headers={'Authorization': 'Bearer ' + self.partner_token,
                         'Accept': 'application/vnd.yclients.v2+json', 'Content-Type': 'application/json'},
                json=payload, timeout=(5, 20), allow_redirects=False, verify=True)
        except requests.RequestException:
            return {'state': 'unknown', 'code': 'network_result_unknown'}
        if method == 'POST' and response.status_code == 201:
            return {'state': 'active'}
        if method == 'GET' and response.status_code == 404:
            return {'state': 'uninstalled'}
        if method == 'GET' and response.status_code == 200:
            try:
                data = response.json()
                status = data['data']['connection_status']['status'] if data.get('success') is True else None
                if status in {'active', 'pending', 'uninstalled', 'frozen'}:
                    if consent and status == 'active':
                        from marketplace_consent import current_installer
                        branch_id, user_id = consent
                        installer = current_installer(data['data'], branch_id, self.application_id)
                        return {'state': status, 'installer_matches': installer is not None and installer == user_id}
                    return {'state': status}
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
            return {'state': 'unknown', 'code': 'invalid_provider_response'}
        # Do not echo provider errors or request URLs containing personal data.
        return {'state': 'rejected' if response.status_code in {400,401,403,404,422} else 'unknown',
                'code': 'provider_http_' + str(response.status_code)}

    def install(self, branch_id):
        if not self.installation_enabled:
            raise MarketplaceError('Активация в YCLIENTS пока выключена владельцем GateBot')
        return self._request('POST', '/marketplace/partner/callback',
                             {'salon_id': int(positive_id(branch_id)), 'application_id': int(self.application_id)})

    def status(self, branch_id, *, user_id=None):
        branch_id = positive_id(branch_id)
        return self._request('GET', '/marketplace/salon/' + branch_id + '/application/' + self.application_id,
                             consent=(branch_id, positive_id(user_id)) if user_id is not None else None)


class MarketplaceService:
    def __init__(self, identity, *, api=None, clock=None, application_id=None,
                 partner_token=None, system_user_token=None, enabled=None):
        self.identity, self.store = identity, identity.product
        self.clock = clock or time.time
        self.application_id = str(application_id if application_id is not None else getattr(config,'YC_MARKETPLACE_APPLICATION_ID','')).strip()
        self.partner_token = config.YC_PARTNER_TOKEN if partner_token is None else partner_token
        self.system_user_token = getattr(config,'YC_MARKETPLACE_SYSTEM_USER_TOKEN','') if system_user_token is None else system_user_token
        self.enabled = getattr(config,'YC_MARKETPLACE_CONNECT_ENABLED',False) if enabled is None else enabled
        self.api = api
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS marketplace_intents(id_hash TEXT PRIMARY KEY,browser_hash TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS marketplace_attempts(intent_hash TEXT NOT NULL,branch_id TEXT NOT NULL,state TEXT NOT NULL,updated REAL NOT NULL,code TEXT,PRIMARY KEY(intent_hash,branch_id))')
            db.execute('CREATE TABLE IF NOT EXISTS marketplace_branches(branch_id TEXT PRIMARY KEY,state TEXT NOT NULL,generation INTEGER NOT NULL DEFAULT 0,updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS marketplace_registrations(intent_hash TEXT NOT NULL,branch_id TEXT NOT NULL,username TEXT NOT NULL,created_account INTEGER NOT NULL,PRIMARY KEY(intent_hash,branch_id))')
            if 'auth_proof' not in {row['name'] for row in db.execute('PRAGMA table_info(marketplace_registrations)')}:
                db.execute('ALTER TABLE marketplace_registrations ADD COLUMN auth_proof TEXT NOT NULL DEFAULT ""')

    @staticmethod
    def _hash(value):
        return hashlib.sha256(str(value).encode()).hexdigest()

    def configured(self):
        return bool(re.fullmatch(r'[1-9][0-9]{0,14}', self.application_id) and self.partner_token and self.system_user_token)

    def _api(self):
        if not self.configured():
            raise MarketplaceError('Владелец GateBot ещё не настроил приложение YCLIENTS')
        return self.api or MarketplaceAPI(self.partner_token, self.application_id, installation_enabled=self.enabled)

    def begin(self, args, browser):
        branches, profile = parse_redirect(args, self.partner_token)
        raw = secrets.token_urlsafe(32)
        stamp = self.clock()
        payload = self.identity._fernet().encrypt(json.dumps({'branches': branches, 'profile': profile}, ensure_ascii=False).encode()).decode()
        with self.store.connect() as db:
            # Bound retention; expired intent bodies contain only encrypted hints.
            old = stamp - 86400
            db.execute('DELETE FROM marketplace_attempts WHERE intent_hash IN (SELECT id_hash FROM marketplace_intents WHERE expires<?)', (old,))
            db.execute('DELETE FROM marketplace_registrations WHERE intent_hash IN (SELECT id_hash FROM marketplace_intents WHERE expires<?)', (old,))
            db.execute('DELETE FROM marketplace_intents WHERE expires<?', (old,))
            db.execute('INSERT INTO marketplace_intents VALUES(?,?,?,?,?)',
                       (self._hash(raw), self._hash(browser), stamp, stamp + 3600, payload))
        return raw

    def _intent(self, db, raw, browser, *, require_fresh=True):
        row = db.execute('SELECT * FROM marketplace_intents WHERE id_hash=?', (self._hash(raw),)).fetchone()
        if not row or not hmac.compare_digest(row['browser_hash'], self._hash(browser)):
            raise MarketplaceError('Начните подключение из маркетплейса YCLIENTS в этом браузере')
        if require_fresh and row['expires'] <= self.clock():
            raise MarketplaceError('Час на подключение истёк. Снова нажмите «Подключить» в YCLIENTS')
        value = json.loads(self.identity._fernet().decrypt(row['payload'].encode()))
        return {**value, 'id_hash': row['id_hash'], 'expires': row['expires']}

    def view(self, raw, browser, username=None):
        with self.store.connect() as db:
            intent = self._intent(db, raw, browser, require_fresh=False)
            admin = self.identity._admin(db, username) if username else None
            branches = read_document(db, 'branches.json')
            result = []
            account = self._onboarding_account(db, intent)
            complete = self._automatic_profile(intent)
            missing_id = bool(complete and not account and not self._identifies_installer(intent['profile']))
            auto = complete and not missing_id
            for branch_id in intent['branches']:
                accessible = bool(admin and (admin.get('is_super') or branch_id in admin['branch_ids']))
                branch = branches.get(branch_id) if accessible else None
                attempt = db.execute('SELECT state,code FROM marketplace_attempts WHERE intent_hash=? AND branch_id=?', (intent['id_hash'], branch_id)).fetchone() if accessible or auto else None
                current = db.execute('SELECT state,generation FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone() if accessible or attempt else None
                state = current['state'] if current else attempt['state'] if attempt else 'pending'
                confirmed = self._confirmed_generation(attempt, current)
                if state == 'active' and not accessible and not confirmed and not (account and account['branch_id'] == branch_id):
                    state = 'unknown'
                result.append({'id': branch_id, 'name': branch.get('name') if branch else None,
                    'accessible': accessible, 'pending_verification': bool(branch and branch.get('onboarding_status') == 'pending_verification'),
                    'registration_required': bool(branch_id not in branches and confirmed),
                    'state': state})
                if result[-1]['registration_required'] and intent['expires'] > self.clock():
                    subject = self._registration_subject(intent, branch_id, current['generation'])
                    latest = db.execute('SELECT subject,phone,state FROM identity_otp WHERE subject=? OR subject LIKE ? '
                                        'ORDER BY created DESC,rowid DESC LIMIT 1', (subject, subject + ':original:%')).fetchone()
                    if latest:
                        original = latest['subject'] != subject
                        target = latest['subject'].rsplit(':', 1)[1] if original else latest['phone']
                        otp = self.identity.otp_subject_status(db, latest['subject'])
                        result[-1]['otp'] = {'phone':target, 'step':'original' if original else 'registration',
                                              **{k:otp[k] for k in ('ready','phone_hint','challenge_id','retry_after')}}
                        if latest['state'] == 'marketplace_verified':
                            # Original proof is complete. The next send goes to the new number.
                            result[-1]['otp'].update(step='registration', ready=False, retry_after=0)
            return {'branches': result, 'profile': intent['profile'], 'expires_at': intent['expires'],
                    'expired': intent['expires'] <= self.clock(), 'configured': self.configured(), 'enabled': self.enabled,
                    'authenticated': bool(admin), 'auto_onboarding': auto,
                    'needs_fresh_redirect': missing_id, 'account': account}

    @staticmethod
    def _automatic_profile(intent):
        return bool(intent['profile'])

    @staticmethod
    def _signed_phone(profile):
        from identity_service import valid_phone
        try:
            return valid_phone(profile.get('phone'))
        except ValueError:
            return None

    def _identifies_installer(self, profile):
        return bool(profile.get('user_id') or (profile.get('salon_id') and self._signed_phone(profile)))

    def _onboarding_account(self, db, intent, branch_id=None):
        query = 'SELECT * FROM marketplace_registrations WHERE intent_hash=?'
        params = [intent['id_hash']]
        if branch_id is not None:
            query += ' AND branch_id=?'
            params.append(branch_id)
        row = db.execute(query, params).fetchone()
        if not row:
            return None
        try:
            admin = self.identity._admin(db, row['username'])
        except PermissionError:
            return None
        if admin.get('is_super') or str(admin.get('branch_id')) != row['branch_id']:
            return None
        return {'username': row['username'], 'branch_id': row['branch_id'], 'can_set_password': False}

    @staticmethod
    def _confirmed_generation(attempt, current):
        return bool(current and current['state'] == 'active' and attempt and attempt['state'] == 'active'
                    and attempt['code'] == 'consent:' + str(current['generation']))

    @staticmethod
    def _registration_subject(intent, branch_id, generation):
        return ':'.join(('marketplace', intent['id_hash'], branch_id, str(generation)))

    def _deactivate_local(self, db, branch_id, state):
        if state not in {'uninstalled', 'frozen'}:
            return
        branches = read_document(db, 'branches.json')
        branch = branches.get(branch_id)
        if (branch and branch.get('yclients_enabled', True)
                and branch.get('yclients_connection_source') != 'direct'
                and branch.get('yclients_connection_mode') != 'direct'):
            previous = branch.get('yclients_marketplace_state')
            branch['yclients_marketplace_state'] = state
            if previous not in {'uninstalled', 'frozen'}:
                # Invalidate polls already in flight, even if the branch reconnects before they finish.
                branch['yclients_sync_revision'] = branch.get('yclients_sync_revision', 0) + 1
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='appointment_schedule'").fetchone():
                    db.execute('UPDATE appointment_schedule SET active=0 WHERE branch_id=?', (branch_id,))
            write_document(db, 'branches.json', branches)
            if state == 'uninstalled':
                db.execute('DELETE FROM identity_integrations WHERE branch_id=? AND kind="yclients"', (branch_id,))
            if previous == 'active' and self.identity.event_sink:
                self.identity.event_sink('marketplace_disconnected', branch_id, {
                    'event_id': secrets.token_hex(16),
                    'message': 'Автоматическая выдача пропусков по записям YCLIENTS остановлена. '
                               'Подписка GateBot продолжает действовать на прежних условиях. Профиль филиала сохранён. '
                               'Для удаления аккаунта обратитесь в поддержку GateBot.',
                })

    def onboard(self, raw, browser, branch_id, *, reconcile=False):
        """Accept YC consent automatically without a GateBot superadmin review.

        Durable claims prevent duplicate installations. An uncertain POST is
        reconciled with GET, never blindly retried. No DB lock spans the network.
        """
        from marketplace_onboarding import provision_marketplace, reconnect_profile, validate_marketplace_target
        branch_id = positive_id(branch_id)
        api = self._api()
        if not self.enabled:
            raise MarketplaceError('Активация в YCLIENTS пока выключена владельцем GateBot')
        with self.store.connect() as db:
            intent = self._intent(db, raw, browser)
            if branch_id not in intent['branches']:
                raise PermissionError('Филиал отсутствует в текущем подключении')
            profile = intent['profile']
            signed_user = profile.get('user_id')
            signed_branch = profile.get('salon_id')
            if signed_branch and signed_branch != branch_id:
                raise MarketplaceError('Выбранный филиал не совпадает с филиалом в данных YCLIENTS.')
            if not self._automatic_profile(intent):
                raise MarketplaceError('Для автоматического подключения включите передачу данных пользователя '
                                       'в карточке приложения YCLIENTS и укажите телефон и email пользователя.')
            if branch_id in read_document(db, 'branches.json'):
                validate_marketplace_target(self.identity, db, branch_id, reconnect_profile(db, branch_id, profile))
            attempt = db.execute('SELECT state,code FROM marketplace_attempts WHERE intent_hash=? AND branch_id=?',
                                 (intent['id_hash'], branch_id)).fetchone()
            current = db.execute('SELECT * FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone()
            if reconcile and not current and not attempt:
                raise MarketplaceError('Сначала начните подключение из карточки GateBot в YCLIENTS')
            account = self._onboarding_account(db, intent, branch_id)
            if attempt and account and not reconcile:
                return {'state': current['state'] if current else attempt['state'], 'account': account, 'replayed': True}
            if not reconcile and self._confirmed_generation(attempt, current) and branch_id not in read_document(db, 'branches.json'):
                return {'state': 'active', 'registration_required': True}
            if attempt and current and current['state'] in {'uninstalled','frozen'}:
                reconcile = True
            if not self._identifies_installer(profile):
                raise MarketplaceError('Данных для регистрации недостаточно. Снова откройте GateBot из карточки YCLIENTS с передачей данных пользователя.')
            if current and current['state'] == 'installing':
                # A concurrent request must not verify/provision before the first
                # POST has returned. A stale claim can be checked after its timeout.
                if self.clock() - current['updated'] < 60:
                    return {'state': 'installing', 'code': 'installation_in_progress'}
                reconcile = True
            if current and current['state'] in {'unknown', 'active'}:
                reconcile = True
            if attempt and attempt['state'] in {'unknown', 'rejected'}:
                reconcile = True
            generation = current['generation'] if current else 0
            if not reconcile:
                db.execute('INSERT INTO marketplace_branches VALUES(?,"installing",?,?) '
                           'ON CONFLICT(branch_id) DO UPDATE SET state="installing",updated=excluded.updated',
                           (branch_id, generation, self.clock()))
            db.execute('INSERT INTO marketplace_attempts VALUES(?,?,"installing",?,NULL) '
                       'ON CONFLICT(intent_hash,branch_id) DO UPDATE SET state="installing",updated=excluded.updated',
                       (intent['id_hash'], branch_id, self.clock()))
        lookup = {'user_id': signed_user} if signed_user else {}
        try:
            result = api.status(branch_id, **lookup)
            if not reconcile and result['state'] in {'pending', 'uninstalled'}:
                with self.store.connect() as db:
                    current = db.execute('SELECT generation,state FROM marketplace_branches WHERE branch_id=?',
                                         (branch_id,)).fetchone()
                    if not current or current['generation'] != generation or current['state'] != 'installing':
                        return {'state': 'unknown', 'code': 'connection_changed'}
                result = api.install(branch_id)
        except Exception:
            result = {'state': 'unknown', 'code': 'provider_result_unknown'}
        with self.store.connect() as db:
            current = db.execute('SELECT generation FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone()
            if current and current['generation'] != generation:
                return {'state': 'unknown', 'code': 'connection_changed'}
            db.execute('INSERT INTO marketplace_branches VALUES(?,?,?,?) '
                       'ON CONFLICT(branch_id) DO UPDATE SET state=excluded.state,updated=excluded.updated',
                       (branch_id, result['state'], generation, self.clock()))
            db.execute('UPDATE marketplace_attempts SET state=?,code=?,updated=? WHERE intent_hash=? AND branch_id=?',
                       (result['state'], result.get('code'), self.clock(), intent['id_hash'], branch_id))
            self._deactivate_local(db, branch_id, result['state'])
        if result['state'] != 'active':
            return result
        # The installation ties a signed user ID to the separately supplied salon.
        if signed_user:
            consent = result if 'installer_matches' in result else api.status(branch_id, **lookup)
            if consent.get('state') != 'active':
                raise MarketplaceError('YCLIENTS пока не подтвердил активное подключение. Нажмите «Проверить состояние».')
            if consent.get('installer_matches') is not True:
                raise MarketplaceError('YCLIENTS не подтвердил, что этот пользователь подключил приложение к выбранному филиалу.')
        with self.store.connect() as db:
            intent = self._intent(db, raw, browser)
            current = db.execute('SELECT generation,state FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone()
            if not current or current['generation'] != generation or current['state'] != 'active':
                return {'state': 'unknown', 'code': 'connection_changed'}
            db.execute('UPDATE marketplace_attempts SET code=? WHERE intent_hash=? AND branch_id=?',
                       ('consent:' + str(generation), intent['id_hash'], branch_id))
            if branch_id not in read_document(db, 'branches.json'):
                return {'state': 'active', 'registration_required': True}
            result = provision_marketplace(self.identity, db, branch_id, reconnect_profile(db, branch_id, intent['profile']), {})
            self._activate_local(db, result['username'], branch_id, generation)
            self._record_account(db, intent, branch_id, result['username'])
            return {'state': 'active', 'account': self._onboarding_account(db, intent, branch_id)}

    @staticmethod
    def _record_account(db, intent, branch_id, username):
        db.execute('INSERT INTO marketplace_registrations(intent_hash,branch_id,username,created_account) VALUES(?,?,?,0) '
                   'ON CONFLICT(intent_hash,branch_id) DO NOTHING', (intent['id_hash'], branch_id, username))

    def _registration_context(self, db, raw, browser, branch_id):
        intent = self._intent(db, raw, browser)
        if not self.enabled or not self.configured() or branch_id not in intent['branches']:
            raise PermissionError('Регистрация недоступна для этого подключения')
        if not self._identifies_installer(intent['profile']):
            raise MarketplaceError('Для регистрации откройте GateBot из карточки YCLIENTS с передачей данных пользователя.')
        attempt = db.execute('SELECT state,code FROM marketplace_attempts WHERE intent_hash=? AND branch_id=?',
                             (intent['id_hash'], branch_id)).fetchone()
        current = db.execute('SELECT state,generation FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone()
        if not self._confirmed_generation(attempt, current):
            raise MarketplaceError('Сначала подтвердите подключение филиала в YCLIENTS')
        if branch_id in read_document(db, 'branches.json'):
            raise PermissionError('Филиал уже зарегистрирован. Войдите в его кабинет GateBot')
        subject = self._registration_subject(intent, branch_id, current['generation'])
        return intent, subject, current['generation']

    def _original_phone_challenge(self, db, intent, subject, phone):
        """Return the additional proof required to replace a signed, usable YC phone."""
        original = self._signed_phone(intent['profile'])
        if original is None or original == phone:
            return None
        original_subject = subject + ':original:' + phone
        verified = db.execute('SELECT * FROM identity_otp WHERE subject=? AND phone=? AND state="marketplace_verified" '
                              'ORDER BY created DESC LIMIT 1', (original_subject, original)).fetchone()
        if verified and self.identity._otp_delivery_allowed(verified):
            return None
        return original_subject, original

    def verify_original_phone(self, raw, browser, branch_id, phone, challenge, code):
        from identity_service import valid_phone
        branch_id, phone = positive_id(branch_id), valid_phone(phone)
        if not isinstance(challenge, str):
            raise ValueError('Запросите код на номер из YCLIENTS')
        with self.store.connect() as db:
            intent, subject, _ = self._registration_context(db, raw, browser, branch_id)
            original = self._original_phone_challenge(db, intent, subject, phone)
            if original:
                otp = self.identity._check_otp(db, original[0], challenge, code)
                if otp and otp['phone'] == original[1]:
                    db.execute('UPDATE identity_otp SET state="marketplace_verified" WHERE id=?', (challenge,))
                    return
            else:
                # A retry after a lost response can resume the next send using this same verified challenge.
                prior = db.execute('SELECT * FROM identity_otp WHERE id=? AND subject=? AND state="marketplace_verified"',
                                   (challenge, subject + ':original:' + phone)).fetchone()
                if prior and self.identity._otp_delivery_allowed(prior):
                    return
        raise ValueError('Проверьте код для номера из YCLIENTS или запросите новый')

    def request_registration_code(self, raw, browser, branch_id, phone, ip):
        from identity_service import valid_phone
        branch_id = positive_id(branch_id)
        phone = valid_phone(phone)
        step = 'registration'
        def resolve(db):
            nonlocal step
            intent, subject, _ = self._registration_context(db, raw, browser, branch_id)
            original = self._original_phone_challenge(db, intent, subject, phone)
            if original:
                step = 'original'
                return original[0], original[1], branch_id
            return subject, phone, branch_id
        result = self.identity.request_phone_otp(resolve, ip, flow='branch_registration')
        return {**result, 'step':step}

    def complete_registration(self, raw, browser, branch_id, profile, challenge, code, password, confirm):
        from admin_auth import AdminAuth
        from marketplace_onboarding import provision_marketplace, validate_marketplace_target
        branch_id = positive_id(branch_id)
        AdminAuth.validate_new_password(password)
        if password != confirm:
            raise ValueError('Пароли не совпадают')
        if not isinstance(profile, dict) or set(profile) != {'name', 'salon_name', 'phone', 'email'}:
            raise ValueError('Заполните данные филиала и администратора')
        if not isinstance(profile['salon_name'], str) or not 1 <= len(profile['salon_name'].strip()) <= 200:
            raise ValueError('Укажите название филиала (до 200 символов)')
        if not isinstance(challenge, str):
            raise ValueError('Запросите код для подтверждения телефона')
        with self.store.connect() as db:
            intent, subject, generation = self._registration_context(db, raw, browser, branch_id)
            profile = validate_marketplace_target(self.identity, db, branch_id, profile)
            if self._original_phone_challenge(db, intent, subject, profile['phone']):
                raise ValueError('Сначала подтвердите номер телефона из YCLIENTS')
            otp = self.identity._check_otp(db, subject, challenge, code)
            if otp and otp['phone'] == profile['phone']:
                result = provision_marketplace(self.identity, db, branch_id, profile, {}, password=password,
                                               signed_profile=intent['profile'])
                self._activate_local(db, result['username'], branch_id, generation)
                self._record_account(db, intent, branch_id, result['username'])
                db.execute('UPDATE identity_otp SET state="used" WHERE id=?', (challenge,))
                return result['username']
        # Commit the failed attempt before reporting it.
        raise ValueError('Код неверен или истёк. Проверьте номер телефона и код из SMS.')

    def _authorize(self, db, username, branch_id):
        admin = self.identity._scope(db, username, branch_id)
        branch = read_document(db, 'branches.json')[branch_id]
        if branch.get('yclients_enabled', True) is False:
            raise MarketplaceError('Этот филиал работает без YCLIENTS')
        if branch.get('onboarding_status') == 'pending_verification':
            raise MarketplaceError('Начните подключение из карточки GateBot в YCLIENTS с передачей данных пользователя')
        if (not admin.get('is_super') and branch.get('yclients_connection_mode', 'direct') == 'direct'
                and branch.get('yclients_connection_source') != 'direct'):
            manual = db.execute('SELECT 1 FROM branch_registrations WHERE branch_id=? AND state="completed" '
                                'AND connection_mode="direct"', (branch_id,)).fetchone()
            if manual or branch.get('id_origin') == 'yclients':
                # A self-entered salon ID gives access only to the new local workspace.
                raise MarketplaceError('Сначала подключите учётную запись YCLIENTS этого филиала в разделе «Интеграции»')

    def _activate_local(self, db, username, branch_id, generation):
        self._authorize(db, username, branch_id)
        current = db.execute('SELECT generation FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone()
        if current and current['generation'] != generation:
            return False  # A later signed disconnect wins a concurrent install.
        self.identity.save_integration(username, branch_id, 'yclients',
            {'user_token': self.system_user_token, 'partner_token': self.partner_token},marketplace=True)
        branches = read_document(db, 'branches.json')
        branches[branch_id]['yclients_marketplace_state'] = 'active'
        branches[branch_id]['yclients_connection_source'] = 'marketplace'
        branches[branch_id]['yclients_connection_mode'] = 'marketplace'
        write_document(db, 'branches.json', branches)
        db.execute('INSERT INTO marketplace_branches VALUES(?,"active",?,?) ON CONFLICT(branch_id) DO UPDATE SET state="active",updated=excluded.updated',
                   (branch_id, generation, self.clock()))
        return True

    def connect(self, raw, browser, username, branch_id, *, reconcile=False):
        branch_id = positive_id(branch_id)
        api = self._api()
        if not reconcile and not self.enabled:
            raise MarketplaceError('Активация в YCLIENTS пока выключена владельцем GateBot')
        with self.store.connect() as db:
            intent = self._intent(db, raw, browser, require_fresh=not reconcile)
            if branch_id not in intent['branches']:
                raise PermissionError('Филиал отсутствует в текущем подключении')
            self._authorize(db, username, branch_id)
            existing = db.execute('SELECT state FROM marketplace_attempts WHERE intent_hash=? AND branch_id=?', (intent['id_hash'], branch_id)).fetchone()
            current = db.execute('SELECT generation,state FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone()
            if existing and not reconcile:
                # The attempt is historical; a later lifecycle callback or
                # provider reconciliation is authoritative for current state.
                return {'state': current['state'] if current else existing['state'], 'replayed': True}
            generation = current['generation'] if current else 0
            # A fresh browser hand-off for an already installed branch checks
            # the provider instead of installing the system user again.
            if current and current['state'] == 'active':
                reconcile = True
            if not reconcile:
                if current and current['state'] in {'installing', 'unknown'}:
                    return {'state': 'unknown', 'code': 'reconcile_required'}
                db.execute('INSERT INTO marketplace_attempts VALUES(?,?,"installing",?,NULL)', (intent['id_hash'], branch_id, self.clock()))
                db.execute('INSERT INTO marketplace_branches VALUES(?,"installing",?,?) ON CONFLICT(branch_id) DO UPDATE SET state="installing",updated=excluded.updated', (branch_id, generation, self.clock()))
        try:
            result = api.status(branch_id) if reconcile else api.install(branch_id)
        except Exception:
            result = {'state': 'unknown', 'code': 'provider_result_unknown'}
        with self.store.connect() as db:
            self._authorize(db, username, branch_id)
            if result['state'] == 'active' and not self._activate_local(db, username, branch_id, generation):
                result = {'state': 'unknown', 'code': 'connection_changed'}
            if result['state'] != 'active':
                db.execute('UPDATE marketplace_branches SET state=?,updated=? WHERE branch_id=? AND generation=?',
                           (result['state'], self.clock(), branch_id, generation))
                current = db.execute('SELECT generation FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone()
                if current and current['generation'] == generation and result['state'] in {'uninstalled', 'frozen'}:
                    self._deactivate_local(db, branch_id, result['state'])
            db.execute('INSERT INTO marketplace_attempts VALUES(?,?,?,?,?) ON CONFLICT(intent_hash,branch_id) DO UPDATE SET state=excluded.state,updated=excluded.updated,code=excluded.code',
                       (intent['id_hash'], branch_id, result['state'], self.clock(), result.get('code')))
        return result

    def disconnect(self, payload):
        if not isinstance(payload, dict) or not self.partner_token or not self.application_id:
            raise PermissionError('Запрос YCLIENTS не подтверждён')
        token = payload.get('partner_token')
        if not isinstance(token, str) or not hmac.compare_digest(token, self.partner_token):
            raise PermissionError('Запрос YCLIENTS не подтверждён')
        if positive_id(payload.get('application_id')) != self.application_id or payload.get('event') not in {'uninstall', 'freeze'}:
            raise PermissionError('Событие не относится к этому приложению')
        branch_id = positive_id(payload.get('salon_id'))
        state = 'uninstalled' if payload['event'] == 'uninstall' else 'frozen'
        with self.store.connect() as db:
            db.execute('INSERT INTO marketplace_branches VALUES(?,?,1,?) ON CONFLICT(branch_id) DO UPDATE SET state=excluded.state,generation=generation+1,updated=excluded.updated',
                       (branch_id, state, self.clock()))
            self._deactivate_local(db, branch_id, state)
        return {'status': 'success'}
