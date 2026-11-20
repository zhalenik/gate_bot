"""Scoped administration of printable QR codes and SMS-verified gate pages."""
from io import BytesIO
import re
import secrets

from flask import Blueprint, abort, current_app, jsonify, render_template, request, session, Response
import qrcode
from qrcode.image.svg import SvgPathImage

from gate_qr_service import DEVICE_SECONDS, GateQRUnavailable
from identity_service import OtpRateLimit
from branch_hours import CLOSED_MESSAGE


def create_blueprint(provider):
    bp = Blueprint('gate_qr', __name__)

    def service():
        return provider()['gate_qr']

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            abort(400)
        return value

    def admin(branch_id):
        actor = provider()['auth'].get_current_admin()
        if not actor:
            abort(401)
        if not actor.get('is_super') and str(actor.get('branch_id')) != str(branch_id):
            abort(403)
        return session['admin_username']

    def browser():
        value = session.get('gate_qr_browser')
        if not isinstance(value, str) or len(value) < 32:
            value = secrets.token_urlsafe(32)
            session['gate_qr_browser'] = value
        return value

    def cookie_name(raw):
        return 'gatebot_qr_' + service().state(raw)['branch_id']

    @bp.errorhandler(ValueError)
    def invalid(error):
        if isinstance(error, OtpRateLimit):
            return jsonify(status='error', message=str(error), retry_after=error.retry_after), 429, {'Retry-After': str(error.retry_after)}
        if isinstance(error, GateQRUnavailable):
            if request.method == 'GET' and request.path.startswith('/join/'):
                return render_template('branch_join.html', client_page=True, unavailable=str(error)), 404
            if request.method == 'GET' and request.path.startswith('/gate/'):
                return render_template('gate_qr.html', client_page=True, unavailable=str(error), bootstrap={}), 404
            return jsonify(status='error', message=str(error)), 404
        return jsonify(status='error', message=str(error)), 400

    @bp.errorhandler(PermissionError)
    def forbidden(error):
        return jsonify(status='error', message=str(error),code='outside_working_hours' if str(error)==CLOSED_MESSAGE else 'access_denied'), 403

    @bp.route('/admin/api/branches/<int:branch_id>/gates/<gate_id>/qr', methods=['GET', 'POST'])
    def admin_qr(branch_id, gate_id):
        username = admin(branch_id)
        action = body().get('action', 'create') if request.method == 'POST' else None
        return jsonify(service().admin_view(username, branch_id, gate_id, action))

    @bp.route('/admin/api/branches/<int:branch_id>/registration-qr', methods=['GET', 'POST'])
    def registration_qr(branch_id):
        username = admin(branch_id)
        action = body().get('action', 'create') if request.method == 'POST' else None
        return jsonify(provider()['branch_join'].admin_view(username, branch_id, action))

    @bp.get('/admin/api/branches/<int:branch_id>/registration-qr.<kind>')
    @bp.get('/admin/api/branches/<int:branch_id>/gates/<gate_id>/qr.<kind>')
    def qr_image(branch_id, kind, gate_id=None):
        if kind not in {'svg', 'png'}:
            abort(404)
        username = admin(branch_id)
        data = (service().admin_view(username, branch_id, gate_id) if gate_id is not None
                else provider()['branch_join'].admin_view(username, branch_id))
        if not data['enabled']:
            abort(404)
        image = qrcode.make(data['url'], image_factory=SvgPathImage) if kind == 'svg' else qrcode.make(data['url'])
        output = BytesIO()
        image.save(output)
        response = Response(output.getvalue(), mimetype='image/svg+xml' if kind == 'svg' else 'image/png')
        filename = 'gate-qr' if gate_id is not None else 'branch-registration-qr'
        response.headers['Content-Disposition'] = f'attachment; filename="{filename}.{kind}"' if request.args.get('download') == '1' else 'inline'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.get('/join/<raw>')
    def join_page(raw):
        browser()
        return render_template('branch_join.html', client_page=True, branch_name=provider()['branch_join'].name(raw))

    @bp.post('/join/<raw>/code')
    def join_code(raw):
        result = provider()['branch_join'].code(raw, browser(), body().get('phone'), request.remote_addr or '')
        return jsonify(challenge_id=result['challenge_id'],ready=result['ready'],retry_after=result['retry_after'])

    @bp.post('/join/<raw>/verify')
    def join_verify(raw):
        data = body()
        provider()['branch_join'].verify(raw, browser(), data.get('challenge_id'), data.get('code'))
        return jsonify(verified=True)

    @bp.post('/join/<raw>/profile')
    def join_profile(raw):
        return jsonify(provider()['branch_join'].profile(raw, browser()))

    @bp.post('/join/<raw>/finish')
    def join_finish(raw):
        data = body()
        return jsonify(provider()['branch_join'].finish(raw, browser(), data.get('name'), data.get('email', '')))

    @bp.get('/gate/<raw>')
    def gate_page(raw):
        browser()
        state = service().state(raw, request.cookies.get(cookie_name(raw)))
        return render_template('gate_qr.html', client_page=True, unavailable=None, bootstrap={'raw': raw, **state})

    @bp.post('/gate/<raw>/state')
    def state(raw):
        data = service().state(raw, request.cookies.get(cookie_name(raw)))
        if data['state'] == 'phone_required':
            data['challenge'] = service().challenge_status(raw, browser())
        return jsonify(data)

    @bp.post('/gate/<raw>/code')
    def code(raw):
        return jsonify(service().request_code(raw, body().get('phone'), browser(), request.remote_addr or ''))

    @bp.post('/gate/<raw>/verify')
    def verify(raw):
        data = body()
        remember = data.get('remember', True)
        device = service().verify_code(raw, browser(), data.get('challenge_id'), data.get('code'), remember=remember)
        response = jsonify(service().state(raw, device))
        response.set_cookie(cookie_name(raw), device, max_age=DEVICE_SECONDS if remember else None, httponly=True, samesite='Lax',
                            secure=current_app.config.get('SESSION_COOKIE_SECURE', False), path='/')
        return response

    @bp.post('/gate/<raw>/forget')
    def forget(raw):
        name = cookie_name(raw)
        service().forget_device(raw, request.cookies.get(name))
        response = jsonify(status='success')
        response.delete_cookie(name, path='/', httponly=True, samesite='Lax', secure=current_app.config.get('SESSION_COOKIE_SECURE', False))
        return response

    @bp.post('/gate/<raw>/open')
    def open_gate(raw):
        data = body()
        # Requests may never choose a target, client or route.
        if set(data) != {'request_id'} or not isinstance(data['request_id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', data['request_id']):
            raise ValueError('Некорректный запрос открытия ворот.')
        result = service().open(raw, request.cookies.get(cookie_name(raw)), data['request_id'])
        return jsonify(result), result.get('http_status', 200)

    return bp
