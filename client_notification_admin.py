"""Scoped client notification controls for the administrative client table."""
from flask import Blueprint, abort, jsonify, request, session

from storage.sql_documents import read_document


class ClientNotificationAdmin:
    def __init__(self, identity):
        self.identity = identity

    def _client(self, db, username, token):
        if not isinstance(token, str):
            raise ValueError('Выберите клиента')
        client = read_document(db, 'links.json').get(token)
        if not client:
            raise ValueError('Клиент не найден')
        self.identity._scope(db, username, client['branch_id'])
        return client

    def _available(self, client):
        if client.get('is_temporary'):
            raise PermissionError('Для гостевого пропуска автоматические уведомления недоступны')

    def get(self, username, token):
        with self.identity.product.connect() as db:
            client = self._client(db, username, token)
            self._available(client)
            return self.identity.prefs_for_token(token)

    def summary(self, username, token):
        with self.identity.product.connect() as db:
            client = self._client(db, username, token)
            return self._summaries(db, {token: client})[token]

    def summaries(self, username, tokens):
        with self.identity.product.connect() as db:
            clients, _ = self.identity._client_table_snapshot(db, username, tokens)
            return self._summaries(db, clients)

    def _summaries(self, db, clients):
        result = {}
        for token, client in clients.items():
            if client.get('is_temporary'):
                result[token] = {'available': False, 'enabled': False, 'effective_channels': {}, 'reason': 'guest'}
                continue
            person_id = self.identity._client(db, token, client)
            preferences = self.identity._prefs_for_client(db, person_id, str(client['branch_id']), client)
            result[token] = {'available': True, 'enabled': preferences['enabled'],
                             'effective_channels': preferences['effective_channels'],
                             'allowed_channels': preferences['allowed_channels']}
        return result

    def save(self, username, token, payload):
        if not isinstance(payload, dict) or set(payload) - {'enabled', 'channels', 'email'}:
            raise ValueError('Некорректные настройки уведомлений')
        with self.identity.product.connect() as db:
            client = self._client(db, username, token)
            self._available(client)
            # Keep the existing administrative-profile email protections intact.
            return self.identity.save_prefs_for_token(token, payload, actor_username=username)

    def verify_email(self, username, token):
        with self.identity.product.connect() as db:
            client = self._client(db, username, token)
            self._available(client)
            person_id, branch_id, legacy, _ = self.identity._preference_identity(db, token)
        # The delivery performs its own person/client/branch check. Do not hold
        # the application-wide write transaction during an SMTP request.
        return self.identity._email_request(person_id, branch_id, client_token=legacy)

    def bot_link(self, username, token, platform):
        with self.identity.product.connect() as db:
            client = self._client(db, username, token)
            self._available(client)
            # This is a client link. It cannot grant an administrative binding.
            return self.identity.client_bot_link(token, platform)


def create_blueprint(provider):
    bp = Blueprint('client_notification_admin', __name__)

    def context():
        services = provider()
        if not services['auth'].get_current_admin():
            abort(401)
        return ClientNotificationAdmin(services['service']), session['admin_username']

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400)
        return data

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(status='error', message=str(error)), 400

    @bp.errorhandler(PermissionError)
    def forbidden(error):
        return jsonify(status='error', message=str(error)), 403

    @bp.get('/admin/api/client-notifications')
    def read():
        service, username = context()
        return jsonify(preferences=service.get(username, request.args.get('token')))

    @bp.post('/admin/api/client-notifications')
    def save():
        service, username = context()
        data = body()
        token = data.pop('token', None)
        return jsonify(status='success', preferences=service.save(username, token, data))

    @bp.post('/admin/api/client-notifications/verify-email')
    def verify_email():
        service, username = context()
        data = body()
        if set(data) != {'token'}:
            abort(400)
        return jsonify(delivery=service.verify_email(username, data['token']))

    @bp.post('/admin/api/client-notifications/bot-link')
    def bot_link():
        service, username = context()
        data = body()
        if set(data) != {'token', 'platform'}:
            abort(400)
        return jsonify(link=service.bot_link(username, data['token'], data['platform']))

    return bp
