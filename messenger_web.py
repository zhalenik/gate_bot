"""Authenticated branch messenger settings. Saving never sends messages."""
from flask import Blueprint, abort, jsonify, request, session
from messenger_service import MessengerService
from integration_test_delivery import IntegrationTestDelivery, TestRequestConflict


def create_blueprint(provider):
    bp = Blueprint('messenger_settings', __name__)

    @bp.route('/admin/api/messenger-settings', methods=['GET', 'POST'])
    def settings():
        services = provider()
        actor = services['auth'].get_current_admin()
        if not actor:
            abort(401)
        data = request.get_json(silent=True) if request.method == 'POST' else request.args
        if data is None or not hasattr(data, 'get'):
            abort(400)
        branch_id = data.get('branch_id') or actor.get('branch_id')
        if branch_id is None:
            abort(400, description='Выберите филиал')
        service = MessengerService(services['service'])
        try:
            state = (service.save(session['admin_username'], str(branch_id), data.get('provider'))
                     if request.method == 'POST' else service.settings(session['admin_username'], str(branch_id)))
        except PermissionError as error:
            return jsonify(status='error', message=str(error)), 403
        except ValueError as error:
            return jsonify(status='error', message=str(error)), 400
        return jsonify(status='success', messaging=state)

    @bp.route('/admin/api/integration-test', methods=['GET', 'POST'])
    def test_delivery():
        services = provider()
        actor = services['auth'].get_current_admin()
        if not actor:
            abort(401)
        data = request.get_json(silent=True) if request.method == 'POST' else request.args.to_dict()
        if not isinstance(data, dict):
            return jsonify(status='error', message='Некорректные параметры тестовой отправки'), 400
        branch_id = data.get('branch_id', actor.get('branch_id'))
        if branch_id is None:
            return jsonify(status='error', message='Выберите филиал'), 400
        service = IntegrationTestDelivery(services['service'], services['notifications'])
        try:
            if request.method == 'GET':
                if set(data) - {'branch_id', 'provider'}:
                    raise ValueError('Некорректные параметры списка клиентов')
                return jsonify(status='success', test=service.settings(session['admin_username'], branch_id, data.get('provider')))
            data['branch_id'] = branch_id
            return jsonify(status='success', delivery=service.send(session['admin_username'], data))
        except PermissionError as error:
            return jsonify(status='error', message=str(error)), 403
        except TestRequestConflict as error:
            return jsonify(status='error', message=str(error)), 409
        except ValueError as error:
            return jsonify(status='error', message=str(error)), 400

    return bp
