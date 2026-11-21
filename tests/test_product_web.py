"""HTTP scope regressions using isolated services and replaceable runtime providers."""
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask, jsonify, session

from billing_service import BillingService
from analytics_service import AnalyticsService
from product_web import create_blueprint


class Auth:
    def __init__(self):self.current={'is_super':False,'branch_id':12,'branch_ids':['12']}
    def get_current_admin(self):return self.current
    def require_auth(self, function):
        @wraps(function)
        def wrapped(*args,**kwargs):
            if self.current is None:return jsonify(message='Login required'),401
            return function(*args,**kwargs)
        return wrapped
    def require_super_admin(self, function):
        @wraps(function)
        def wrapped(*args,**kwargs):
            if self.current is None:return jsonify(message='Login required'),401
            if self.current.get('is_super') is not True:return jsonify(message='Forbidden'),403
            return function(*args,**kwargs)
        return wrapped


@pytest.fixture
def web(tmp_path):
    billing=BillingService(tmp_path/'application.sqlite3');auth=Auth()
    branches=[SimpleNamespace(branch_id=n,name='Branch '+str(n),buttons=[SimpleNamespace(id='secret-target-id',stable_id='gate'+str(n),label='Gate',type='gate')],enabled=True,timezone='Europe/Moscow') for n in (12,123)]
    services={'auth':auth,'billing':billing,'analytics':AnalyticsService(billing.store),
        'branches':SimpleNamespace(get_all_branches=lambda:branches,get_branch=lambda n:next((b for b in branches if str(b.branch_id)==str(n)),None)),
        'links':SimpleNamespace(load_links=lambda:{'private-token12':{'branch_id':12,'user_name':'Alice','person_id':'p12','branch_client_id':'c12'},'private-token123':{'branch_id':123,'user_name':'Other','person_id':'p123','branch_client_id':'c123'}})}
    app=Flask(__name__,template_folder=str(Path(__file__).resolve().parents[1]/'templates'));app.secret_key='test-only-product-session'
    app.context_processor(lambda:{'csrf_token':lambda:'test-csrf-token'})
    app.register_blueprint(create_blueprint(provider=lambda:services))
    client=app.test_client()
    with client.session_transaction() as s:s['admin_username']='tester'
    return client,services


def test_subscription_and_options_scope_and_safe_identifiers(web):
    client,services=web
    result=client.get('/admin/api/subscriptions')
    assert result.status_code==200 and [r['branch_id'] for r in result.json['branches']]==['12']
    options=client.get('/admin/api/product/options').json
    assert [r['id'] for r in options['clients']]==['c12']
    assert options['gates'][0]['id']=='gate12'
    assert b'private-token' not in client.get('/admin/api/product/options').data
    assert client.get('/admin/api/subscriptions/123').status_code==403
    assert client.post('/admin/api/subscriptions/quote',json={'branch_id':123,'plan_id':'start'}).status_code==403
    assert client.post('/admin/api/subscriptions/gifts',json={'branch_id':12,'quota':10,'days':2}).status_code==403
    export=client.get('/admin/api/subscriptions/export')
    assert export.status_code==200 and 'Branch 123' not in export.get_data(as_text=True)


def test_replaced_auth_and_revoked_role_apply_on_next_request(web):
    client,services=web
    replacement=Auth();replacement.current=None;services['auth']=replacement
    assert client.get('/admin/api/subscriptions').status_code==401
    replacement.current={'is_super':True}
    assert len(client.get('/admin/api/subscriptions').json['branches'])==2
    replacement.current={'is_super':False,'branch_id':123}
    assert [r['branch_id'] for r in client.get('/admin/api/subscriptions').json['branches']]==['123']


def test_super_gift_order_and_payment_stub(web):
    client,services=web;services['auth'].current={'is_super':True}
    assert client.post('/admin/api/subscriptions/gifts',json={'branch_id':123,'quota':300,'days':10}).status_code==410
    gift=client.post('/admin/api/subscriptions/gifts/tariff',json={'branch_id':123,'plan_id':'start','quota':300,'days':10,'mode':'replace','confirm_burn':True})
    assert gift.status_code==200
    assert client.get('/admin/subscription').status_code==302
    page=client.get('/admin/branch-plans')
    assert page.status_code==200 and b'id="planCards"' not in page.data and b'id="purchaseDialog"' not in page.data
    for endpoint in ('quote','orders'):
        assert client.post('/admin/api/subscriptions/'+endpoint,json={'branch_id':123,'plan_id':'start','request_id':'example-order'}).status_code==403
    services['auth'].current={'is_super':False,'branch_id':123}
    offer=services['billing'].catalog.publish_offer({'filename':'offer.pdf','url':'/admin/billing-assets/'+'a'*32+'.pdf','sha256':'a'*64},actor='tester')
    order=client.post('/admin/api/subscriptions/orders',json={'branch_id':123,'plan_id':'start','request_id':'example-order','offer_version':offer['id'],'accept_offer':True})
    assert order.status_code==201
    key=order.json['order']['id']
    assert client.post('/admin/api/subscriptions/orders/'+key+'/checkout',json={'branch_id':123}).json['status']=='payment_unavailable'
    services['auth'].current={'is_super':True}
    assert client.post('/admin/api/subscriptions/orders/'+key+'/checkout',json={'branch_id':123}).status_code==403
    assert services['billing'].subscription_view('123')['source']=='gift'
    assert not services['billing'].history('123')['payments']
    services['auth'].current={'is_super':False,'branch_id':12}
    assert client.get('/admin/branch-plans').status_code==403
    assert client.post('/admin/api/subscriptions/orders/'+key+'/checkout',json={'branch_id':123}).status_code==403
    assert client.post('/admin/api/subscriptions/orders/'+key+'/checkout',json={'branch_id':12}).status_code==400


def test_scoped_analytics_json_csv_records(web):
    client,services=web
    for branch in ('12','123'):services['billing'].record_denied('deny'+branch,branch,'person','gate','pass_expired')
    filters={'group_by':['branch']}
    query=client.post('/admin/api/statistics/query',json=filters)
    assert query.status_code==200 and query.json['summary']['requests']==1
    assert client.post('/admin/api/statistics/records',json=filters).json['total']==1
    exported=client.post('/admin/api/statistics/export',json=filters)
    assert exported.status_code==200 and '123' not in exported.get_data(as_text=True)
    for path in ('query','export','records'):
        assert client.post('/admin/api/statistics/'+path,json={**filters,'branches':['123']}).status_code==403


def test_templates_render_and_invalid_inputs_stay_client_errors(web):
    client,services=web
    for page in ('subscription','statistics'):
        response=client.get('/admin/'+page)
        assert response.status_code==200 and 'data-shared-header' in response.get_data(as_text=True)
    assert client.post('/admin/api/subscriptions/orders',json={'branch_id':12,'plan_id':'start'}).status_code==400
    assert client.post('/admin/api/statistics/query',json={'timezone':'invalid'}).status_code==400
    assert client.post('/admin/api/statistics/query',json={'group_by':[{}]}).status_code==400


def test_role_filter_tracks_role_at_request_and_creation(web):
    client,services=web
    for i,role in enumerate(('employee','client','guest')):
        services['billing'].record_denied('r-'+role,'12','same-person','gate','hours_closed',client_role=role)
        services['analytics'].record_client('c-'+str(i),'12','p-'+str(i),role=role)
    result=client.post('/admin/api/statistics/query',json={'roles':['employee'],'group_by':['role']}).json
    assert result['summary']['requests']==result['summary']['new_branch_clients']==1
    assert result['groups'][0]['dimensions']=={'role':'employee'}
    detail=client.post('/admin/api/statistics/records',json={'roles':['guest']}).json
    assert [record['client_role'] for record in detail['records']]==['guest']
