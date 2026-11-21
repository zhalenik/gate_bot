"""Token-only ChatPush delivery. Import and configuration never send messages."""
import re

import requests

from link_manager import normalize_phone


DELIVERY_URL = 'https://api.chatpush.ru/api/v1/delivery'
MAX_TEXT_LENGTH = 3500


def validate_token(token):
    if not isinstance(token, str):
        raise ValueError('Укажите токен ChatPush')
    token = token.strip()
    if not token or len(token) > 4096 or not token.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in token):
        raise ValueError('Вставьте только токен ChatPush, без префикса и пробелов')
    return token


class ChatPushClient:
    def __init__(self, token, *, session=None, timeout=15):
        self.token = validate_token(token)
        self.session = session or requests.Session()
        self.timeout = timeout

    def send(self, phone, text, *, external_id=None):
        # external_id remains local outbox metadata; use the minimal API body.
        if not isinstance(phone, str) or not re.fullmatch(r'[+0-9\s().-]{7,64}', phone):
            return {'status': 'failed', 'code': 'invalid_message', 'confirmed': False}
        phone = normalize_phone(phone).lstrip('+')
        if not re.fullmatch(r'[1-9][0-9]{6,14}', phone) or not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_LENGTH:
            return {'status': 'failed', 'code': 'invalid_message', 'confirmed': False}
        try:
            response = self.session.request(
                'POST', DELIVERY_URL,
                headers={'authorization': self.token, 'content-type': 'application/json', 'accept': 'application/json'},
                json={'text': text, 'phone': phone}, timeout=self.timeout,
                allow_redirects=False, verify=True)
        except requests.RequestException:
            return {'status': 'unknown', 'code': 'transport_error', 'confirmed': False}
        if response.status_code != 200:
            ambiguous = response.status_code in {408, 409} or response.status_code >= 500
            return {'status': 'unknown' if ambiguous else 'failed',
                    'code': 'provider_result_unknown' if ambiguous else 'provider_rejected',
                    'http_status': response.status_code, 'confirmed': False}
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return {'status': 'unknown', 'code': 'invalid_provider_response', 'confirmed': False}
        meta = data.get('meta')
        if isinstance(meta, dict) and meta.get('status') in ('fail', 'failed', 'error'):
            return {'status': 'failed', 'code': 'provider_rejected', 'confirmed': False}
        delivery = data.get('delivery')
        if (not isinstance(delivery, dict) or not isinstance(delivery.get('id'), (str, int))
                or isinstance(delivery.get('id'), bool) or not str(delivery['id']).strip()):
            return {'status': 'unknown', 'code': 'invalid_provider_response', 'confirmed': False}
        return {'status': 'accepted', 'provider_id': str(delivery['id']),
                'provider_status': delivery.get('status_id'), 'confirmed': False}


def send_message(phone, text, branch_id=None, *, credentials=None, external_id=None, audience='client', **kwargs):
    from notification_policy import transport_enabled
    if not transport_enabled(audience):
        return {'status': 'error', 'skipped': True, 'message': 'Отправка уведомлений выключена'}
    if not credentials or not credentials.get('token'):
        return {'status': 'error', 'skipped': True, 'message': 'ChatPush не подключён для филиала'}
    return ChatPushClient(credentials['token']).send(phone, text, external_id=external_id)


def send_link(phone, link, branch_id=None, **kwargs):
    from message_templates import pass_link
    return send_message(phone, pass_link(link), branch_id, **kwargs)
