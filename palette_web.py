"""Superadmin palette editor and a public, color-only stylesheet."""
import hashlib

from flask import Blueprint, abort, jsonify, render_template, request, Response, session

from palette_settings import PaletteConflict, PaletteSettings


def create_blueprint(provider):
    bp = Blueprint('palette', __name__)

    def context():
        services = provider()
        actor = services['auth'].get_current_admin()
        if not actor:
            abort(401)
        if actor.get('is_super') is not True:
            abort(403)
        return PaletteSettings(services['service']), session['admin_username']

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            abort(400)
        return data

    @bp.errorhandler(PaletteConflict)
    def conflict(error):
        return jsonify(status='conflict', message=str(error), revision=error.revision,
                       catalog_version=error.catalog_version), 409

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(status='error', message=str(error)), 400

    @bp.errorhandler(PermissionError)
    def denied(error):
        return jsonify(status='error', message='Цвета интерфейса настраивает только суперадминистратор'), 403

    @bp.get('/admin/palette')
    def page():
        settings, username = context()
        settings.public(username)
        return render_template('admin/palette.html', admin_page=True, is_super=True,
                               palette_defaults=request.args.get('defaults') == '1')

    @bp.get('/admin/api/palette')
    def read():
        settings, username = context()
        return jsonify(settings.public(username))

    @bp.post('/admin/api/palette')
    def save():
        settings, username = context()
        return jsonify(status='success', **settings.save(username, body()))

    @bp.post('/admin/api/palette/reset')
    def reset():
        settings, username = context()
        return jsonify(status='success', **settings.reset(username, body()))

    @bp.get('/ui/palette.css')
    def public_styles():
        css = PaletteSettings(provider()['service']).stylesheet()
        response = Response(css, content_type='text/css; charset=utf-8')
        response.set_etag(hashlib.sha256(css.encode()).hexdigest())
        response.headers['Cache-Control'] = 'no-cache'
        return response.make_conditional(request)

    return bp
