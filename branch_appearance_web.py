import json
import re

from flask import Blueprint, Response, abort, jsonify, render_template, request, session
from branch_appearance import ASSETS, FONTS, client_colors
from branch_logos import MAX_UPLOAD_BYTES


def css_values(theme):
    settings, assets = theme['settings'],theme['assets']
    palette = client_colors()
    values = {palette[key]['css_var']:value for key,value in settings['colors'].items() if key in palette}
    values.update({'--client-font':FONTS[settings['font']], '--client-banner-bg':settings['banner_bg'],
                   '--client-banner-fg':settings['banner_color'], '--client-banner-size':str(settings['banner_size'])+'px',
                   '--client-banner-align':settings['banner_align']})
    if settings['radius'] is not None:
        values['--client-radius']=str(settings['radius'])+'px'
    if settings['font_size'] is not None:
        values['--client-unit']=str(settings['font_size'])+'px'
    if 'background' in assets:
        values['--client-wallpaper']='url('+assets['background']+')'
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

    @bp.get(path)
    def editor(branch_id):
        service, username = admin()
        return jsonify(service.editor(username,branch_id))

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
        if not isinstance(data,dict) or set(data)!={'settings','revision','remove'} or request.files.keys()-ASSETS:
            abort(400)
        files={kind:upload.stream.read(MAX_UPLOAD_BYTES+1) for kind,upload in request.files.items()}
        return jsonify(service.save(username,branch_id,files=files,**data))

    @bp.get('/admin/branches/<int:branch_id>/appearance-preview')
    def preview(branch_id):
        service,username=admin()
        with service.store.connect() as db:
            service.authorize(db,username,branch_id)
            view=service._view(db,branch_id)
        branch=provider()['branches'].get_branch(branch_id)
        if not branch:
            abort(404)
        return render_template('appearance_preview.html', client_page=True, appearance_preview=True,
                               client_appearance=view, client_styles=css_values(view),
                               user_name='Анна',branch_name=branch.name,branch_logo=branch.logo,
                               buttons=[{'id':'sample','type':'gate','label':next((b.label for b in branch.buttons if b.type=='gate'),'Открыть ворота')}],
                               telegram_url=None,max_url=None,token=None)

    @bp.get('/media/client-appearance/<int:branch_id>/<filename>')
    def asset(branch_id,filename):
        if not re.fullmatch(r'[a-f0-9]{64}\.(png|svg)',filename):
            abort(404)
        content=provider()['appearance'].asset(branch_id,filename)
        if not content:
            abort(404)
        response=Response(content,mimetype='image/svg+xml' if filename.endswith('.svg') else 'image/png')
        response.headers['Content-Security-Policy']="default-src 'none'; style-src 'unsafe-inline'; sandbox"
        return response
    return bp
