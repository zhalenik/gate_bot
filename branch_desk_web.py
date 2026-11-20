"""Branch inbox and guest handoffs. Never return a pass owner's bearer to a guest."""
from flask import Blueprint, abort, jsonify, redirect, render_template, request, session


def create_blueprint(provider):
    bp = Blueprint('branch_desk', __name__)

    def actor():
        admin = provider()['auth'].get_current_admin()
        if not admin:
            abort(401)
        return session['admin_username']

    def desk():
        return provider()['desk']

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value,dict):
            abort(400)
        return value

    @bp.errorhandler(ValueError)
    @bp.errorhandler(PermissionError)
    def rejected(error):
        status = 403 if isinstance(error, PermissionError) else 400
        if request.endpoint == 'branch_desk.shared':
            return render_template('shared_gate.html', error=str(error)),status
        return jsonify(message=str(error), status='error'),status

    @bp.route('/api/pass/<token>/desk', methods=['GET','POST'])
    def client(token):
        if request.method == 'POST':
            data = body()
            desk().client_send(token, data.get('kind'), data.get('body'), data.get('key'))
        return jsonify(desk().client_view(token))

    @bp.post('/api/pass/<token>/share')
    def share(token):
        return jsonify(path=desk().share(token,body().get('gate_id')))

    @bp.route('/shared/<raw>', methods=['GET','POST'])
    def shared(raw):
        view = desk().shared(raw)
        if request.method == 'GET':
            return render_template('shared_gate.html', **view)
        result = desk().shared(raw,provider()['access'])
        status = result.get('http_status',200)
        return render_template('shared_gate.html', **view, result=result),status

    @bp.get('/admin/client-messages')
    def inbox_page():
        if not provider()['auth'].get_current_admin():
            return redirect('/admin/login')
        admin = provider()['service'].web_admin(actor())
        return render_template('branch_messages.html', is_super=admin.get('is_super'),
                               admin_branch_name=provider()['branches'].get_branch_name(admin.get('branch_id')))

    @bp.get('/admin/api/desk/threads')
    def threads():
        return jsonify(items=desk().threads(actor(),request.args.get('branch_id')))

    @bp.get('/admin/api/desk/threads/<int:identifier>')
    def thread(identifier):
        return jsonify(desk().admin_view(actor(),identifier))

    @bp.post('/admin/api/desk/threads/<int:identifier>/reply')
    def reply(identifier):
        data=body()
        desk().reply(actor(),identifier,data.get('body'),data.get('key'))
        return jsonify(desk().admin_view(actor(),identifier))

    @bp.post('/admin/api/desk/threads/<int:identifier>/approve/<int:message>')
    def approve(identifier,message):
        desk().approve(actor(),identifier,message)
        return jsonify(desk().admin_view(actor(),identifier))

    return bp
