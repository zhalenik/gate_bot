"""Explicit, scoped, single-recipient integration diagnostics.

No provider calls on import, configuration or recipient listing. Delivery uses
the normal durable effect claim and never changes notification preferences.
"""
import hashlib
import hmac
import json
import re
import time

from identity_service import valid_phone
from storage import config
from storage.sql_documents import read_document


LIMITS = {'chatpush': 3500, 'yclients': 16000}
READY_MESSAGES = {
    'ready': 'Можно отправить тестовое сообщение.',
    'outbound_disabled': 'Отправка сообщений выключена в системных настройках.',
    'yclients_disabled': 'YCLIENTS выключен в системных настройках.',
    'credentials_missing': 'Сначала подключите выбранный сервис в этом филиале.',
    'integration_unavailable': 'Подключение YCLIENTS отключено или приостановлено. Подключите его заново.',
}


class TestRequestConflict(ValueError):
    __test__ = False


class IntegrationTestDelivery:
    def __init__(self, identity, notifications=None):
        self.identity, self.notifications = identity, notifications
        with identity.product.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS integration_test_requests('
                       'id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,provider TEXT NOT NULL,'
                       'fingerprint TEXT NOT NULL,created REAL NOT NULL)')

    @staticmethod
    def _provider(provider):
        if not isinstance(provider, str) or provider not in LIMITS:
            raise ValueError('Выберите ChatPush или YCLIENTS')
        return provider

    def _state(self, db, username, branch_id, provider):
        self._provider(provider)
        self.identity._scope(db, username, branch_id)
        branch = read_document(db, 'branches.json')[str(branch_id)]
        if branch.get('yclients_enabled', True) is False:
            raise PermissionError('Для этого филиала подключение сервиса рассылки недоступно')
        credentials = self.identity.integration_credentials(branch_id, provider)
        available = (bool(credentials.get('token')) if provider == 'chatpush' else
                     bool(credentials.get('user_token') and (credentials.get('partner_token') or config.YC_PARTNER_TOKEN)))
        if not config.OUTBOUND_ENABLED:
            reason = 'outbound_disabled'
        elif provider == 'yclients' and not config.YCLIENTS_ENABLED:
            reason = 'yclients_disabled'
        elif provider == 'yclients' and branch.get('yclients_marketplace_state') in {'frozen', 'uninstalled'}:
            reason = 'integration_unavailable'
        else:
            reason = 'ready' if available else 'credentials_missing'
        return {'provider': provider, 'ready': reason == 'ready', 'reason': reason,
                'message': READY_MESSAGES[reason], 'max_length': LIMITS[provider]}

    @staticmethod
    def _yc_id(client):
        value = client.get('id_ycl')
        return (int(value) if not isinstance(value, bool) and
                re.fullmatch(r'[1-9][0-9]{0,15}', str(value)) else None)

    def settings(self, username, branch_id, provider):
        with self.identity.product.connect() as db:
            state = self._state(db, username, branch_id, provider)
            clients = []
            for token, client in read_document(db, 'links.json').items():
                if str(client.get('branch_id')) != str(branch_id):
                    continue
                try:
                    phone = valid_phone(client.get('phone'))
                except ValueError:
                    phone = ''
                yc_id = self._yc_id(client)
                if (provider == 'yclients' and yc_id is None) or (provider == 'chatpush' and not phone):
                    continue
                clients.append({'token': token, 'name': str(client.get('user_name') or 'Без имени'),
                                'phone': phone, 'yclients_id': yc_id})
            clients.sort(key=lambda client: (client['name'].casefold(), client['phone'], client['token']))
            return {**state, 'clients': clients}

    def _recipient(self, db, branch_id, provider, recipient):
        if set(recipient) == {'phone'} and provider == 'chatpush':
            return valid_phone(recipient['phone']), None
        if set(recipient) != {'client_token'} or not isinstance(recipient['client_token'], str):
            raise ValueError('Выберите клиента филиала' if provider == 'yclients' else 'Введите телефон или выберите клиента')
        client = read_document(db, 'links.json').get(recipient['client_token'])
        if not client or str(client.get('branch_id')) != str(branch_id):
            raise ValueError('Клиент не найден в выбранном филиале')
        if provider == 'yclients':
            yc_id = self._yc_id(client)
            if yc_id is None:
                raise ValueError('У выбранного клиента нет ID YCLIENTS')
            return f'yclients:{int(branch_id)}:{yc_id}', yc_id
        return valid_phone(client.get('phone')), None

    def send(self, username, payload):
        fields = {'branch_id', 'provider', 'text', 'request_id', 'phone', 'client_token'}
        if (not isinstance(payload, dict) or set(payload) - fields or
                not {'branch_id', 'provider', 'text', 'request_id'} <= set(payload)):
            raise ValueError('Некорректные параметры тестовой отправки')
        provider = self._provider(payload['provider'])
        text, request_id = payload['text'], payload['request_id']
        if not isinstance(text, str) or not text.strip() or len(text) > LIMITS[provider]:
            raise ValueError(f'Введите сообщение длиной от 1 до {LIMITS[provider]} символов')
        if not isinstance(request_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', request_id):
            raise ValueError('Обновите форму тестовой отправки')
        branch_id = str(payload['branch_id'])
        recipient = {key: payload[key] for key in ('phone', 'client_token') if key in payload}
        with self.identity.product.connect() as db:
            state = self._state(db, username, branch_id, provider)
            target, _ = self._recipient(db, branch_id, provider, recipient)
            if 'phone' in recipient:
                recipient['phone'] = target
            request_key = hashlib.sha256(json.dumps([username, request_id]).encode()).hexdigest()
            body = json.dumps([branch_id, provider, recipient, text], sort_keys=True, ensure_ascii=False)
            fingerprint = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()
            previous = db.execute('SELECT fingerprint FROM integration_test_requests WHERE id=?', (request_key,)).fetchone()
            if previous and not hmac.compare_digest(previous['fingerprint'], fingerprint):
                raise TestRequestConflict('Этот запрос уже использован для другого сообщения. Начните новую отправку.')
            if not state['ready']:
                return self._result({'status': 'failed', 'code': state['reason']})
            if previous is None:
                db.execute('INSERT INTO integration_test_requests VALUES(?,?,?,?,?)',
                           (request_key, branch_id, provider, fingerprint, time.time()))
        result = self.notifications.send(provider, target, text, {
            'purpose': 'integration_test', 'branch_id': branch_id,
            'idempotency_key': 'integration-test:' + request_key,
            'test_actor': username, 'test_recipient': recipient,
        })
        result = self._result(result)
        import logger
        logger.event('INTEGRATION_TEST_MESSAGE', branch_id=branch_id, actor=username,
                     details={'provider': provider, 'result': result['status'], 'code': result['code']})
        return result

    def deliver(self, provider, target, text, context):
        """Recheck scope and exact recipient after the durable effect claim."""
        try:
            with self.identity.product.connect() as db:
                branch_id = context.get('branch_id')
                state = self._state(db, context.get('test_actor'), branch_id, provider)
                if not state['ready']:
                    return {'status': 'failed', 'code': state['reason'], 'confirmed': False}
                current, yc_id = self._recipient(db, branch_id, provider, context.get('test_recipient', {}))
                if current != target:
                    return {'status': 'skipped', 'code': 'messenger_recipient_changed', 'confirmed': False}
                credentials = self.identity.integration_credentials(branch_id, provider)
        except (PermissionError, ValueError):
            return {'status': 'skipped', 'code': 'recipient_or_access_changed', 'confirmed': False}
        if provider == 'chatpush':
            from chatpush import send_message
            return send_message(target, text, branch_id, credentials=credentials,
                                external_id=context['idempotency_key'])
        from yclients_delivery import YClientsDelivery
        return YClientsDelivery().send(branch_id, yc_id, text,
            {**credentials, 'partner_token': credentials.get('partner_token') or config.YC_PARTNER_TOKEN})

    @staticmethod
    def _result(result):
        # Do not return provider IDs, messages, bodies, addresses or exceptions.
        status = result.get('status') if isinstance(result, dict) else 'unknown'
        if status not in {'accepted', 'failed', 'unknown', 'skipped', 'pending'}:
            status = 'unknown'
        messages = {
            **READY_MESSAGES,
            'provider_accepted': 'Сервис принял сообщение. Это ещё не подтверждение доставки клиенту.',
            'provider_rejected': 'Сервис отклонил отправку. Проверьте подключение, права на рассылку и баланс сервиса.',
            'transport_error': 'Ответ сервиса не получен. Сообщение могло уйти; автоматического повтора не будет.',
            'provider_result_unknown': 'Результат отправки неизвестен. Проверьте получение сообщения у клиента.',
            'transport_result_unknown': 'Результат отправки неизвестен. Проверьте получение сообщения у клиента.',
            'invalid_provider_response': 'Не удалось подтвердить результат отправки. Автоматического повтора не будет.',
            'effect_result_unknown': 'Результат предыдущей отправки неизвестен. Повторное сообщение не отправлено.',
            'effect_in_progress': 'Этот запрос уже обрабатывается. Повторное сообщение не отправлено.',
            'messenger_recipient_changed': 'Данные получателя изменились. Обновите список клиентов.',
            'recipient_or_access_changed': 'Получатель или права доступа изменились. Обновите страницу.',
            'branch_deleted': 'Филиал удалён. Сообщение не отправлено.',
            'runtime_stopping': 'Сервис завершает работу. Повторите после запуска.',
        }
        code = result.get('code') if isinstance(result, dict) else None
        if code not in messages:
            code = 'provider_accepted' if status == 'accepted' else ('provider_rejected' if status == 'failed' else 'provider_result_unknown')
        public = {'status': status, 'code': code, 'confirmed': False, 'message': messages[code]}
        http_status = result.get('http_status') if isinstance(result, dict) else None
        if type(http_status) is int and 100 <= http_status <= 599:
            public['http_status'] = http_status
        return public
