"""Authenticated help and private support. Global application CSRF applies."""
from flask import Blueprint, abort, jsonify, render_template, request, session

from help_catalog import articles_for


def create_blueprint(provider):
    bp=Blueprint('help_support',__name__)

    def actor():
        services=provider();admin=services['auth'].get_current_admin()
        if not admin:abort(401)
        # Check the live identity as well as the browser session on every call.
        services['service'].web_admin(session['admin_username'])
        return services,session['admin_username'],admin

    def body():
        value=request.get_json(silent=True)
        if not isinstance(value,dict):abort(400)
        return value

    def branch(admin,data,required=False):
        value=data.get('branch_id') or admin.get('branch_id')
        if value is None:
            if required:raise ValueError('Выберите филиал')
            return None
        if not admin.get('is_super') and str(value)!=str(admin.get('branch_id')):
            raise PermissionError('Сначала выберите этот филиал в меню аккаунта')
        return str(value)

    def thread_scope(services,username,admin,identifier):
        result=services['support'].messages(username,identifier,limit=1)
        branch(admin,{'branch_id':result['thread']['branch_id']})

    def choices(services,admin):
        return [{'id':str(b.branch_id),'name':b.name} for b in services['branches'].get_all_branches()
                if admin.get('is_super') or str(b.branch_id)==str(admin.get('branch_id'))]

    @bp.errorhandler(ValueError)
    def invalid(error):return jsonify(status='error',message=str(error)),400

    @bp.errorhandler(PermissionError)
    def forbidden(error):return jsonify(status='error',message=str(error)),403

    @bp.get('/admin/help')
    def help_page():
        services,username,admin=actor();selected=branch(admin,request.args)
        if selected:
            with services['service'].product.connect() as db:services['service']._scope(db,username,selected)
        current=services['branches'].get_branch(selected) if selected else None
        return render_template('admin/help.html',admin_page=True,is_super=bool(admin.get('is_super')),
            help_articles=articles_for(bool(admin.get('is_super')),getattr(current,'yclients_enabled',True)))

    @bp.get('/admin/support')
    def support_page():
        services,username,admin=actor()
        return render_template('admin/support.html',admin_page=True,is_super=bool(admin.get('is_super')))

    @bp.get('/admin/api/support')
    def support_index():
        services,username,admin=actor()
        result=services['support'].list_threads(username,branch(admin,request.args),before=request.args.get('before'))
        return jsonify(**result,branches=choices(services,admin))

    @bp.get('/admin/api/support/threads/<int:identifier>')
    def messages(identifier):
        services,username,admin=actor();thread_scope(services,username,admin,identifier)
        return jsonify(services['support'].messages(username,identifier,after=request.args.get('after','0'),before=request.args.get('before')))

    @bp.post('/admin/api/support/messages')
    def send():
        services,username,admin=actor();data=body();identifier=data.get('thread_id')
        if identifier is not None:thread_scope(services,username,admin,identifier)
        selected=branch(admin,data,required=identifier is None)
        return jsonify(services['support'].send(username,data.get('text'),data.get('request_key'),branch_id=selected if identifier is None else None,thread_id=identifier,title=data.get('title')))

    @bp.post('/admin/api/support/threads/<int:identifier>/read')
    def read(identifier):
        services,username,admin=actor();thread_scope(services,username,admin,identifier)
        services['support'].mark_read(username,identifier,body().get('message_id'))
        return jsonify(status='success')

    @bp.post('/admin/api/support/threads/<int:identifier>/state')
    def state(identifier):
        services,username,admin=actor();thread_scope(services,username,admin,identifier)
        services['support'].set_state(username,identifier,body().get('state'))
        return jsonify(status='success')

    return bp
