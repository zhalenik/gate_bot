"""Public recovery routes; the cookie receives only a restricted reset token."""
import secrets
from flask import Blueprint, abort, jsonify, render_template, request, session
from identity_service import OtpRateLimit
from password_recovery import PasswordRecovery


def create_blueprint(provider):
    bp=Blueprint('password_recovery',__name__)
    def service():return PasswordRecovery(provider()['service'])
    def browser():
        value=session.get('password_reset_browser')
        if not isinstance(value,str) or len(value)<32:
            value=secrets.token_urlsafe(32);session['password_reset_browser']=value
        return value
    def body():
        value=request.get_json(silent=True)
        if not isinstance(value,dict):abort(400)
        return value
    @bp.errorhandler(ValueError)
    def invalid(error):
        if isinstance(error,OtpRateLimit):return jsonify(message=str(error),retry_after=error.retry_after),429,{'Retry-After':str(error.retry_after)}
        return jsonify(message=str(error)),400
    @bp.get('/admin/recover')
    def recovery_page():
        session.pop('password_reset_token',None)
        browser()
        return render_template('identity/recovery.html')
    @bp.post('/admin/recover/code')
    def request_code():
        data=body();session.pop('password_reset_token',None)
        return jsonify(service().request_code(data.get('phone'),browser(),request.remote_addr or ''))
    @bp.post('/admin/recover/verify')
    def verify_code():
        data=body();token=service().verify_code(data.get('request_token'),data.get('code'),browser())
        for key in ('admin_logged_in','admin_username','admin_auth_version','active_branch_id'):session.pop(key,None)
        session['password_reset_token']=token
        return jsonify(status='verified')
    @bp.post('/admin/recover/complete')
    def complete():
        data=body();token=session.get('password_reset_token')
        if not token:raise ValueError('Сначала подтвердите телефон SMS-кодом')
        result=service().complete(token,browser(),data.get('password'),data.get('confirmation'))
        session.pop('password_reset_token',None)
        return jsonify(result)
    return bp
