"""Read/save installation settings; ordinary admins never receive settings data."""
from flask import Blueprint,Response,abort,current_app,jsonify,render_template,request,session
from system_settings import SystemSettings


def _webhook_status(settings):
    """Only the running worker can confirm provider registration for the active key."""
    from bot_webhooks import EXTENSION
    fields = {field['key']:field for field in settings['fields']}
    inboxes = current_app.extensions.get(EXTENSION, {})
    failures = current_app.extensions.get('gatebot_bot_startup_errors', {})
    statuses = {}
    for platform, prefix, secret in (('telegram','BOT','TELEGRAM_WEBHOOK_SECRET'), ('max','MAX_BOT','MAX_WEBHOOK_SECRET')):
        keys = {secret, 'BASE_URL', prefix+'_TOKEN', prefix+'_ENABLED', prefix+'_MODE'}
        failure = failures.get(platform)
        mode_pending = fields[prefix+'_ENABLED']['restart_required'] or fields[prefix+'_MODE']['restart_required']
        if platform == 'telegram':
            keys.add('TELEGRAM_WEBHOOK_URL')
        if not fields[prefix+'_ENABLED']['value']:
            state, message = 'disabled', 'Бот выключится после перезапуска.' if mode_pending else 'Бот выключен.'
        elif fields[prefix+'_MODE']['value'] != 'webhook':
            state, message = 'polling', 'Polling включится после перезапуска.' if mode_pending else 'Выбран polling.'
        elif keys.intersection(settings['restart_keys']):
            state, message = 'pending_restart', 'Новые настройки webhook вступят в силу после перезапуска.'
        elif failure:
            state, message = 'error', f"Ошибка подключения ({failure['error_code']}). {failure['action']}"
        elif getattr(inboxes.get(platform), 'registration_confirmed', False):
            state, message = 'registered', 'Регистрация webhook подтверждена.'
        else:
            state, message = 'unconfirmed', 'Ожидается подтверждение регистрации webhook.'
        statuses[platform] = {'state':state,'message':message}
    return statuses


def _settings_response(settings, **extra):
    settings['webhooks'] = _webhook_status(settings)
    response = jsonify(settings=settings, **extra)
    response.headers['Cache-Control'] = 'no-store'
    return response


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
        settings,username=context()
        return _settings_response(settings.public(username,include_development=request.args.get('edit_environment')=='development'))

    @bp.post('/admin/api/system-settings')
    def save():
        settings,username=context();payload=request.get_json(silent=True)
        if not isinstance(payload,dict):abort(400)
        return _settings_response(settings.save(username,payload),status='success')

    @bp.post('/admin/api/system-settings/rotate/<target>')
    def rotate(target):
        from runtime_control import restart_callback
        settings,username=context()
        updated=settings.rotate(username,target,request.get_json(silent=True))
        mode=_webhook_status(updated).get(target,{}).get('state')
        apply_now=target=='session' or mode not in {'disabled','polling'}
        callback=restart_callback() if apply_now else None
        if callback:
            message=('Ключ сохранён. GateBot перезапускается. Потребуется новый вход.' if target=='session' else
                     'Секрет сохранён. После перезапуска GateBot зарегистрирует webhook.')
        elif apply_now:
            message='Секрет сохранён. Перезапустите процесс GateBot для применения.'
        else:
            message='Секрет сохранён для следующего запуска бота в режиме webhook.'
        response=_settings_response(updated,message=message,restarting=bool(callback))
        if callback:
            response.call_on_close(callback)
        return response, 202 if callback else 200

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
        response.call_on_close(callback)
        return response, 202

    @bp.get('/admin/api/system-settings/compose.yaml')
    def export_yaml():
        from deployment_yaml import compose
        from modem_settings import read_modems
        settings, username = context()
        fields = {f['key']:f['value'] for f in settings.public(username)['fields'] if not f['secret']}
        document = compose(request.args, fields, read_modems(settings.identity.product))
        return Response(document, mimetype='text/plain', headers={'Cache-Control':'no-store'})

    @bp.post('/admin/api/system-settings/export')
    def export_json():
        from settings_transfer import export_settings
        settings, username = context()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            abort(400)
        response = jsonify(export_settings(settings, username, payload.get('password')))
        response.headers['Content-Disposition'] = 'attachment; filename="gatebot-settings.json"'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @bp.post('/admin/api/system-settings/import')
    def import_json():
        from settings_transfer import import_settings
        settings, username = context()
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            abort(400)
        result = import_settings(settings, username, payload.get('password'), payload.get('document'), payload.get('revision'))
        return jsonify(settings=result)

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
