"""Match local clients to a branch's CRM directory, without changing CRM data."""
import re

from identity_service import valid_phone
from storage import config
from storage.sql_documents import read_document, write_document
from yclients_api import YClientsClient, YClientsError


def _id(value):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]{0,15}', str(value)):
        return None
    return int(value)


def _same_id(left, right):
    return (None if left in (None, '') else str(left)) == (None if right in (None, '') else str(right))


def _phone_key(value):
    try:
        return valid_phone(value)
    except ValueError:
        return value


class YClientsClientSync:
    def __init__(self, identity, branches, crm=None):
        self.identity, self.branches = identity, branches
        self._crm = crm

    @property
    def crm(self):
        # A malformed saved connection must not prevent opening unrelated
        # administration pages (including the system settings needed to fix it).
        if self._crm is None:
            self._crm = YClientsClient(branches=self.branches,
                credential_provider=lambda branch_id: self.identity.integration_credentials(branch_id, 'yclients'))
        return self._crm

    @crm.setter
    def crm(self, value):
        self._crm = value

    def _availability(self, db, branch_id, actor=None):
        if actor is not None:
            self.identity._scope(db, actor, branch_id)
        branch = read_document(db, 'branches.json').get(str(branch_id))
        if branch is None:
            return {'ready': False, 'message': 'Филиал не найден.'}
        if branch.get('yclients_enabled', True) is False:
            raise PermissionError('Филиал работает без YCLIENTS')
        if not config.YCLIENTS_ENABLED:
            return {'ready': False, 'message': 'YCLIENTS выключен в системных настройках.'}
        if branch.get('yclients_marketplace_state') in {'uninstalled', 'frozen'}:
            return {'ready': False, 'message': 'Подключение YCLIENTS приостановлено или отключено.'}
        credentials = self.identity.integration_credentials(str(branch_id), 'yclients')
        if not credentials.get('user_token') or not (credentials.get('partner_token') or config.YC_PARTNER_TOKEN):
            return {'ready': False, 'message': 'Сначала подключите YCLIENTS в этом филиале.'}
        return {'ready': True, 'message': 'Поиск выполняется по сохранённому телефону в клиентской базе филиала YCLIENTS.'}

    def readiness(self, username, branch_id):
        with self.identity.product.connect() as db:
            return self._availability(db, str(branch_id), username)

    @staticmethod
    def _result(status, message, client=None):
        return {'status': status, 'message': message, 'id_ycl': (client or {}).get('id_ycl')}

    def refresh(self, username, token):
        if not isinstance(token, str) or not token or len(token) > 128:
            raise ValueError('Выберите клиента')
        with self.identity.product.connect() as db:
            client = read_document(db, 'links.json').get(token)
            if not client:
                raise ValueError('Клиент не найден')
            self.identity._scope(db, username, client['branch_id'])
            snapshot = (str(client['branch_id']), client.get('phone'), client.get('id_ycl'))
        return self.refresh_snapshot(snapshot[0], token, snapshot[1], snapshot[2], actor=username)

    def refresh_snapshot(self, branch_id, token, phone, expected_id, actor=None):
        """Read outside SQL locks; recheck the saved identity and scope on commit."""
        result = self._refresh_snapshot(str(branch_id), token, phone, expected_id, actor)
        import logger
        logger.event('CRM_CLIENT_ID_LOOKUP', branch_id=branch_id, actor=actor,
                     details={'outcome': result['status']})
        return result

    def _snapshot(self, db, branch_id, token, phone, expected_id, actor):
        state = self._availability(db, branch_id, actor)
        client = read_document(db, 'links.json').get(token)
        if (not client or str(client.get('branch_id')) != branch_id or
                _phone_key(client.get('phone')) != _phone_key(phone) or not _same_id(client.get('id_ycl'), expected_id)):
            return client, self._result('stale', 'Карточка клиента изменилась. Обновите её перед повторным поиском.', client)
        if not state['ready']:
            return client, self._result('unavailable', state['message'], client)
        return client, None

    def _refresh_snapshot(self, branch_id, token, phone, expected_id, actor):
        with self.identity.product.connect() as db:
            client, stopped = self._snapshot(db, branch_id, token, phone, expected_id, actor)
            if stopped:
                return stopped
        try:
            normalized = valid_phone(phone)
        except ValueError:
            return self._result('error', 'В карточке нет полного корректного телефона. Сначала сохраните номер.', client)
        try:
            candidates = self.crm.find_clients_by_phone(branch_id, normalized)
            if not isinstance(candidates, list):
                raise YClientsError('Invalid clients collection')
            identities = set()
            for candidate in candidates:
                if (not isinstance(candidate, dict) or _id(candidate.get('id')) is None or
                        (candidate.get('company_id') is not None and str(candidate['company_id']) != branch_id)):
                    raise YClientsError('Invalid client identity')
                try:
                    candidate_phone = valid_phone(candidate.get('phone'))
                except ValueError:
                    raise YClientsError('Client phone unavailable') from None
                if candidate_phone == normalized:
                    identities.add(_id(candidate['id']))
        except Exception as error:
            # Do not expose responses/phones/tokens. A read failure never clears
            # an existing ID or prevents creating the local client.
            message = ('Не удалось прочитать клиентскую базу YCLIENTS. Проверьте подключение и права '
                       'на просмотр всех клиентов и полных номеров телефонов; существующий ID сохранён.')
            if isinstance(error, YClientsError) and 'HTTP 429' in str(error):
                message = 'YCLIENTS ограничил частоту запросов. Повторите позже; существующий ID сохранён.'
            return self._result('error', message, client)
        with self.identity.product.connect() as db:
            client, stopped = self._snapshot(db, branch_id, token, phone, expected_id, actor)
            if stopped:
                return stopped
            if not identities:
                return self._result('not_found', 'Клиент с таким телефоном в YCLIENTS не найден. Существующий ID сохранён.', client)
            if len(identities) != 1:
                return self._result('ambiguous', 'В YCLIENTS несколько клиентов с таким телефоном. ID не изменён; проверьте карточки в CRM.', client)
            client_id = next(iter(identities))
            links = read_document(db, 'links.json')
            # Guest links are separate one-time grants. Only the ordinary
            # directory entry owns the canonical appointment identity.
            if not client.get('is_temporary'):
                for other_token, other in links.items():
                    if other_token == token or other.get('is_temporary') or str(other.get('branch_id')) != branch_id:
                        continue
                    try:
                        same_phone = valid_phone(other.get('phone')) == normalized
                    except ValueError:
                        same_phone = False
                    if _id(other.get('id_ycl')) == client_id or same_phone:
                        return self._result('conflict', 'В филиале уже есть другая обычная карточка с этим телефоном или ID. Устраните дубликат; карточки автоматически не объединяются.', client)
            if _same_id(client.get('id_ycl'), client_id):
                return self._result('unchanged', 'ID YCLIENTS подтверждён по телефону.', client)
            links[token]['id_ycl'] = client_id
            write_document(db, 'links.json', links)
            return self._result('updated', 'ID YCLIENTS получен по телефону и сохранён.', links[token])
