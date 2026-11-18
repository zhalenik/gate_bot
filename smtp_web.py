"""SMTP management is installation-wide and available only to the superadmin."""
from flask import Blueprint, abort, jsonify, render_template, request, session
from smtp_settings import SmtpSettings


def create_blueprint(provider):
    bp = Blueprint('smtp_settings', __name__)

    def context():
        services = provider()
        admin = services['auth'].get_current_admin()
        if not admin:
            abort(401)
        if admin.get('is_super') is not True:
            abort(403)
        return SmtpSettings(services['service']), session['admin_username']

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(status='error', message=str(error)), 400

    @bp.errorhandler(PermissionError)
    def denied(error):
        return jsonify(status='error', message='SMTP настраивает только суперадминистратор'), 403

    @bp.get('/admin/smtp')
    def page():
        settings, username = context()
        settings.public(username)
        return render_template('admin/smtp.html', admin_page=True, is_super=True)

    @bp.get('/admin/api/smtp')
    def read():
        settings, username = context()
        return jsonify(settings=settings.public(username))

    @bp.post('/admin/api/smtp')
    def save():
        settings, username = context()
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400)
        return jsonify(status='success', settings=settings.save(username, data))

    @bp.post('/admin/api/smtp/check')
    def check():
        settings, username = context()
        result = settings.check(username)
        return jsonify(result), 200 if result['status'] == 'success' else 502

    return bp
