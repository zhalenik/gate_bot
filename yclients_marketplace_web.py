"""Browser-bound marketplace hand-off and authenticated lifecycle callback."""
import secrets
from flask import Blueprint, abort, jsonify, redirect, render_template, request, session
from yclients_marketplace import MarketplaceService, MarketplaceError


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
        # Remove signed personal data from the address before rendering assets.
        return redirect('/yclients/marketplace', code=303)

    @bp.get('/yclients/marketplace')
    def page():
        marketplace, raw, browser, username, admin = current()
        return render_template('marketplace/connect.html', state=marketplace.view(raw,browser,username),
                               admin_page=bool(admin), is_super=bool(admin and admin.get('is_super')))

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

    @bp.post('/yclients/marketplace/password')
    def password():
        marketplace, raw, browser, _, _ = current()
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or set(body) - {'password', 'confirm'}:
            abort(400)
        try:
            username = marketplace.set_onboarding_password(raw, browser, body.get('password'), body.get('confirm'))
        except ValueError as error:
            raise MarketplaceError(str(error)) from None
        dependencies, _ = services()
        if not dependencies['auth'].login_admin(username, body.get('password')):
            raise MarketplaceError('Пароль сохранён. Войдите в кабинет с email и новым паролем.')
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


def registration_defaults(provider):
    """Optional form hints, never a grant of branch ownership or OTP bypass."""
    raw, browser = session.get('yc_marketplace_intent'), session.get('yc_marketplace_browser')
    if not raw or not browser:
        return {}
    dependencies = provider()
    marketplace = dependencies.get('marketplace') or MarketplaceService(dependencies['service'])
    try:
        state = marketplace.view(raw, browser)
    except MarketplaceError:
        return {}
    if state['expired']:
        return {}
    profile = state['profile']
    return {'branch_id': state['branches'][0]['id'], 'branch_name': profile.get('salon_name',''),
            'name': profile.get('name',''), 'phone': profile.get('phone',''), 'email':profile.get('email',''), 'marketplace': True}


def registration_context(provider):
    """The branch is server-selected from the browser hand-off, never form data.

    salon_id is not an ownership proof. OTP and super review remain required.
    """
    raw,browser=session.get('yc_marketplace_intent'),session.get('yc_marketplace_browser')
    if not raw and not browser:return None
    if not raw or not browser:raise MarketplaceError('Начните подключение из карточки GateBot в YCLIENTS')
    dependencies=provider()
    marketplace=dependencies.get('marketplace') or MarketplaceService(dependencies['service'])
    with marketplace.store.connect() as db:
        intent=marketplace._intent(db,raw,browser)
    return {'branch_id':intent['branches'][0],'connection_mode':'marketplace'}
