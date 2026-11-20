"""Read/save installation settings; ordinary admins never receive settings data."""
from flask import Blueprint,Response,abort,jsonify,render_template,request,session
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
    def forbidden(error):return jsonify(status='error',message='Нет доступа к системным настройкам'),403

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

    @bp.post('/admin/api/system-settings/restart')
    def restart():
        from runtime_control import restart_callback
        settings, username = context()
        current = settings.public(username)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or data.get('revision') != current['revision'] or current['invalid_keys']:
            return jsonify(message='Проверьте и сохраните актуальные настройки'), 409
        callback = restart_callback()
        if callback is None:
            return jsonify(message='Настройки сохранены. Для перезапуска запустите сервис через run.py.'), 409
        response = jsonify(message='Настройки сохранены. Сервис перезапускается.')
        response.headers['Connection'] = 'close'
        response.call_on_close(callback)
        return response, 202

    @bp.get('/admin/api/system-settings/compose.yaml')
    def export_yaml():
        from deployment_yaml import compose
        from modem_settings import read_modems
        settings, username = context()
        fields = {f['key']:f['value'] for f in settings.public(username)['fields'] if not f['secret']}
        document = compose(request.args, fields, read_modems(settings.identity.product))
        return Response(document, mimetype='application/yaml', headers={
            'Content-Disposition':'attachment; filename="gatebot.yaml"', 'Cache-Control':'no-store'})

    @bp.get('/admin/api/system-settings/modems')
    def modems():
        from modem_settings import inventory
        from sim800c import _get_queue
        context()
        saved = inventory(provider()['service'].product)
        queue = _get_queue()
        applied = [row for row, driver in queue.devices.values()]
        response = jsonify(**saved, active=applied, devices=queue.public_devices(),
                           online=queue.monitor_started and queue.enabled and not queue._closed)
        response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.post('/admin/api/system-settings/modems')
    def update_modems():
        from modem_settings import save_modems
        context()
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            abort(400)
        return jsonify(save_modems(provider()['service'].product, payload.get('revision'), payload.get('modems')))

    @bp.post('/admin/api/system-settings/modems/<key>/probe')
    def probe_modem(key):
        from sim800c import _get_queue
        context()
        return jsonify(result=_get_queue().probe(key))

    return bp
