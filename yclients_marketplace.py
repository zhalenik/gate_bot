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
        # The signature authenticates the profile, NOT salon_id. Automatic
        # onboarding also checks the user against the installed company's users.
    return ids, profile


class MarketplaceAPI:
    ROOT = 'https://api.yclients.ru'

    def __init__(self, partner_token, application_id, *, session=None, installation_enabled=False):
        self.partner_token = partner_token
        self.application_id = positive_id(application_id)
        self.session = session or requests.Session()
        self.installation_enabled = installation_enabled

    def _request(self, method, path, payload=None):
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

    def status(self, branch_id):
        return self._request('GET', '/marketplace/salon/' + positive_id(branch_id) + '/application/' + self.application_id)

    def verify_user(self, branch_id, profile, system_user_token):
        """Bind signed contact data to this company before granting a local role."""
        from identity_service import valid_email, valid_phone
        phone, email = valid_phone(profile.get('phone')), valid_email(profile.get('email'))
        try:
            response = self.session.request('GET', self.ROOT + '/api/v1/company/' + positive_id(branch_id) + '/users',
                headers={'Authorization': 'Bearer ' + self.partner_token + ', User ' + system_user_token,
                         'Accept': 'application/vnd.yclients.v2+json'},
                timeout=(5, 20), allow_redirects=False, verify=True)
        except requests.RequestException:
            raise MarketplaceError('Не удалось проверить пользователя филиала. Повторите проверку состояния подключения.') from None
        if response.status_code != 200:
            raise MarketplaceError('Приложение активировано, но YCLIENTS не разрешил проверить пользователей филиала. '
                                   'Проверьте права системного пользователя на чтение пользователей и повторите проверку.')
        try:
            payload = response.json()
            users = payload.get('data') if payload.get('success') is True else None
            if not isinstance(users, list) or not all(isinstance(user, dict) for user in users):
                raise ValueError()
            matches = []
            for user in users:
                if user.get('is_approved') not in (True, 1):
                    continue
                try:
                    if valid_phone(user.get('phone')) == phone and valid_email(user.get('email')) == email:
                        matches.append(user)
                except ValueError:
                    continue
            if len(matches) != 1:
                raise MarketplaceError('YCLIENTS не подтвердил пользователя в выбранном филиале. '
                                       'Проверьте телефон, email и принятый доступ пользователя в YCLIENTS.')
        except (ValueError, TypeError, AttributeError):
            raise MarketplaceError('Не удалось подтвердить пользователя филиала по ответу YCLIENTS. '
                                   'Проверьте его телефон, email и доступ к филиалу.') from None
        return True


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
            auto = self._automatic_profile(intent)
            for branch_id in intent['branches']:
                accessible = bool(admin and (admin.get('is_super') or branch_id in admin['branch_ids']))
                branch = branches.get(branch_id) if accessible else None
                attempt = db.execute('SELECT state,code FROM marketplace_attempts WHERE intent_hash=? AND branch_id=?', (intent['id_hash'], branch_id)).fetchone() if accessible or auto else None
                current = db.execute('SELECT state FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone() if accessible else None
                result.append({'id': branch_id, 'name': branch.get('name') if branch else None,
                    'accessible': accessible, 'pending_verification': bool(branch and branch.get('onboarding_status') == 'pending_verification'),
                    'state': current['state'] if current else attempt['state'] if attempt else 'pending'})
            return {'branches': result, 'profile': intent['profile'], 'expires_at': intent['expires'],
                    'expired': intent['expires'] <= self.clock(), 'configured': self.configured(), 'enabled': self.enabled,
                    'authenticated': bool(admin), 'auto_onboarding': auto,
                    'account': self._onboarding_account(db, intent)}

    @staticmethod
    def _automatic_profile(intent):
        from identity_service import valid_email, valid_phone
        try:
            return bool(valid_phone(intent['profile'].get('phone')) and valid_email(intent['profile'].get('email')))
        except ValueError:
            return False

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
        required = bool(admin.get('marketplace_password_setup_required'))
        proof = self._hash(str(admin.get('person_id')) + '|' + str(admin.get('password_hash')) + '|' + str(admin.get('session_version', 0)))
        return {'username': row['username'], 'password_setup_required': required,
                'can_set_password': bool(required and row['created_account'] and intent['expires'] > self.clock()
                                         and hmac.compare_digest(row['auth_proof'], proof))}

    def _deactivate_local(self, db, branch_id, state):
        if state not in {'uninstalled', 'frozen'}:
            return
        branches = read_document(db, 'branches.json')
        branch = branches.get(branch_id)
        if branch and branch.get('yclients_connection_source') != 'direct':
            branch['yclients_marketplace_state'] = state
            write_document(db, 'branches.json', branches)
            if state == 'uninstalled':
                db.execute('DELETE FROM identity_integrations WHERE branch_id=? AND kind="yclients"', (branch_id,))

    def onboard(self, raw, browser, branch_id, *, reconcile=False):
        """Accept YC consent automatically without a GateBot superadmin review.

        Durable claims prevent duplicate installations. An uncertain POST is
        reconciled with GET, never blindly retried. No DB lock spans the network.
        """
        from marketplace_onboarding import provision_marketplace, validate_marketplace_target
        branch_id = positive_id(branch_id)
        api = self._api()
        if not self.enabled:
            raise MarketplaceError('Активация в YCLIENTS пока выключена владельцем GateBot')
        with self.store.connect() as db:
            intent = self._intent(db, raw, browser)
            if branch_id not in intent['branches']:
                raise PermissionError('Филиал отсутствует в текущем подключении')
            if not self._automatic_profile(intent):
                raise MarketplaceError('Для автоматического подключения включите передачу данных пользователя '
                                       'в карточке приложения YCLIENTS и укажите телефон и email пользователя.')
            validate_marketplace_target(self.identity, db, branch_id, intent['profile'])
            attempt = db.execute('SELECT state FROM marketplace_attempts WHERE intent_hash=? AND branch_id=?',
                                 (intent['id_hash'], branch_id)).fetchone()
            current = db.execute('SELECT * FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone()
            if reconcile and not current and not attempt:
                raise MarketplaceError('Сначала начните подключение из карточки GateBot в YCLIENTS')
            account = self._onboarding_account(db, intent, branch_id)
            if attempt and account and not reconcile:
                return {'state': current['state'] if current else attempt['state'], 'account': account, 'replayed': True}
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
        try:
            result = api.status(branch_id) if reconcile else api.install(branch_id)
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
        # The hand-off signature alone cannot authorize the query-string salon.
        if api.verify_user(branch_id, intent['profile'], self.system_user_token) is not True:
            raise MarketplaceError('YCLIENTS не подтвердил пользователя филиала')
        with self.store.connect() as db:
            intent = self._intent(db, raw, browser)
            current = db.execute('SELECT generation,state FROM marketplace_branches WHERE branch_id=?', (branch_id,)).fetchone()
            if not current or current['generation'] != generation or current['state'] != 'active':
                return {'state': 'unknown', 'code': 'connection_changed'}
            result = provision_marketplace(self.identity, db, branch_id, intent['profile'],
                                           {'name': intent['profile'].get('salon_name')})
            self._activate_local(db, result['username'], branch_id, generation)
            admin = self.identity._admin(db, result['username'])
            proof = self._hash(str(admin.get('person_id')) + '|' + str(admin.get('password_hash')) + '|' + str(admin.get('session_version', 0)))
            db.execute('INSERT INTO marketplace_registrations VALUES(?,?,?,?,?) '
                       'ON CONFLICT(intent_hash,branch_id) DO NOTHING',
                       (intent['id_hash'], branch_id, result['username'], int(result['created']), proof))
            return {'state': 'active', 'account': self._onboarding_account(db, intent, branch_id)}

    def set_onboarding_password(self, raw, browser, password, confirm):
        from marketplace_onboarding import set_marketplace_password
        with self.store.connect() as db:
            intent = self._intent(db, raw, browser)
            account = self._onboarding_account(db, intent)
            if not account or not account['can_set_password']:
                raise PermissionError('Установка пароля недоступна. Войдите в свой аккаунт или восстановите пароль.')
            set_marketplace_password(self.identity, db, account['username'], password, confirm)
            return account['username']

    def _authorize(self, db, username, branch_id):
        self.identity._scope(db, username, branch_id)
        branch = read_document(db, 'branches.json')[branch_id]
        if branch.get('yclients_enabled', True) is False:
            raise MarketplaceError('Этот филиал работает без YCLIENTS')
        if branch.get('onboarding_status') == 'pending_verification':
            raise MarketplaceError('Подключение нового филиала сначала должен подтвердить суперадминистратор')

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
                    branches = read_document(db, 'branches.json')
                    if branches[branch_id].get('yclients_connection_source') != 'direct':
                        branches[branch_id]['yclients_marketplace_state'] = result['state']
                        write_document(db, 'branches.json', branches)
                        if result['state'] == 'uninstalled':
                            db.execute('DELETE FROM identity_integrations WHERE branch_id=? AND kind="yclients"', (branch_id,))
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
            branches = read_document(db, 'branches.json')
            db.execute('INSERT INTO marketplace_branches VALUES(?,?,1,?) ON CONFLICT(branch_id) DO UPDATE SET state=excluded.state,generation=generation+1,updated=excluded.updated',
                       (branch_id, state, self.clock()))
            if branch_id not in branches:
                return {'status': 'success'}
            if branches[branch_id].get('yclients_connection_source') != 'direct':
                branches[branch_id]['yclients_marketplace_state'] = state
                write_document(db, 'branches.json', branches)
                if state == 'uninstalled':
                    db.execute('DELETE FROM identity_integrations WHERE branch_id=? AND kind="yclients"', (branch_id,))
        return {'status': 'success'}
