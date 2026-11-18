"""Scoped weekly access settings. No pass or notification writes."""
from flask import Blueprint,abort,jsonify,request,session
from branch_hours import BranchHoursService


def create_blueprint(provider):
    bp=Blueprint('branch_hours',__name__)

    def context():
        services=provider()
        if not services['auth'].get_current_admin():abort(401)
        return BranchHoursService(services['service']),session['admin_username']

    @bp.errorhandler(ValueError)
    def invalid(error):return jsonify(status='error',message=str(error)),400

    @bp.errorhandler(PermissionError)
    def forbidden(error):return jsonify(status='error',message=str(error)),403

    @bp.route('/admin/api/branches/<int:branch_id>/access-hours',methods=['GET','POST'])
    def settings(branch_id):
        service,username=context()
        if request.method=='GET':return jsonify(service.read(username,branch_id))
        body=request.get_json(silent=True)
        if not isinstance(body,dict) or set(body)!={'schedule'}:abort(400)
        return jsonify(service.save(username,branch_id,body['schedule']))

    return bp
