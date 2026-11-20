"""Branch administrator's installable workspace. Every page uses current server-side scope."""
from pathlib import Path
import re

from flask import Blueprint, abort, jsonify, redirect, render_template, request, send_file, session

from branding import BrandingSettings
from identity_service import ADMIN_EVENTS


PAGES = {'clients':'admin/users.html','new-client':'admin/create-link.html','guest':'admin/temp-link.html',
         'notifications':'admin/notifications.html','settings':'identity/page.html','billing':'office/page.html',
         'messages':'office/page.html','help':'office/page.html'}


def create_blueprint(provider):
    bp = Blueprint('office', __name__,url_prefix='/office')

    @bp.get('/manifest.webmanifest')
    def manifest():
        brand = BrandingSettings(provider()['service']).public_view()
        response = jsonify(id='/office/',scope='/office/',start_url='/office/',lang='ru',name='GateBot · Кабинет филиала',
                short_name='Кабинет',display='standalone',background_color='#FFFFFF',theme_color='#EFF0E8',
                icons=[{'src':brand['icon192_url'],'sizes':'192x192','type':'image/png','purpose':'any'},
                       {'src':brand['icon512_url'],'sizes':'512x512','type':'image/png','purpose':'any'}],
                shortcuts=[{'name':'Сообщения','url':'/office/messages'},{'name':'Разовый пропуск','url':'/office/guest'}])
        response.mimetype='application/manifest+json'
        return response

    @bp.get('/sw.js')
    def worker():
        response = send_file(Path(__file__).parent/'static'/'office'/'sw.js', mimetype='application/javascript')
        response.headers['Service-Worker-Allowed']='/office/'
        return response

    @bp.before_request
    def check_role():
        if request.endpoint in {'office.manifest','office.worker'}:return
        services = provider()
        if not services['auth'].get_current_admin():
            if request.method=='GET':
                dest=request.view_args.get('section','clients')
                if dest in PAGES:
                    thread=request.args.get('thread','')
                    session['office_destination']='/office/'+dest+('?thread='+thread if dest=='messages' and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',thread) else '')
                return redirect('/office/login')
            return jsonify(message='Войдите в кабинет филиала.'),401
        with services['service'].product.connect() as db:
            services['pwa'].office.actor(db,session['admin_username'])

    @bp.errorhandler(PermissionError)
    def forbidden(error):
        if request.method=='GET':return render_template('office/denied.html'),403
        return jsonify(message=str(error)),403

    @bp.errorhandler(ValueError)
    def invalid(error):return jsonify(message=str(error)),400

    @bp.get('/')
    @bp.get('/<section>')
    def page(section='clients'):
        if section not in PAGES:abort(404)
        services = provider()
        identity = services['service']
        username=session['admin_username']
        admin=identity.web_admin(username)
        branch=services['branches'].get_branch(admin['branch_id'])
        if not branch:abort(403)
        options = {'office':True,'office_section':section,'admin_page':True,'is_super':False,'admin_branch_name':branch.name,
                   'admin_branch_id':int(branch.branch_id)}
        if section=='settings':
            prefs=identity.admin_preferences(username)
            options.update(title='Настройки уведомлений',kind='admin_preferences',branches=[],
                           bootstrap={'preferences':prefs,'events':[e for e in ADMIN_EVENTS if e in prefs['events']]})
        if section=='billing':
            resource=request.args.get('resource','calls')
            number=request.args.get('page',1,type=int)
            if resource not in {'calls','sms'} or not 1<=number<=100000:abort(400)
            billing=services['billing']
            options.update(view=billing.subscription_view(branch.branch_id),resource=resource,
                           details=billing.usage_details([str(branch.branch_id)],resource,page=number,page_size=20))
        return render_template(PAGES[section],**options)

    @bp.post('/push/<action>')
    def push(action):
        data=request.get_json(silent=True)
        if not isinstance(data,dict):abort(400)
        office=provider()['pwa'].office
        name=session['admin_username']
        if action=='state':return jsonify(office.settings(name,data.get('endpoint')))
        if action=='subscribe':
            session['office_push_device']=office.subscribe(name,data.get('subscription'),data.get('events'),data.get('consent'))
        elif action=='unsubscribe':
            office.disable(name,endpoint=data.get('endpoint'))
            session.pop('office_push_device',None)
        else:abort(404)
        return jsonify(saved=True)

    return bp
