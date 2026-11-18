"""Billing portal scope, immutable documents and order consent. Offline only."""
import io
from pathlib import Path

from PIL import Image

from test_product_web import web


PDF = b'%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n'


def plan(**changes):
    return {'name':'Synthetic tariff','price_kopecks':123400,'duration_value':45,
            'duration_unit':'days','quota':200,'sms_quota':50,'logo':'','active':True,**changes}


def publish(client):
    return client.post('/admin/api/billing/offer',data={'document':(io.BytesIO(PDF),'offer.pdf')})


def test_catalog_role_revision_archive_and_primary_branch_only(web):
    client,s=web
    assert client.get('/admin/tariffs').status_code==200
    assert client.get('/admin/packages').status_code==200
    assert client.post('/admin/api/billing/catalog/plan',json=plan()).status_code==403
    s['auth'].current={'is_super':True}
    saved=client.post('/admin/api/billing/catalog/plan',json=plan()).json['item']
    assert saved['revision']==1
    changed=client.post('/admin/api/billing/catalog/plan',json={**saved,'name':'Changed','active':False})
    assert changed.status_code==200
    assert client.post('/admin/api/billing/catalog/plan',json=saved).status_code==400
    assert any(p['id']==saved['id'] for p in client.get('/admin/api/billing/catalog').json['plans'])
    s['auth'].current={'is_super':False,'branch_id':12,'branch_ids':['12','123']}
    assert not any(p['id']==saved['id'] for p in client.get('/admin/api/billing/catalog').json['plans'])
    assert [b['branch_id'] for b in client.get('/admin/api/subscriptions').json['branches']]==['12']
    for path in ('usage?resource=sms','history?kind=payments','history?kind=operations'):
        assert client.get('/admin/api/subscriptions/'+path+'&branch_id=123').status_code==403
    assert client.post('/admin/api/subscriptions/cancel',json={'branch_id':123}).status_code==403


def test_offer_versions_and_logo_are_private_and_validated(web):
    client,s=web
    assert publish(client).status_code==403
    s['auth'].current={'is_super':True}
    first=publish(client)
    assert first.status_code==200
    offer=first.json['offer']
    response=client.get(offer['url'])
    assert response.data==PDF and response.headers['X-Content-Type-Options']=='nosniff'
    response.close()
    second=publish(client).json['offer']
    assert second['id']!=offer['id'] and second['url']!=offer['url']
    response=client.get(offer['url']);assert response.data==PDF;response.close()
    assert client.get('/admin/api/billing/offer?version='+offer['id']).json['offer']==offer
    assert client.post('/admin/api/billing/offer',data={'document':(io.BytesIO(b'not pdf'),'wrong.pdf')}).status_code==400
    image=Image.new('RGB',(20,20)); data=io.BytesIO();image.save(data,format='PNG')
    logo=client.post('/admin/api/billing/logo',data={'image':(io.BytesIO(data.getvalue()+b'private trailing data'),'logo.png')})
    assert logo.status_code==200
    response=client.get(logo.json['logo']);assert response.mimetype=='image/png' and b'private trailing data' not in response.data;response.close()
    assert client.post('/admin/api/billing/catalog/plan',json=plan(logo='https://example.invalid/logo.png')).status_code==400
    s['auth'].current=None
    assert client.get(offer['url']).status_code==401
    assert client.get(logo.json['logo']).status_code==401
    assert (Path(s['billing'].store.path).parent/'billing-assets').is_dir()


def test_order_requires_offer_and_burn_consent_then_stub_does_not_activate(web):
    client,s=web;s['auth'].current={'is_super':True}
    offer=publish(client).json['offer']
    assert client.post('/admin/api/subscriptions/gifts/tariff',json={'branch_id':12,'plan_id':'start','mode':'replace','confirm_burn':True}).status_code==200
    before=s['billing'].subscription_view(12)
    s['auth'].current={'is_super':False,'branch_id':12}
    data={'branch_id':12,'plan_id':'business','mode':'now','request_id':'request-synthetic-one','offer_version':offer['id']}
    assert client.post('/admin/api/subscriptions/orders',json=data).status_code==400
    assert client.post('/admin/api/subscriptions/orders',json={**data,'accept_offer':True}).status_code==400
    response=client.post('/admin/api/subscriptions/orders',json={**data,'accept_offer':True,'confirm_burn':True})
    assert response.status_code==201
    key=response.json['order']['id']
    assert client.post('/admin/api/subscriptions/orders/'+key+'/checkout',json={'branch_id':12}).json['status']=='payment_unavailable'
    after=s['billing'].subscription_view(12)
    assert after['term_id']==before['term_id'] and after['remaining']==before['remaining']
    assert not after['renewal']['enabled']
    assert client.get('/admin/api/subscriptions/history?kind=payments').json['total']==0
    assert client.get('/admin/api/subscriptions/history?kind=operations').json['total']>0


def test_renewal_withdrawal_preserves_access_and_immediate_cancel_requires_confirmation(web):
    client,s=web;s['auth'].current={'is_super':True};offer=publish(client).json['offer']
    client.post('/admin/api/subscriptions/gifts/tariff',json={'branch_id':12,'plan_id':'start','mode':'replace','confirm_burn':True})
    order=s['billing'].create_order(12,'start',actor='tester',idempotency_key='synthetic-paid',mode='now',offer_version=offer['id'],accept_offer=True,confirm_burn=True)
    s['billing'].apply_payment(order['id'],'synthetic-payment',order['amount_kopecks'])
    s['auth'].current={'is_super':False,'branch_id':12}
    data={'branch_id':12,'enabled':True,'offer_version':offer['id'],'accept_offer':True}
    assert client.post('/admin/api/subscriptions/renewal',json=data).status_code==400
    assert client.post('/admin/api/subscriptions/renewal',json={**data,'consent':True}).status_code==200
    before=s['billing'].subscription_view(12)
    assert client.post('/admin/api/subscriptions/renewal',json={'branch_id':12,'enabled':False}).status_code==200
    after=s['billing'].subscription_view(12)
    assert after['available'] and after['ends_at']==before['ends_at'] and not after['renewal']['enabled']
    assert client.post('/admin/api/subscriptions/cancel',json={'branch_id':12,'immediate':True}).status_code==400
    assert client.post('/admin/api/subscriptions/cancel',json={'branch_id':12,'immediate':True,'confirm_burn':True}).status_code==200
    assert not s['billing'].subscription_view(12)['available']


def test_test_activation_requires_super_explicit_development_and_never_creates_payment(web,monkeypatch):
    client,s=web
    payload={'branch_id':12,'plan_id':'start','request_id':'test-activation','confirm_test':True}
    endpoint='/admin/api/subscriptions/test-activate'
    assert client.post(endpoint,json=payload).status_code==403
    s['auth'].current={'is_super':True}
    monkeypatch.setenv('GATEBOT_ENV','production')
    assert client.post(endpoint,json=payload).status_code==403
    assert not client.get('/admin/api/subscriptions').json['test_activation_available']
    monkeypatch.setenv('GATEBOT_ENV','development')
    assert client.get('/admin/api/subscriptions').json['test_activation_available']
    assert client.post(endpoint,json={**payload,'confirm_test':False}).status_code==400
    plan=s['billing'].catalog.get('plan','start')
    s['billing'].catalog.save('plan',{**plan,'visible':False},actor='super')
    response=client.post(endpoint,json=payload)
    assert response.status_code==200
    view=response.json['subscription']
    assert view['source']=='test' and view['available'] and view['calls']['remaining']==100
    assert not view['renewal']['enabled']
    assert client.post(endpoint,json=payload).json['duplicate']
    with s['billing'].store.connect() as db:
        assert db.execute('SELECT count(*) FROM billing_payments').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM billing_orders').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM billing_consents').fetchone()[0]==0
    s['auth'].current={'is_super':False,'branch_id':12}
    assert not any(p['id'] in {'start','trial'} for p in client.get('/admin/api/billing/catalog').json['plans'])
    assert client.get('/admin/api/subscriptions/12').json['subscription']['plan']['id']=='start'
    assert client.post('/admin/api/subscriptions/quote',json={'branch_id':12,'plan_id':'start'}).status_code==400
