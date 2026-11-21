"""Browser-bound marketplace hand-off and authenticated lifecycle callback."""
import secrets
from flask import Blueprint, abort, jsonify, redirect, render_template, request, session
from yclients_marketplace import MarketplaceService, MarketplaceError
from marketplace_frame import claim_first_party_link, in_marketplace_frame, issue_first_party_link


SESSION_KEYS = ('yc_marketplace_browser', 'yc_marketplace_intent')


def create_blueprint(provider):
    bp = Blueprint('yclients_marketplace', __name__)

    def services():
        dependencies = provider()
        return dependencies, dependencies.get('marketplace') or MarketplaceService(dependencies['service'])

    def current():
        dependencies, marketplace = services()
        admin = dependencies['auth'].get_current_admin()
        username = session.get('admin_username') if admin else None
        raw, browser = session.get('yc_marketplace_intent'), session.get('yc_marketplace_browser')
        if not raw or not browser:
            raise MarketplaceError('Начните подключение из карточки GateBot в YCLIENTS')
        return marketplace, raw, browser, username, admin

    @bp.errorhandler(ValueError)
    @bp.errorhandler(MarketplaceError)
    def invalid(error):
        if request.method == 'GET':
            return render_template('marketplace/error.html', message=str(error)), 400
        return jsonify(status='error', message=str(error)), 400

    @bp.errorhandler(PermissionError)
    def forbidden(error):
        return jsonify(status='error', message=str(error)), 403

    @bp.get('/yclients/marketplace/connect')
    def entry():
        _, marketplace = services()
        browser = session.get('yc_marketplace_browser') or secrets.token_urlsafe(32)
        raw = marketplace.begin(request.args, browser)
        session['yc_marketplace_browser'], session['yc_marketplace_intent'] = browser, raw
        if in_marketplace_frame(request):
            ticket = issue_first_party_link(marketplace, raw, browser)
            session['yc_frame_ticket'] = ticket
            return redirect('/yclients/marketplace?frame=start#' + ticket, code=303)
        # Remove signed personal data from the address before rendering assets.
        return redirect('/yclients/marketplace', code=303)

    @bp.get('/yclients/marketplace')
    def page():
        if request.args.get('frame') == 'start':
            return render_template('marketplace/frame_start.html', client_page=True)
        marketplace, raw, browser, username, admin = current()
        return render_template('marketplace/connect.html', state=marketplace.view(raw,browser,username),
                               frame_ticket=session.get('yc_frame_ticket') if in_marketplace_frame(request) else None,
                               admin_page=bool(admin), is_super=bool(admin and admin.get('is_super')))

    @bp.route('/yclients/marketplace/resume', methods=['GET', 'POST'])
    def resume():
        if request.method == 'GET':
            return render_template('marketplace/resume.html', client_page=True)
        _, marketplace = services()
        data = request.get_json(silent=True) or {}
        ticket = data.get('ticket') if isinstance(data, dict) else None
        browser = secrets.token_urlsafe(32)
        if not claim_first_party_link(marketplace, ticket, browser):
            raise MarketplaceError('Переход уже использован или истёк. Откройте подключение из YCLIENTS ещё раз.')
        session['yc_marketplace_browser'], session['yc_marketplace_intent'] = browser, ticket
        return jsonify(status='success')

    @bp.get('/yclients/marketplace/state')
    def state():
        marketplace, raw, browser, username, admin = current()
        return jsonify(marketplace.view(raw, browser, username))

    @bp.post('/yclients/marketplace/connect')
    def connect():
        marketplace, raw, browser, username, admin = current()
        if not admin:
            abort(401)
        body = request.get_json(silent=True)
        if not isinstance(body,dict) or set(body)-{'branch_id','reconcile'}:
            abort(400)
        return jsonify(marketplace.connect(raw, browser, username, body.get('branch_id'), reconcile=body.get('reconcile') is True))

    @bp.post('/yclients/marketplace/onboard')
    def onboard():
        marketplace, raw, browser, _, _ = current()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {'branch_id', 'reconcile'}:
            abort(400)
        return jsonify(marketplace.onboard(raw, browser, body.get('branch_id'),
                                          reconcile=body.get('reconcile') is True))

    @bp.post('/yclients/marketplace/code')
    def registration_code():
        from identity_service import OtpRateLimit
        marketplace, raw, browser, _, _ = current()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) not in ({'branch_id', 'phone', 'documents'},
                                                         {'branch_id', 'phone', 'documents', 'original_challenge', 'original_code'}):
            abort(400)
        if 'original_challenge' in body:
            marketplace.verify_original_phone(raw, browser, body['branch_id'], body['phone'],
                                              body['original_challenge'], body['original_code'])
        try:
            challenge = marketplace.request_registration_code(raw, browser, body['branch_id'], body['phone'], request.remote_addr or '',documents=body['documents'])
        except OtpRateLimit as exc:
            return jsonify(message=str(exc), retry_after=exc.retry_after), 429
        return jsonify({key: challenge[key] for key in ('challenge_id', 'ready', 'retry_after', 'phone_hint', 'step')})

    @bp.post('/yclients/marketplace/register')
    def register():
        marketplace, raw, browser, _, _ = current()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) != {'branch_id', 'profile', 'challenge', 'code', 'password', 'confirm', 'documents'}:
            abort(400)
        username = marketplace.complete_registration(raw, browser, body['branch_id'], body['profile'], body['challenge'],
                                                     body['code'], body['password'], body['confirm'],documents=body['documents'])
        if request.args.get('frame') == '1':
            return jsonify(status='success', redirect_url='/admin/integrations')
        dependencies, _ = services()
        if not dependencies['auth'].login_admin(username, body.get('password')):
            raise MarketplaceError('Регистрация завершена. Войдите в кабинет с вашим email и паролем.')
        return jsonify(status='success', redirect_url='/admin/integrations')

    @bp.post('/webhook/yclients/marketplace')
    def lifecycle():
        # CSRF-exempt server callback. Body token and application ID are checked
        # before any local mutation; raw payload/token never reaches audit logs.
        if request.content_length is not None and request.content_length > 4096:
            abort(413)
        _, marketplace = services()
        return jsonify(marketplace.disconnect(request.get_json(silent=True)))

    return bp
