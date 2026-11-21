"""Private Web Push consent, tenant/client boundaries and mocked delivery."""
import base64
import json
import socket
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
import pytest
import requests

from pwa_service import PwaService
from product_notifications import ProductNotifications
from storage import config
from storage.sql_documents import write_document, read_document
from test_identity_product import identities
from test_web_security import web, post
from webpush_delivery import PushSession, send_webpush, validate_endpoint


def subscription(device='one'):
    public=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
    encoded=lambda value:base64.urlsafe_b64encode(value).decode().rstrip('=')
    return {'endpoint':'https://fcm.googleapis.com/fcm/send/synthetic-'+device,
            'keys':{'p256dh':encoded(public),'auth':encoded(b'0123456789abcdef')},'expirationTime':None}


def test_metadata_resolves_preference_ticket_and_guests_cannot_subscribe(identities):
    state=identities;pwa=PwaService(state.service)
    token=state.service.preferences_link_for_token('example-client').rsplit('/',1)[1]
    assert pwa.metadata(token)['token']=='example-client'
    assert pwa.metadata(token)['scope']=='/pwa/example-client/'
    guest=state.links.create_link({'branch_id':11,'phone':'+15550100555','is_temporary':True})
    assert not pwa.metadata(guest)['allow_push'] and pwa.metadata(guest)['preferences_url'] is None
    with pytest.raises(PermissionError):pwa.status(guest)
    with pytest.raises(PermissionError):pwa.subscribe(guest,subscription(),consent=True)
    with pytest.raises(ValueError):pwa.metadata('missing')


def test_consent_encrypted_persistence_multiple_devices_and_client_scope(identities):
    state=identities;pwa=PwaService(state.service);one=subscription();two=subscription('two')
    assert not state.service.prefs_for_token('example-client')['channels']['push']
    with pytest.raises(ValueError):state.service.save_prefs_for_token('example-client',{'enabled':True,'channels':{'push':True}})
    with pytest.raises(ValueError):pwa.subscribe('example-client',one)
    status=pwa.subscribe('example-client',one,consent=True)
    assert status['channel_enabled'] and status['subscription_count']==1
    public=status['public_key']
    assert PwaService(state.service).status('example-client')['public_key']==public
    pwa.subscribe('example-client',one,consent=True)
    assert pwa.subscribe('example-client',two,consent=True)['subscription_count']==2
    other=state.links.create_link({'branch_id':12,'phone':'+15550100556'})
    with pytest.raises(ValueError):pwa.subscribe(other,one,consent=True)
    assert pwa.unsubscribe(other,one['endpoint'])['subscription_count']==0
    assert pwa.status('example-client')['subscription_count']==2
    with state.product.connect() as db:
        rows=[dict(row) for row in db.execute('SELECT * FROM pwa_subscriptions')]
        assert one['endpoint'] not in json.dumps(rows) and one['keys']['auth'] not in json.dumps(rows)
        assert public not in db.execute('SELECT ciphertext FROM pwa_keys').fetchone()[0]
    assert 'endpoint' not in json.dumps(status) and one['keys']['auth'] not in json.dumps(status)


def test_device_unsubscribe_and_new_push_opt_in_do_not_restore_other_channels(identities):
    state=identities;pwa=PwaService(state.service);one=subscription();two=subscription('two')
    state.service.save_prefs_for_token('example-client',{'enabled':False,'channels':{'telegram':True}})
    result=pwa.subscribe('example-client',one,consent=True)
    assert result['preferences']['channels']['push'] and not result['preferences']['channels']['telegram']
    pwa.subscribe('example-client',two,consent=True)
    result=pwa.unsubscribe('example-client',one['endpoint'])
    assert result['channel_enabled'] and result['subscription_count']==1
    result=pwa.unsubscribe('example-client',two['endpoint'])
    assert not result['channel_enabled'] and not result['preferences']['channels']['push']


@pytest.mark.parametrize('endpoint',[
    'http://fcm.googleapis.com/fcm/send/test','https://127.0.0.1/test',
    'https://fcm.googleapis.com.evil.invalid/test','https://user@fcm.googleapis.com/test',
    'https://fcm.googleapis.com:444/test','https://fcm.googleapis.com/test#fragment',
    'https://web.push.apple.com\\@127.0.0.1/test',
])
def test_endpoint_validation_rejects_private_and_unapproved_destinations(endpoint):
    with pytest.raises(ValueError):validate_endpoint(endpoint)


@pytest.mark.parametrize('stale',['none','revision','disabled','deleted'])
def test_durable_push_outbox_fans_out_and_rechecks_current_recipient(identities,monkeypatch,stale):
    state=identities;sent=[]
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    monkeypatch.setattr(config,'BASE_URL','https://gate.example.invalid')
    pwa=PwaService(state.service,transport=lambda *args:sent.append(args) or {'status':'accepted','confirmed':False})
    pwa.subscribe('example-client',subscription(),consent=True)
    pwa.subscribe('example-client',subscription('two'),consent=True)
    worker=ProductNotifications(state.service,state.links,state.branches,state.billing,pwa=pwa)
    payload={**state.links.load_links()['example-client'],'token':'example-client','event':'granted'}
    worker.enqueue(payload);worker.enqueue(payload)
    with state.product.connect() as db:
        assert db.execute('SELECT count(*) FROM message_outbox WHERE channel="push"').fetchone()[0]==2
    if stale=='revision':
        with state.links.store.transaction() as clients:clients['example-client']['access_revision']=1
    elif stale=='disabled':state.service.save_prefs_for_token('example-client',{'enabled':False,'channels':{'push':True}})
    elif stale=='deleted':state.links.delete_link('example-client')
    worker.process_once()
    assert len(sent)==(2 if stale=='none' else 0)
    if sent:
        assert sent[0][1]['url']=='/pwa/example-client/gate'
        assert sent[0][1]['title']=='GateBot'
    worker.process_once()
    assert len(sent)==(2 if stale=='none' else 0)


def test_expired_push_subscription_is_cleaned_without_touching_other_device(identities,monkeypatch):
    state=identities;monkeypatch.setattr(config,'BASE_URL','https://gate.example.invalid')
    pwa=PwaService(state.service,transport=lambda *args:{'status':'expired','code':'push_subscription_expired'})
    pwa.subscribe('example-client',subscription(),consent=True);pwa.subscribe('example-client',subscription('two'),consent=True)
    with state.product.connect() as db:
        first=db.execute('SELECT id FROM pwa_subscriptions ORDER BY created,id').fetchone()[0]
    result=pwa.deliver(first,{'client_token':'example-client','branch_id':'11','event':'access_granted','access_revision':0})
    assert result['status']=='expired' and pwa.status('example-client')['subscription_count']==1


@pytest.mark.parametrize('change',['phone','branch','delete'])
def test_client_identity_changes_remove_browser_subscription_in_shared_writer(identities,change):
    state=identities;pwa=PwaService(state.service)
    pwa.subscribe('example-client',subscription(),consent=True)
    original=state.links.load_links()['example-client']
    with state.links.store.transaction() as clients:
        if change=='phone':clients['example-client']['phone']='+15550100666'
        elif change=='branch':clients['example-client']['branch_id']=12
        else:del clients['example-client']
    with state.product.connect() as db:
        assert db.execute('SELECT count(*) FROM pwa_subscriptions').fetchone()[0]==0
    if change!='delete':
        with state.links.store.transaction() as clients:
            clients['example-client']['phone']=original['phone'];clients['example-client']['branch_id']=11
        assert not pwa.status('example-client')['channel_enabled']


def test_real_encryption_uses_bounded_no_redirect_transport_and_safe_errors(identities,monkeypatch):
    pwa=PwaService(identities.service);pwa.status('example-client');captured=[]
    with identities.product.connect() as db:private=pwa._keys(db)['private']
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))])
    def request(session,method,url,**kwargs):
        captured.append((session,kwargs))
        response=requests.Response();response.status_code=201;response._content=b''
        return response
    monkeypatch.setattr(requests.Session,'request',request)
    result=send_webpush(subscription(),{'title':'GateBot','body':'Synthetic push'},private,'https://gate.example.invalid')
    assert result['status']=='accepted'
    session,options=captured[0]
    assert not session.trust_env and options['allow_redirects'] is False and options['timeout']==10
    assert b'Synthetic push' not in options['data'] and options['headers']['authorization'].startswith('vapid ')
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))])
    assert send_webpush(subscription(),{'title':'GateBot'},private,'https://gate.example.invalid')['code']=='push_invalid'
    assert len(captured)==1


def test_http_push_requires_csrf_and_never_returns_subscription_secrets(web,tmp_path,monkeypatch):
    monkeypatch.setattr(config,'CLIENT_OUTBOUND_ENABLED',True)
    from storage.sql_documents import migrate_legacy
    migrate_legacy(tmp_path,activate=True)
    client=web.app.test_client();endpoint='/pwa/'+web.token_a+'/push/'
    status=client.get(endpoint+'status')
    assert status.status_code==200 and status.headers['Cache-Control']=='no-store'
    data={'subscription':subscription(),'consent':True}
    assert client.post(endpoint+'subscribe',json=data).status_code==403
    result=post(client,status.json['csrf_token'],endpoint+'subscribe',data)
    assert result.status_code==200 and result.json['channel_enabled']
    assert data['subscription']['endpoint'] not in result.get_data(as_text=True)
    assert client.get('/pwa/missing/push/status').status_code==400


def test_http_pwa_scope_is_personal_and_only_gate_preferences_have_install_hooks(web,tmp_path):
    from storage.sql_documents import migrate_legacy
    migrate_legacy(tmp_path,activate=True)
    products=web.module.product_services();client=web.app.test_client()
    ticket=products.identity.preferences_link_for_token(web.token_a).rsplit('/',1)[1]
    scope='/pwa/'+web.token_a+'/'
    manifest=client.get(scope+'manifest.webmanifest')
    assert manifest.status_code==200 and manifest.json['id']==scope
    assert manifest.json['scope']==scope and manifest.json['start_url']==scope+'gate'
    assert client.get('/pwa/'+ticket+'/manifest.webmanifest').json['id']==scope
    assert client.get('/pwa/'+web.token_b+'/manifest.webmanifest').json['id']!=scope
    for url in ('/opn/'+web.token_a,'/preferences/'+web.token_a,'/preferences/'+ticket,scope+'gate',scope+'preferences'):
        response=client.get(url)
        assert response.status_code==200
        html=response.get_data(as_text=True)
        assert 'rel="manifest"' in html and 'id="clientPwaConfig"' in html
        assert scope+'manifest.webmanifest' in html
    for url in ('/','/admin/login'):
        html=client.get(url).get_data(as_text=True)
        assert 'rel="manifest"' not in html and 'id="clientPwaConfig"' not in html
    guest=web.links.create_link({'branch_id':12,'phone':'+15550100777','is_temporary':True})
    guest_html=client.get('/pwa/'+guest+'/gate').get_data(as_text=True)
    assert '"allow_push": false' in guest_html
    assert client.get('/pwa/'+guest+'/preferences').status_code==403
    assert client.get('/pwa/'+guest+'/push/status').status_code==403
    for suffix in ('gate','preferences','manifest.webmanifest'):
        assert 400<=client.get('/pwa/missing/'+suffix).status_code<500
    worker=client.get('/pwa/sw.js')
    assert worker.status_code==200 and worker.headers['Service-Worker-Allowed']=='/pwa/'
    # A public QR entry identifies a gate, not a personal PWA identity.
    web.module.product_provider()
    gate=web.branches.get_branch(12).buttons[0]
    qr=products.gate_qr.admin_view('super',12,gate.stable_id,'create')
    html=client.get('/gate/'+qr['url'].rsplit('/',1)[1]).get_data(as_text=True)
    assert 'rel="manifest"' not in html and 'id="clientPwaConfig"' not in html
