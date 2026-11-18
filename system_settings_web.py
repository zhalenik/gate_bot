"""Read/save installation settings; ordinary admins never receive settings data."""
from flask import Blueprint,abort,jsonify,render_template,request,session
from system_settings import SystemSettings


def create_blueprint(provider):
    bp=Blueprint('system_settings',__name__)

    def context():
        services=provider();admin=services['auth'].get_current_admin()
        if not admin:abort(401)
        if admin.get('is_super') is not True:abort(403)
        return SystemSettings(services['service']),session['admin_username']

    @bp.errorhandler(ValueError)
    def invalid(error):return jsonify(status='error',message=str(error)),400

    @bp.errorhandler(PermissionError)
    def forbidden(error):return jsonify(status='error',message='Системные настройки доступны только суперадминистратору'),403

    @bp.errorhandler(RuntimeError)
    def unavailable(error):return jsonify(status='error',message='Системные настройки недоступны. Проверьте резервную копию файла настроек и ключа.'),503

    @bp.get('/admin/system-settings')
    def page():
        settings,username=context();settings.public(username)
        return render_template('admin/system-settings.html',admin_page=True,is_super=True)

    @bp.get('/admin/api/system-settings')
    def read():
        settings,username=context();response=jsonify(settings=settings.public(username));response.headers['Cache-Control']='no-store';return response

    @bp.post('/admin/api/system-settings')
    def save():
        settings,username=context();payload=request.get_json(silent=True)
        if not isinstance(payload,dict):abort(400)
        response=jsonify(status='success',settings=settings.save(username,payload));response.headers['Cache-Control']='no-store';return response

    return bp
