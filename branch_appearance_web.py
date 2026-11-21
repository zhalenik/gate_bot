import json
import re

from flask import Blueprint, Response, abort, jsonify, render_template, request, session
from branch_appearance import ASSETS, FONTS, GLOBAL, client_colors
from branch_logos import MAX_UPLOAD_BYTES


def css_values(theme):
    settings, assets = theme['settings'],theme['assets']
    palette = client_colors()
    values = {palette[key]['css_var']:value for key,value in settings['colors'].items() if key in palette}
    values.update({'--client-font':FONTS[settings['font']], '--client-banner-bg':settings['banner_bg'],
                   '--client-banner-fg':settings['banner_color'], '--client-banner-size':str(settings['banner_size'])+'px',
                   '--client-banner-align':settings['banner_align'],'--client-button-align':settings['button_align'],
                   '--client-title-align':settings['title_align'],
                   '--client-logo-align':{'left':'flex-start','center':'center','right':'flex-end'}[settings['logo_align']]})
    if settings['button_gap'] is not None:
        values['--client-button-gap']=str(settings['button_gap'])+'px'
    if settings['radius'] is not None:
        values['--client-radius']=str(settings['radius'])+'px'
    if settings['font_size'] is not None:
        values['--client-unit']=str(settings['font_size'])+'px'
    if 'background' in assets:
        values['--client-wallpaper']='url('+assets['background']+')'
    if 'card_background' in assets:
        values['--client-card-image']='url('+assets['card_background']+')'
    return values


def create_blueprint(provider):
    bp = Blueprint('branch_appearance', __name__)
    path='/admin/api/branches/<int:branch_id>/appearance'

    def admin():
        services=provider()
        if not services['auth'].get_current_admin():
            abort(401)
        return services['appearance'],session['admin_username']

    @bp.errorhandler(PermissionError)
    def forbidden(error):
        return jsonify(message=str(error)),403

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(message=str(error)),400

    @bp.get('/admin/api/palette/client-appearance',defaults={'branch_id':GLOBAL})
    @bp.get(path)
    def editor(branch_id):
        service, username = admin()
        return jsonify(service.editor(username,branch_id))

    @bp.post('/admin/api/palette/client-appearance',defaults={'branch_id':GLOBAL})
    @bp.post(path)
    def save(branch_id):
        service, username = admin()
        with service.store.connect() as db:
            service.authorize(db,username,branch_id)
        request.max_content_length = len(ASSETS)*MAX_UPLOAD_BYTES+65536
        if request.mimetype!='multipart/form-data':
            abort(400)
        try:
            data=json.loads(request.form['appearance'])
        except (KeyError, ValueError):
            abort(400)
        required={'settings','revision','remove'}
        if not isinstance(data,dict) or not required<=data.keys()<=required|{'base_revision'} or request.files.keys()-ASSETS:
            abort(400)
        files={kind:upload.stream.read(MAX_UPLOAD_BYTES+1) for kind,upload in request.files.items()}
        return jsonify(service.save(username,branch_id,files=files,**data))

    @bp.get('/admin/palette/client-appearance-preview',defaults={'branch_id':GLOBAL})
    @bp.get('/admin/branches/<int:branch_id>/appearance-preview')
    def preview(branch_id):
        service,username=admin()
        with service.store.connect() as db:
            service.authorize(db,username,branch_id)
            view=service._view(db,branch_id)
        branch=provider()['branches'].get_branch(branch_id) if branch_id!=GLOBAL else None
        if branch_id!=GLOBAL and not branch:
            abort(404)
        buttons=[{'id':str(i),'type':'gate','label':b.label} for i,b in enumerate(branch.buttons) if b.type=='gate'] if branch else []
        if not buttons:
            buttons=[{'id':'entry','type':'gate','label':'Открыть въезд'},{'id':'exit','type':'gate','label':'Открыть выезд'}]
        return render_template('appearance_preview.html', client_page=True, appearance_preview=True,
                               client_appearance=view, client_styles=css_values(view),
                               user_name='Анна',branch_name=branch.name if branch else 'Ваш филиал',branch_logo=branch.logo if branch else None,
                               buttons=buttons,
                               telegram_url=None,max_url=None,token=None)

    @bp.get('/media/client-appearance/<branch_id>/<filename>')
    @bp.get('/media/client-icon-source/<branch_id>/<filename>',defaults={'legacy':True})
    def asset(branch_id,filename,legacy=False):
        if branch_id!=GLOBAL and not re.fullmatch(r'[0-9]+',branch_id):
            abort(404)
        if not re.fullmatch(r'[a-f0-9]{64}\.(png|svg)',filename):
            abort(404)
        content=provider()['appearance'].asset(branch_id,filename,legacy)
        if not content:
            abort(404)
        response=Response(content,mimetype='image/svg+xml' if filename.endswith('.svg') else 'image/png')
        response.headers['Content-Security-Policy']="default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return response
    return bp
