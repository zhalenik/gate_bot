"""Subscription/statistics HTTP surfaces. Every read and mutation checks current scope."""
import json
import csv
import io
import os
from functools import wraps
from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request, session

from analytics_service import AnalyticsService
from billing_service import BillingService


def create_blueprint(admin_auth=None, branch_manager=None, link_manager=None, render_admin=None, *, provider=None):
    bp=Blueprint('product',__name__)

    def service(name):
        supplied=provider() if provider else {}
        if name in supplied:return supplied[name]
        defaults={'auth':admin_auth,'branches':branch_manager,'links':link_manager}
        if name=='billing':return BillingService()
        if name=='analytics':return AnalyticsService(service('billing').store)
        return defaults[name]

    def auth_required(function):
        @wraps(function)
        def wrapped(*args,**kwargs):return service('auth').require_auth(function)(*args,**kwargs)
        return wrapped

    def super_required(function):
        @wraps(function)
        def wrapped(*args,**kwargs):return service('auth').require_super_admin(function)(*args,**kwargs)
        return wrapped

    def admin():
        value=service('auth').get_current_admin()
        if not value:abort(401)
        return value

    def allowed():
        value=admin()
        if value.get('is_super') is True:return None
        return {str(value['branch_id'])} if value.get('branch_id') is not None else set()

    def branch(value):
        if not isinstance(value,(int,str)) or isinstance(value,bool):abort(400,description='Выберите филиал')
        value=str(value);scope=allowed()
        if scope is not None and value not in scope:abort(403)
        if service('branches').get_branch(value) is None:abort(404)
        return value

    def body():
        value=request.get_json(silent=True)
        if not isinstance(value,dict):abort(400)
        return value

    def visible_branches():
        scope=allowed()
        return [b for b in service('branches').get_all_branches() if scope is None or str(b.branch_id) in scope]

    def render(name, **context):
        if render_admin:return render_admin(name, **context)
        return render_template(name,admin_page=True,is_super=admin().get('is_super') is True,branches=visible_branches(),**context)

    @bp.errorhandler(ValueError)
    def invalid(error):return jsonify(status='error',message=str(error)),400

    @bp.errorhandler(PermissionError)
    def denied(error):return jsonify(status='error',message='Нет доступа к этой выборке'),403

    @bp.route('/admin/subscription')
    @auth_required
    def subscriptions():
        if admin().get('is_super') is True:return redirect('/admin/branch-plans')
        return render('admin/subscription.html')

    @bp.route('/admin/branch-plans')
    @super_required
    def branch_plans():return render('admin/subscription.html')

    def purchaser():
        if admin().get('is_super') is True:
            abort(403,description='Суперадминистратор управляет тарифами филиалов без оформления покупки')

    @bp.route('/admin/statistics')
    @auth_required
    def statistics():return render('admin/statistics.html')

    @bp.route('/admin/api/product/options')
    @auth_required
    def options():
        branches=visible_branches();ids={str(b.branch_id) for b in branches}
        clients=[{'id':str(v.get('branch_client_id') or k),'person_id':str(v.get('person_id') or k),
            'name':v.get('user_name') or 'Без имени','branch_id':str(v.get('branch_id'))}
            for k,v in service('links').load_links().items() if str(v.get('branch_id')) in ids]
        return jsonify(branches=[{'id':str(b.branch_id),'name':b.name} for b in branches],clients=clients,
            gates=[{'id':str(getattr(g,'stable_id',g.id)),'name':g.label,'branch_id':str(b.branch_id)} for b in branches for g in b.buttons if g.type=='gate'],
            is_super=admin().get('is_super') is True)

    @bp.route('/admin/api/subscriptions')
    @auth_required
    def subscriptions_list():
        billing=service('billing')
        rows=[{'name':b.name,'operational':b.enabled,**billing.subscription_view(b.branch_id,timezone=b.timezone)} for b in visible_branches()]
        is_super=admin().get('is_super') is True
        return jsonify(branches=rows,test_activation_available=is_super and os.getenv('GATEBOT_ENV')=='development',
                       **billing.catalog.list(include_archived=is_super,include_hidden=is_super))

    @bp.route('/admin/api/subscriptions/<branch_id>')
    @auth_required
    def subscription(branch_id):
        key=branch(branch_id);billing=service('billing')
        return jsonify(subscription=billing.subscription_view(key),history=billing.history(key),
                       **billing.catalog.list(include_archived=admin().get('is_super') is True,include_hidden=admin().get('is_super') is True))

    @bp.route('/admin/api/subscriptions/export')
    @auth_required
    def subscriptions_export():
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['Филиал','Статус','План','Действует до','Квота','Израсходовано','Резерв','Остаток'])
        for b in visible_branches():
            view=service('billing').subscription_view(b.branch_id)
            values=[b.name,view['status'],view['plan_id'] or view['source'],view['ends_at'],view['quota'],view['spent'],view['reserved'],view['remaining']]
            writer.writerow(["'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v for v in values])
        return Response('\ufeff'+output.getvalue(),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=gatebot-subscriptions.csv'})

    @bp.route('/admin/api/subscriptions/quote',methods=['POST'])
    @auth_required
    def quote():
        purchaser()
        data=body();return jsonify(service('billing').quote(branch(data.get('branch_id')),data.get('plan_id'),mode=data.get('mode','period_end')))

    @bp.route('/admin/api/subscriptions/orders',methods=['POST'])
    @auth_required
    def order():
        purchaser()
        data=body();return jsonify(order=service('billing').create_order(branch(data.get('branch_id')),data.get('plan_id'),
            actor=session['admin_username'],idempotency_key=data.get('request_id'),mode=data.get('mode','period_end'),
            offer_version=data.get('offer_version'),accept_offer=data.get('accept_offer') is True,
            auto_renew=data.get('auto_renew') is True,confirm_burn=data.get('confirm_burn') is True)),201

    @bp.route('/admin/api/subscriptions/orders/<order_id>/checkout',methods=['POST'])
    @auth_required
    def checkout(order_id):
        purchaser()
        data=body();return jsonify(service('billing').checkout(order_id,branch_id=branch(data.get('branch_id'))))

    @bp.route('/admin/api/subscriptions/gifts',methods=['POST'])
    @bp.route('/admin/api/subscriptions/gifts/preview',methods=['POST'])
    @super_required
    def gift():
        return jsonify(status='error',message='Обновите страницу: подарки теперь оформляются из каталога тарифов и пакетов.'),410

    @bp.route('/admin/api/subscriptions/credits',methods=['POST'])
    @super_required
    def credit():
        return jsonify(status='error',message='Обновите страницу: дополнительная квота предоставляется подарком пакета.'),410

    @bp.route('/admin/api/subscriptions/suspend',methods=['POST'])
    @super_required
    def suspend():
        data=body();service('billing').suspend(branch(data.get('branch_id')),data.get('suspended'),actor=session['admin_username'],reason=str(data.get('reason',''))[:1000])
        return jsonify(status='success')

    @bp.route('/admin/api/statistics/query',methods=['POST'])
    @auth_required
    def query():return jsonify(service('analytics').query(body(),allowed_branches=allowed()))

    @bp.route('/admin/api/statistics/records',methods=['POST'])
    @auth_required
    def records():
        data=body();return jsonify(service('analytics').records(data,allowed_branches=allowed(),page=data.get('page',1)))

    @bp.route('/admin/api/statistics/export',methods=['POST'])
    @auth_required
    def export():
        analytics=service('analytics');result=analytics.query(body(),allowed_branches=allowed())
        return Response(analytics.csv(result),mimetype='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename=gatebot-statistics.csv'})

    from billing_portal_web import register_routes
    register_routes(bp,service=service,auth_required=auth_required,super_required=super_required,
        admin=admin,allowed=allowed,branch=branch,body=body,visible_branches=visible_branches,
        render=render,purchaser=purchaser)
    return bp
