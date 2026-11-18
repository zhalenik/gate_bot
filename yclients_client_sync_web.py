"""Scoped explicit refresh controls; batch work belongs to the runtime worker."""
from flask import Blueprint, jsonify, request, session


def create_blueprint(provider):
    bp = Blueprint('yclients_client_sync', __name__)

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(status='error', message=str(error)), 400

    @bp.errorhandler(PermissionError)
    def forbidden(error):
        return jsonify(status='error', message=str(error)), 403

    def context():
        from flask import abort
        services = provider()
        actor = services['auth'].get_current_admin()
        if not actor:
            abort(401)
        return services, session['admin_username'], actor

    @bp.post('/admin/api/yclients/client-id')
    def refresh_client():
        services, username, _ = context()
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'token'}:
            raise ValueError('Выберите сохранённую карточку клиента')
        result = services['yclients_client_sync'].refresh(username, data['token'])
        return jsonify(status='success', result=result)

    @bp.route('/admin/api/yclients/client-ids', methods=['GET', 'POST'])
    def refresh_branch():
        services, username, actor = context()
        data = request.get_json(silent=True) if request.method == 'POST' else request.args.to_dict()
        if not isinstance(data, dict) or set(data) - {'branch_id'}:
            raise ValueError('Выберите филиал')
        branch_id = data.get('branch_id', actor.get('branch_id'))
        if branch_id is None:
            raise ValueError('Выберите филиал')
        jobs = services['yclients_client_jobs']
        state = (jobs.start_batch(username, str(branch_id)) if request.method == 'POST'
                 else jobs.batch_status(username, str(branch_id)))
        return jsonify(status='success', sync=state)

    return bp
