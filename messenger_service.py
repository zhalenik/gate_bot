"""Branch-selected messenger provider; server-owned recipients and scoped settings."""
import re

from link_manager import normalize_phone
from storage import config
from storage.sql_documents import read_document, write_document


class MessengerService:
    def __init__(self, identity):
        self.identity = identity

    def _settings(self, db, branch_id):
        branch_id = str(branch_id)
        branch = read_document(db, 'branches.json').get(branch_id)
        if not branch:
            return {'selected': 'auto', 'provider': None, 'configured': False,
                    'available': {'chatpush': False, 'yclients': False, 'sms': False}, 'reason': 'branch_missing'}
        crm = branch.get('yclients_enabled', True) is not False
        cp = self.identity.integration_credentials(branch_id, 'chatpush') if crm else {}
        yc = self.identity.integration_credentials(branch_id, 'yclients') if crm else {}
        available = {'chatpush': bool(cp.get('token')), 'sms': branch.get('sms_notifications_enabled') is True,
                     'yclients': bool(yc.get('user_token') and (yc.get('partner_token') or config.YC_PARTNER_TOKEN)
                                      and branch.get('yclients_marketplace_state') not in {'uninstalled', 'frozen'})}
        selected = branch.get('messenger_provider', 'auto')
        if not isinstance(selected,str) or (selected!='auto' and selected not in available):
            selected = 'auto'
        provider = next((p for p in ('yclients','chatpush','sms') if available[p]), None) if selected == 'auto' else selected
        configured = bool(available.get(provider))
        return {'selected': selected, 'provider': provider, 'configured': configured,
                'available': available, 'reason': 'ready' if configured else ('provider_not_connected' if crm else 'standalone')}

    def client_state(self, db, branch_id, client):
        state = self._settings(db, branch_id)
        if state['selected'] == 'auto' and state['provider'] == 'yclients' and not self._yc_id(client):
            state['provider'] = next((p for p in ('chatpush','sms') if state['available'][p]), 'yclients')
        connected = state['configured']
        reason = state['reason']
        if str(client.get('branch_id')) != str(branch_id):
            connected, reason = False, 'client_scope'
        elif connected and state['provider'] == 'yclients':
            if not self._yc_id(client):
                connected, reason = False, 'yclients_client_missing'
        elif connected and (client.get('system_phone') or not normalize_phone(client.get('phone'))):
            connected, reason = False, 'phone_missing'
        return {**state, 'connected': connected, 'reason': reason}

    @staticmethod
    def _yc_id(client):
        return re.fullmatch(r'[1-9][0-9]{0,15}', str(client.get('id_ycl', '')))

    def plan(self, db, branch_id, token):
        client = read_document(db, 'links.json').get(token)
        if not client:
            return {'connected': False, 'provider': None, 'target': None, 'reason': 'client_missing'}
        state = self.client_state(db, branch_id, client)
        target = None
        if state['connected']:
            target = (f"yclients:{int(branch_id)}:{int(client['id_ycl'])}" if state['provider'] == 'yclients'
                      else normalize_phone(client.get('phone')))
        return {**state, 'target': target}

    def settings(self, username, branch_id):
        with self.identity.product.connect() as db:
            self.identity._scope(db, username, branch_id)
            return self._settings(db, branch_id)

    def save(self, username, branch_id, provider):
        if not isinstance(provider,str) or provider not in {'auto', 'chatpush', 'yclients', 'sms'}:
            raise ValueError('Выберите способ отправки сообщений')
        with self.identity.product.connect() as db:
            self.identity._scope(db, username, branch_id)
            branches = read_document(db, 'branches.json')
            state = self._settings(db, branch_id)
            if provider != 'auto' and not state['available'][provider]:
                raise ValueError('Выбранный канал не подключён к филиалу')
            branches[str(branch_id)]['messenger_provider'] = provider
            write_document(db, 'branches.json', branches)
            return self._settings(db, branch_id)

    def send(self, branch_id, token, provider, target, text):
        with self.identity.product.connect() as db:
            from notification_policy import client_delivery_enabled
            if not client_delivery_enabled(db, branch_id):
                return {'status': 'failed', 'code': 'outbound_disabled'}
            plan = self.plan(db, branch_id, token)
            if not plan['connected'] or plan['provider'] != provider or plan['target'] != str(target):
                return {'status': 'skipped', 'code': 'messenger_recipient_changed', 'confirmed': False}
            if provider not in {'chatpush','yclients'}:
                return {'status':'failed','code':'invalid_messenger_provider','confirmed':False}
            credentials = self.identity.integration_credentials(branch_id, provider)
            client_id = read_document(db, 'links.json')[token].get('id_ycl')
        if provider == 'chatpush':
            from chatpush import send_message
            return send_message(target, text, branch_id, credentials=credentials)
        if not config.YCLIENTS_ENABLED:
            return {'status': 'pending', 'code': 'yclients_disabled', 'confirmed': False}
        from yclients_delivery import YClientsDelivery
        return YClientsDelivery().send(branch_id, client_id, text,
                                      {**credentials, 'partner_token': credentials.get('partner_token') or config.YC_PARTNER_TOKEN})
