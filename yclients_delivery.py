"""Explicit message delivery; the separate CRM reader remains GET-only.

Contract: https://developers.yclients.com/ru/#tag/SMS-rassylka
Only a nonempty, single client ID is accepted. No phone/filter/bulk endpoint.
"""
import re

import requests


class YClientsDelivery:
    def __init__(self, session=None, pace=None):
        self.session = session or requests.Session()
        if pace is None:
            from yclients_api import _pace
            pace = _pace
        self.pace = pace

    def send(self, branch_id, client_id, text, credentials):
        if (isinstance(branch_id, bool) or isinstance(client_id, bool)
                or not re.fullmatch(r'[1-9][0-9]{0,15}', str(branch_id))
                or not re.fullmatch(r'[1-9][0-9]{0,15}', str(client_id))
                or not isinstance(text, str) or not text.strip() or len(text) > 16000):
            return {'status': 'failed', 'code': 'invalid_message', 'confirmed': False}
        partner = credentials.get('partner_token')
        user = credentials.get('user_token')
        if any(not isinstance(token, str) or not token or len(token)>4096 or
               not token.isascii() or any(ord(c)<=32 or ord(c)==127 for c in token)
               for token in (partner, user)):
            return {'status': 'failed', 'code': 'credentials_missing', 'confirmed': False}
        try:
            self.pace()
            response = self.session.post(
                'https://api.yclients.ru/api/v1/sms/clients/by_id/' + str(branch_id),
                headers={'Authorization': f'Bearer {partner}, User {user}',
                         'Accept': 'application/vnd.yclients.v2+json', 'Content-Type': 'application/json'},
                json={'client_ids': [int(client_id)], 'text': text},
                timeout=(5, 20), allow_redirects=False, verify=True)
        except requests.RequestException:
            return {'status': 'unknown', 'code': 'transport_error', 'confirmed': False}
        # The documented response is 201, but the live service also returns
        # 202 Accepted. Check its success envelope for every successful HTTP
        # status instead of mistaking an accepted request for a rejection.
        if not 200 <= response.status_code < 300:
            uncertain = response.status_code in {408, 409} or response.status_code >= 500
            return {'status': 'unknown' if uncertain else 'failed',
                    'code': 'provider_result_unknown' if uncertain else 'provider_rejected',
                    'http_status': response.status_code, 'confirmed': False}
        try:
            data = response.json()
        except ValueError:
            data = None
        if not isinstance(data, dict) or type(data.get('success')) is not bool:
            return {'status': 'unknown', 'code': 'invalid_provider_response',
                    'http_status': response.status_code, 'confirmed': False}
        return {'status': 'accepted' if data['success'] else 'failed',
                'code': 'provider_accepted' if data['success'] else 'provider_rejected',
                'http_status': response.status_code, 'confirmed': False}
