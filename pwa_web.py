"""Client-token Web Push endpoints; application-wide CSRF remains mandatory."""
import secrets
from flask import Blueprint, abort, jsonify, request, session


def create_blueprint(provider):
    bp = Blueprint('pwa_push', __name__)

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(status='error', message=str(error)), 400

    @bp.errorhandler(PermissionError)
    def forbidden(error):
        return jsonify(status='error', message=str(error)), 403

    def result(value):
        csrf = session.get('csrf_token')
        if not isinstance(csrf, str) or len(csrf) < 32:
            csrf = secrets.token_urlsafe(32)
            session['csrf_token'] = csrf
        return jsonify(status='success', csrf_token=csrf, **value)

    def body(fields):
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != set(fields):
            abort(400)
        return value

    @bp.get('/pwa/<token>/push/status')
    def status(token):
        return result(provider()['pwa'].status(token))

    @bp.post('/pwa/<token>/push/subscribe')
    def subscribe(token):
        data = body(('subscription','consent'))
        return result(provider()['pwa'].subscribe(token, data['subscription'], consent=data['consent']))

    @bp.post('/pwa/<token>/push/unsubscribe')
    def unsubscribe(token):
        data = body(('endpoint',))
        return result(provider()['pwa'].unsubscribe(token, data['endpoint']))

    return bp
