"""Marketplace contracts and tenant boundaries, with all remote effects mocked."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from flask import Flask
from werkzeug.datastructures import MultiDict

from test_identity_product import identities, PASSWORD
from storage.sql_documents import read_document, write_document
from yclients_marketplace import MarketplaceAPI, MarketplaceError, MarketplaceService, parse_redirect
from yclients_marketplace_web import create_blueprint, registration_defaults


@pytest.fixture
def marketplace(identities):
    api=Mock();api.install.return_value={'state':'active'};api.status.return_value={'state':'active'}
    service=MarketplaceService(identities.service,api=api,clock=lambda:identities.clock[0],
        application_id='123',partner_token='offline-partner-key',system_user_token='offline-system-user-token',enabled=True)
    return SimpleNamespace(service=service,identity=identities,api=api,browser='random-test-browser-nonce')


def begin(state,branch='11'):
    return state.service.begin(MultiDict({'salon_id':branch}),state.browser)


def test_provider_contract_is_explicit_and_contains_no_webhook_or_sms_registration():
    transport=Mock();transport.request.return_value=SimpleNamespace(status_code=201)
    api=MarketplaceAPI('offline-partner-key','123',session=transport,installation_enabled=True)
    assert api.install('11')=={'state':'active'}
    args,kwargs=transport.request.call_args
    assert args==('POST','https://api.yclients.ru/marketplace/partner/callback')
    assert kwargs['json']=={'salon_id':11,'application_id':123}
    assert kwargs['headers']['Authorization']=='Bearer offline-partner-key'
    assert kwargs['verify'] is True and kwargs['allow_redirects'] is False


def test_live_installation_disabled_by_default():
    transport=Mock();api=MarketplaceAPI('offline-partner-key','123',session=transport)
    with pytest.raises(MarketplaceError):api.install(11)
    transport.request.assert_not_called()


def test_install_timeout_is_unknown_and_never_retried():
    transport=Mock();transport.request.side_effect=requests.Timeout('private provider details')
    api=MarketplaceAPI('offline-partner-key','123',session=transport,installation_enabled=True)
    assert api.install(11)=={'state':'unknown','code':'network_result_unknown'}
    assert transport.request.call_count==1


def test_status_uses_only_scoped_get():
    transport=Mock();transport.request.return_value=SimpleNamespace(status_code=200,json=lambda:{'success':True,'data':{'connection_status':{'status':'active'}}})
    api=MarketplaceAPI('offline-partner-key','123',session=transport)
    assert api.status(11)=={'state':'active'}
    assert transport.request.call_args.args==('GET','https://api.yclients.ru/marketplace/salon/11/application/123')
    with pytest.raises(MarketplaceError):api.status('../12')
    assert transport.request.call_count==1


def test_signed_user_data_uses_raw_decoded_json_and_never_grants_role(marketplace):
    s=marketplace;raw=json.dumps({'name':'Unique private prefill','phone':'+15550100333','salon_name':'Example salon'},separators=(',',':')).encode()
    args=MultiDict({'salon_id':'11','user_data':base64.b64encode(raw).decode(),
        'user_data_sign':hmac.new(b'offline-partner-key',raw,hashlib.sha256).hexdigest()})
    before=s.identity.links.load_links()
    token=s.service.begin(args,s.browser)
    view=s.service.view(token,s.browser)
    assert view['profile']['name']=='Unique private prefill' and not view['authenticated']
    assert s.identity.links.load_links()==before
    assert b'Unique private prefill' not in Path(s.service.store.path).read_bytes()
    args['user_data_sign']='0'*64
    with pytest.raises(MarketplaceError):s.service.begin(args,s.browser)


@pytest.mark.parametrize('args',[
    [('salon_id','11'),('salon_id','12')], [('salon_id','11'),('salon_ids[]','12')],
    [('salon_id','0')], [('salon_id','../11')], [('salon_id','١١')], [],
    [('salon_id','11'),('user_data','%%%'),('user_data_sign','0'*64)],
])
def test_malformed_and_ambiguous_redirect_is_rejected(args):
    with pytest.raises(MarketplaceError):parse_redirect(MultiDict(args),'offline-partner-key')


def test_multibranch_requires_scope_for_every_branch(marketplace):
    s=marketplace;token=s.service.begin(MultiDict([('salon_ids[]','11'),('salon_ids[]','12')]),s.browser)
    assert s.service.connect(token,s.browser,'north','11')['state']=='active'
    with pytest.raises(PermissionError):s.service.connect(token,s.browser,'north','12')
    assert s.api.install.call_count==1
    assert s.identity.service.integration_credentials('11','yclients')['user_token']=='offline-system-user-token'
    assert s.identity.service.integration_credentials('12','yclients')=={}


def test_browser_binding_expiry_and_local_pending_verification(marketplace):
    s=marketplace;token=begin(s)
    with pytest.raises(MarketplaceError):s.service.connect(token,'wrong-browser','owner','11')
    with s.service.store.connect() as db:
        branches=read_document(db,'branches.json');branches['11']['onboarding_status']='pending_verification';write_document(db,'branches.json',branches)
    with pytest.raises(MarketplaceError):s.service.connect(token,s.browser,'owner','11')
    s.identity.clock[0]+=3601
    with pytest.raises(MarketplaceError):s.service.connect(token,s.browser,'owner','11')
    s.api.install.assert_not_called()


def test_duplicate_click_and_new_redirect_cannot_repeat_unknown_install(marketplace):
    s=marketplace;s.api.install.return_value={'state':'unknown','code':'network_result_unknown'}
    token=begin(s)
    assert s.service.connect(token,s.browser,'owner','11')['state']=='unknown'
    assert s.service.connect(token,s.browser,'owner','11')['replayed']
    another=begin(s)
    assert s.service.connect(another,s.browser,'owner','11')['code']=='reconcile_required'
    assert s.api.install.call_count==1
    assert s.service.connect(another,s.browser,'owner','11',reconcile=True)['state']=='active'
    assert s.api.status.call_count==1 and s.api.install.call_count==1


def test_authenticated_disconnect_stops_crm_but_keeps_manual_access_and_subscription(marketplace,monkeypatch):
    s=marketplace;s.service.connect(begin(s),s.browser,'owner','11')
    before=s.identity.links.load_links()
    payload={'salon_id':11,'application_id':123,'partner_token':'offline-partner-key','event':'uninstall'}
    with pytest.raises(PermissionError):s.service.disconnect({**payload,'partner_token':'wrong'})
    with pytest.raises(PermissionError):s.service.disconnect({**payload,'application_id':999})
    assert s.service.disconnect(payload)=={'status':'success'}
    assert s.identity.links.load_links()==before
    assert s.identity.branches.get_branch('11').enabled
    assert s.identity.service.integration_credentials('11','yclients')=={}
    from yclients_api import YClientsClient,YClientsError
    client=YClientsClient(branches=s.identity.branches,session=Mock())
    with pytest.raises(YClientsError,match='inactive'):client._credentials('11')
    client.session.get.assert_not_called()


def test_disconnect_wins_install_race(marketplace):
    s=marketplace
    def installing(branch):
        s.service.disconnect({'salon_id':11,'application_id':123,'partner_token':'offline-partner-key','event':'uninstall'})
        return {'state':'active'}
    s.api.install.side_effect=installing
    assert s.service.connect(begin(s),s.browser,'owner','11')['state']=='unknown'
    assert s.identity.service.integration_credentials('11','yclients')=={}
    assert s.identity.branches.load_branches()['11']['yclients_marketplace_state']=='uninstalled'


@pytest.mark.parametrize('provider_state',['uninstalled','frozen'])
def test_status_reconciliation_stops_reader_without_removing_manual_access(marketplace,provider_state):
    s=marketplace;s.service.connect(begin(s),s.browser,'owner','11')
    before=s.identity.links.load_links()
    s.api.status.return_value={'state':provider_state}
    # A new intent must GET-check an existing installation, never duplicate POST.
    assert s.service.connect(begin(s),s.browser,'owner','11')['state']==provider_state
    assert s.api.install.call_count==1 and s.api.status.call_count==1
    assert s.identity.branches.load_branches()['11']['yclients_marketplace_state']==provider_state
    assert s.identity.links.load_links()==before and s.identity.branches.get_branch('11').enabled
    if provider_state=='uninstalled':assert s.identity.service.integration_credentials('11','yclients')=={}
    from yclients_api import YClientsClient,YClientsError
    with pytest.raises(YClientsError,match='inactive'):
        YClientsClient(branches=s.identity.branches,session=Mock())._credentials('11')


@pytest.mark.parametrize('event,expected',[('uninstall','uninstalled'),('freeze','frozen')])
def test_lifecycle_overrides_completed_attempt_without_repeating_install(marketplace,event,expected):
    s=marketplace;token=begin(s)
    assert s.service.connect(token,s.browser,'owner','11')['state']=='active'
    s.service.disconnect({'salon_id':11,'application_id':123,'partner_token':'offline-partner-key','event':event})
    assert s.service.view(token,s.browser,'owner')['branches'][0]['state']==expected
    assert s.service.connect(token,s.browser,'owner','11')=={'state':expected,'replayed':True}
    fresh=begin(s)
    assert s.service.view(fresh,s.browser,'owner')['branches'][0]['state']==expected
    assert s.service.view(fresh,s.browser)['branches'][0]['state']=='pending'
    assert s.api.install.call_count==1
    # Reinstallation requires a fresh marketplace consent and explicit action.
    assert s.service.connect(fresh,s.browser,'owner','11')['state']=='active'
    assert s.api.install.call_count==2


def test_late_duplicate_disconnect_recovers_only_by_provider_get(marketplace):
    s=marketplace;s.service.connect(begin(s),s.browser,'owner','11')
    callback={'salon_id':11,'application_id':123,'partner_token':'offline-partner-key','event':'uninstall'}
    s.service.disconnect(callback)
    fresh=begin(s);s.service.connect(fresh,s.browser,'owner','11')
    assert s.api.install.call_count==2
    s.service.disconnect(callback)
    assert s.service.connect(fresh,s.browser,'owner','11')=={'state':'uninstalled','replayed':True}
    assert s.identity.service.integration_credentials('11','yclients')=={}
    assert s.service.connect(fresh,s.browser,'owner','11',reconcile=True)['state']=='active'
    assert s.identity.branches.load_branches()['11']['yclients_marketplace_state']=='active'
    assert s.identity.service.integration_credentials('11','yclients')['user_token']=='offline-system-user-token'
    assert s.api.install.call_count==2 and s.api.status.call_count==1


def test_actual_app_preserves_handoff_through_login_and_authenticates_callback(marketplace,monkeypatch):
    import app as module
    s=marketplace
    provider=lambda auth,access=None:{'service':s.identity.service,'auth':auth,'marketplace':s.service,
        'branches':s.identity.branches,'billing':s.identity.billing}
    monkeypatch.setattr(module,'product_services',lambda:SimpleNamespace(provider=provider,identity=s.identity.service))
    monkeypatch.setattr(module,'admin_auth',s.identity.auth)
    monkeypatch.setattr(module,'branch_manager',s.identity.branches)
    monkeypatch.setattr(module,'link_manager',s.identity.links)
    monkeypatch.setattr(module.logger,'log_admin_action',lambda *args,**kwargs:None)
    module._login_attempts.clear()
    client=module.app.test_client()
    assert client.get('/yclients/marketplace/connect?salon_id=11').status_code==303
    registration=client.get('/register-branch')
    assert registration.status_code==200
    import re
    bootstrap=json.loads(re.search(r'<script id="identityBootstrap" type="application/json">(.*?)</script>',registration.text,re.S).group(1))
    assert bootstrap['data']['initial']['branch_id']=='11' and bootstrap['data']['initial']['marketplace'] is True
    with client.session_transaction() as session:
        original={key:session[key] for key in ('yc_marketplace_intent','yc_marketplace_browser')}
        csrf=session['csrf_token']
    logged_in=client.post('/admin/login',data={'username':'owner','password':PASSWORD,'csrf_token':csrf})
    assert logged_in.status_code==302 and logged_in.location=='/yclients/marketplace'
    assert client.get(logged_in.location).status_code==200
    with client.session_transaction() as session:
        assert all(session[key]==value for key,value in original.items())
        csrf=session['csrf_token']
    assert client.post('/yclients/marketplace/connect',json={'branch_id':'11'}).status_code==403
    connected=client.post('/yclients/marketplace/connect',json={'branch_id':'11'},headers={'X-CSRF-Token':csrf})
    assert connected.status_code==200 and connected.json['state']=='active'
    external=module.app.test_client()
    callback={'salon_id':11,'application_id':123,'event':'uninstall'}
    assert external.post('/webhook/yclients/marketplace',json=callback).status_code==403
    assert s.identity.service.integration_credentials('11','yclients')
    callback['partner_token']='offline-partner-key'
    assert external.post('/webhook/yclients/marketplace',json=callback).status_code==200
    assert s.identity.service.integration_credentials('11','yclients')=={}
    assert s.api.install.call_count==1


def test_public_entry_removes_profile_from_url_and_keeps_authenticated_views_scoped(marketplace):
    s=marketplace
    app=Flask(__name__,template_folder=str(Path(__file__).resolve().parents[1]/'templates'))
    app.secret_key='offline-browser-session-secret'*2
    auth=SimpleNamespace(get_current_admin=lambda:None)
    provider=lambda:{'service':s.identity.service,'auth':auth,'marketplace':s.service}
    app.register_blueprint(create_blueprint(provider))
    app.context_processor(lambda:{'csrf_token':lambda:'offline-csrf-token'})
    client=app.test_client()
    response=client.get('/yclients/marketplace/connect?salon_id=11')
    assert response.status_code==303 and response.location=='/yclients/marketplace'
    page=client.get(response.location)
    assert page.status_code==200 and b'/admin/login?next=marketplace' in page.data
    other=app.test_client();assert other.get('/yclients/marketplace/state').status_code==400
    with client.session_transaction() as session:values=dict(session)
    with app.test_request_context('/'):
        from flask import session
        session.update(values)
        assert registration_defaults(provider)['branch_id']=='11'
