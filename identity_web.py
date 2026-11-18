"""Identity/onboarding views. Provider lookups keep tenant checks current."""
from functools import wraps
import secrets
from flask import Blueprint, abort, jsonify, render_template, request, session
from storage import config
from identity_service import ADMIN_EVENTS, OtpRateLimit


def create_blueprint(provider):
    bp=Blueprint('identity',__name__)

    def services():return provider()
    def svc():return services()['service']
    def body():
        value=request.get_json(silent=True)
        if not isinstance(value,dict):abort(400)
        return value
    def actor():
        current=services()['auth'].get_current_admin()
        if not current:abort(401)
        return session['admin_username'],current
    def branch(data=None):
        username,admin=actor();value=(data or request.args).get('branch_id') or admin.get('branch_id')
        if value is None:raise ValueError('Выберите филиал')
        return str(value)
    def choices(admin):
        return [{'id':str(b.branch_id),'name':b.name,'yclients_enabled':getattr(b,'yclients_enabled',True)} for b in services()['branches'].get_all_branches() if admin.get('is_super') or str(b.branch_id) in (admin.get('branch_ids') or [str(admin.get('branch_id'))])]
    def page(kind,title,**data):
        admin=data.pop('admin',None)
        return render_template('identity/page.html',kind=kind,title=title,bootstrap=data,
            admin_page=admin is not None,is_super=bool(admin and admin.get('is_super')),branches=choices(admin) if admin else [])

    @bp.errorhandler(ValueError)
    def invalid(error):
        if isinstance(error, OtpRateLimit):
            return jsonify(status='error',message=str(error),retry_after=error.retry_after),429,{'Retry-After':str(error.retry_after)}
        return jsonify(status='error',message=str(error)),400
    @bp.errorhandler(PermissionError)
    def forbidden(error):return jsonify(status='error',message=str(error)),403

    @bp.get('/admin/invitations')
    def invitations_page():
        username,admin=actor();return page('invitations','Администраторы и приглашения',admin=admin)

    @bp.post('/admin/invitations/new')
    def invitation_create():
        username,admin=actor();data=body()
        result=svc().invite(username,branch(data),phone=data.get('phone'),email=data.get('email',''),client_token=data.get('client_token'),channel=data.get('channel','sms'))
        return jsonify(status='success',**result)

    @bp.get('/admin/api/identity/invitations')
    def invitation_list():
        username,admin=actor();return jsonify(invitations=svc().list_invitations(username,branch()))

    @bp.get('/admin/api/identity/admins')
    def admin_list():
        username,admin=actor();return jsonify(admins=svc().list_admins(username,branch()))

    @bp.post('/admin/api/identity/invitations/<identifier>/revoke')
    def invitation_revoke(identifier):
        username,admin=actor();return jsonify(status='success',revoked=svc().revoke_invitation(username,identifier))

    @bp.get('/invite/<token>')
    def invitation_page(token):return page('accept','Приглашение в GateBot',token=token,**svc().invitation_info(token))

    @bp.post('/invite/<token>/code')
    def invitation_code(token):return jsonify(svc().request_otp(token,body().get('phone'),request.remote_addr or ''))

    @bp.post('/invite/<token>/code-status')
    def invitation_code_status(token):return jsonify(svc().otp_status(token))

    @bp.post('/invite/<token>/accept')
    def invitation_accept(token):
        data=body();result=svc().accept_invitation(token,data.get('challenge_id'),data.get('code'),data.get('name'),data.get('username'),data.get('password'),data.get('existing_password'))
        return jsonify(status='success',**result)

    @bp.get('/register-branch')
    def registration_page():
        from yclients_marketplace_web import registration_defaults, SESSION_KEYS
        if request.args.get('mode')=='direct':
            for key in SESSION_KEYS:session.pop(key,None)
        return page('register','Подключить филиал',initial=registration_defaults(provider))

    @bp.post('/register-branch')
    def registration_create():
        from yclients_marketplace_web import registration_context
        data=body();context=registration_context(provider)
        if context:
            if data.get('branch_id') not in (None,'',context['branch_id'],int(context['branch_id'])):
                raise ValueError('Филиал выбран в YCLIENTS. Начните новый переход для другого филиала')
            if data.get('yclients_enabled',True) not in (True,'true'):raise ValueError('Это регистрация через маркетплейс YCLIENTS')
        return jsonify(svc().register_branch(context['branch_id'] if context else data.get('branch_id'),data.get('branch_name'),data.get('name'),data.get('phone'),yclients_enabled=True if context else data.get('yclients_enabled',True),connection_mode='marketplace' if context else 'direct'))

    @bp.post('/register-branch/code')
    def registration_code():
        data=body();return jsonify(svc().request_otp(data.get('registration_token'),ip=request.remote_addr or '',registration=True))

    @bp.post('/register-branch/code-status')
    def registration_code_status():return jsonify(svc().otp_status(body().get('registration_token'),registration=True))

    @bp.post('/register-branch/complete')
    def registration_complete():
        from yclients_marketplace_web import registration_context
        data=body()
        with svc().product.connect() as db:
            registration=svc()._registration(db,data.get('registration_token'))
            if registration['connection_mode']=='marketplace':
                context=registration_context(provider)
                if not context or context['branch_id']!=registration['branch_id']:
                    raise ValueError('Продолжите регистрацию из исходного перехода YCLIENTS')
                if data.get('branch_id') not in (None,'',context['branch_id'],int(context['branch_id'])):
                    raise ValueError('Филиал выбран в YCLIENTS')
                data.update(branch_id=context['branch_id'],yclients_enabled=True)
            result=svc().complete_registration(data.get('registration_token'),data.get('challenge_id'),data.get('code'),data.get('username'),data.get('password'),billing=services().get('billing'),details=data)
        return jsonify(result)

    @bp.post('/admin/api/identity/branches/<branch_id>/activate')
    def activate(branch_id):
        username,admin=actor();billing=services().get('billing')
        if billing is None:raise ValueError('Сервис подписок не настроен')
        return jsonify(svc().activate_branch(username,branch_id,billing))

    @bp.get('/preferences/<token>')
    def preferences_page(token):return page('preferences','Уведомления',token=token,preferences=svc().prefs_for_token(token))

    @bp.get('/preferences/<token>/status')
    def preferences_status(token):
        preferences=svc().prefs_for_token(token)
        csrf=session.get('csrf_token')
        if not isinstance(csrf,str) or not csrf:
            csrf=secrets.token_urlsafe(32);session['csrf_token']=csrf
        return jsonify(preferences=preferences,csrf_token=csrf)

    @bp.post('/preferences/<token>')
    def preferences_save(token):
        current=services()['auth'].get_current_admin()
        username=session.get('admin_username') if current else None
        return jsonify(status='success',preferences=svc().save_prefs_for_token(token,body(),actor_username=username))

    @bp.post('/preferences/<token>/verify-email')
    def email_request(token):return jsonify(delivery=svc().verify_email_request(token))

    @bp.post('/preferences/<token>/bot-link')
    def client_bot_link(token):return jsonify(link=svc().client_bot_link(token,body().get('platform')))

    @bp.get('/verify-email/<token>')
    def email_page(token):return page('email_verify','Подтверждение email',token=token)

    @bp.post('/verify-email/<token>')
    def email_verify(token):svc().verify_email(token);return jsonify(status='success')

    @bp.get('/admin/notification-settings')
    def admin_preferences_page():
        username,admin=actor();return page('admin_preferences','Мои уведомления',admin=admin,preferences=svc().admin_preferences(username),events=ADMIN_EVENTS)

    @bp.post('/admin/api/identity/notification-settings')
    def admin_preferences_save():
        username,admin=actor();return jsonify(status='success',preferences=svc().save_admin_preferences(username,body()))

    @bp.get('/admin/api/identity/notification-settings')
    def admin_preferences_status():
        username,admin=actor();return jsonify(preferences=svc().admin_preferences(username))

    @bp.post('/admin/api/identity/email')
    def admin_email():
        username,admin=actor();data=body()
        result=svc().admin_email(username,data.get('email',''),verify=data.get('verify') is True)
        return jsonify(status='success',delivery=result,preferences=svc().admin_preferences(username))

    @bp.post('/admin/api/identity/bot-link')
    def admin_bot_link():
        from urllib.parse import quote
        username,admin=actor();platform=body().get('platform');ticket=svc().issue_bind_ticket(username,platform)
        name=config.BOT_USERNAME if platform=='telegram' else config.MAX_BOT_USERNAME
        if not name:raise ValueError('Бот не настроен')
        return jsonify(link=('https://t.me/' if platform=='telegram' else 'https://max.ru/')+quote(name.lstrip('@'))+'?start='+quote(ticket))

    @bp.post('/admin/api/identity/branch')
    def choose_branch():
        username,admin=actor();data=body();current=svc().web_admin(username);selected=str(data.get('branch_id'))
        if not current.get('is_super') and selected not in current['branch_ids']:abort(403)
        session['active_branch_id']=selected;return jsonify(status='success')

    @bp.post('/admin/api/identity/admins/revoke')
    def revoke_admin():
        username,admin=actor();data=body();svc().revoke_admin(username,data.get('person_id'),branch(data));return jsonify(status='success')

    @bp.get('/admin/integrations')
    def integrations_page():
        username,admin=actor()
        allowed={str(b.branch_id) for b in services()['branches'].get_all_branches() if getattr(b,'yclients_enabled',True)}
        branches=[b for b in choices(admin) if b['id'] in allowed]
        return render_template('identity/page.html',kind='integrations',title='Подключения филиала',bootstrap={},admin_page=True,is_super=bool(admin.get('is_super')),branches=branches)

    @bp.get('/admin/api/identity/integrations')
    def integration_status():
        username,admin=actor();selected=branch()
        integrations=svc().integration_status(username,selected)
        return jsonify(integrations=integrations)

    @bp.post('/admin/api/identity/integrations/<kind>')
    def integration_save(kind):
        username,admin=actor();data=body();selected=branch(data)
        svc().integration_status(username,selected)
        if kind=='yclients' and admin.get('is_super') is not True:raise PermissionError('Подключите YCLIENTS через логин и пароль или маркетплейс')
        return jsonify(svc().save_integration(username,selected,kind,data.get('credentials')))

    @bp.post('/admin/api/identity/integrations/<kind>/check')
    def integration_check(kind):
        username,admin=actor();selected=branch(body());svc().integration_status(username,selected)
        if kind!='yclients':abort(404)
        credentials=svc().integration_credentials(selected,kind)
        if not credentials:raise ValueError('Сначала сохраните подключение')
        checker=services().get('check_yclients')
        if kind=='yclients' and checker:
            result=checker(selected,credentials)
            svc().record_integration_check(username,selected,kind,result,credentials)
            return jsonify(result)
        raise ValueError('Проверка подключения недоступна')

    @bp.post('/admin/api/identity/integrations/yclients/authorize')
    def yclients_authorize():
        import hashlib
        import time
        username,admin=actor();data=body();selected=branch(data)
        status=svc().integration_status(username,selected)
        if status.get('yclients',{}).get('manual_allowed') is False:
            raise PermissionError('Этот филиал подключается через маркетплейс YCLIENTS; логин и пароль не требуются')
        authorizer=services().get('authorize_yclients')
        if not authorizer:
            from yclients_connection import YClientsConnector
            authorizer=YClientsConnector().authorize
        login=data.get('login');login_hash=hashlib.sha256(str(login).strip().encode()).hexdigest()
        challenge=None
        if data.get('code'):
            pending=session.get('yclients_connection',{})
            if pending.get('branch_id')!=selected or pending.get('login_hash')!=login_hash or pending.get('expires',0)<=time.time():raise ValueError('Запрос двухфакторной защиты истёк. Начните подключение заново')
            challenge=pending.get('challenge')
        result=authorizer(login,data.get('password'),data.get('code'),challenge)
        if result.get('requires_2fa'):
            session['yclients_connection']={'branch_id':selected,'login_hash':login_hash,'challenge':result['challenge'],'expires':time.time()+300}
        else:session.pop('yclients_connection',None)
        if result.get('user_token'):
            credentials={'user_token':result['user_token']}
            checker=services().get('check_yclients')
            if not checker:
                from yclients_connection import YClientsConnector
                checker=YClientsConnector().check_permissions
            check=checker(selected,credentials)
            if check.get('connected') is not True:
                svc().record_integration_check(username,selected,'yclients',check)
                return jsonify(status='error',requires_2fa=False,configured=False,check=check,message=check.get('message','Недостаточно прав пользователя YCLIENTS')),400
            svc().save_integration(username,selected,'yclients',credentials,verified_direct=True,account=result.get('account'))
            svc().record_integration_check(username,selected,'yclients',check,credentials)
        return jsonify(status='success',requires_2fa=bool(result.get('requires_2fa')),configured=bool(result.get('user_token')))
    return bp
