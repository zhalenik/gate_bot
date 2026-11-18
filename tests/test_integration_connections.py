"""Offline connection diagnostics: permissions, tenant kind, no token disclosure."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from flask import Flask

from test_identity_product import identities
from identity_web import create_blueprint
from storage.sql_documents import read_document, write_document
from yclients_connection import YClientsConnector
from chatpush import validate_token


def permissions():
    return {'timetable': {'timetable_access': True, 'timetable_phones_access': True, 'master_id': 0, 'position_id': 0},
            'record_form': {'record_form_access': True, 'record_form_client_access': True},
            'clients': {'clients_access': True, 'client_phones_access': True, 'clients_phones_email_access': True, 'clients_card_phone_access': True}}


def response(data, status=200):
    return SimpleNamespace(status_code=status,json=lambda:{'success':True,'data':data})


def connector(http):
    return YClientsConnector(http,partner_token='offline-partner',base_url='https://api.yclients.ru/api/v1')


def test_permission_and_records_checks_are_selected_branch_get_only():
    http=Mock();http.get.side_effect=[response(permissions()),response([])]
    result=connector(http).check_permissions('11',{'user_token':'offline-user'})
    assert result['connected'] and result['code']=='ready'
    assert [call.args[0] for call in http.get.call_args_list]==[
        'https://api.yclients.ru/api/v1/user/permissions/11','https://api.yclients.ru/api/v1/records/11']
    for call in http.get.call_args_list:
        assert call.kwargs['verify'] and call.kwargs['allow_redirects'] is False
    assert http.get.call_args.kwargs['params']['count']==1
    http.post.assert_not_called()
    assert 'offline' not in json.dumps(result)


@pytest.mark.parametrize('group,field,value',[('timetable','timetable_phones_access',False),('record_form','record_form_client_access',None),('timetable','master_id',81),('timetable','position_id',None),('record_form','record_form_access','true')])
def test_permissions_fail_closed_even_if_records_could_be_empty(group,field,value):
    http=Mock();rights=permissions();rights[group][field]=value;http.get.return_value=response(rights)
    result=connector(http).check_permissions('11',{'user_token':'offline-user'})
    assert not result['connected'] and result['code']=='permissions_missing'
    assert http.get.call_count==1


def test_records_contacts_do_not_require_access_to_whole_client_directory():
    http=Mock();rights=permissions();rights['clients']={}
    http.get.side_effect=[response(rights),response([])]
    result=connector(http).check_permissions('11',{'user_token':'offline-user'})
    assert result['connected']
    assert next(item for item in result['checks'] if item['key']=='client_directory')['state']=='warning'


def test_record_with_hidden_phone_is_not_confirmed_even_with_declared_permissions():
    http=Mock();http.get.side_effect=[response(permissions()),response([{'company_id':11,'client':{'id':31,'phone':'*******'}}])]
    result=connector(http).check_permissions('11',{'user_token':'offline-user'})
    assert not result['connected'] and result['code']=='contacts_unconfirmed'


@pytest.mark.parametrize('status,code',[(401,'authorization_failed'),(403,'access_denied'),(404,'branch_unavailable'),(429,'rate_limited'),(500,'provider_unavailable')])
def test_provider_failure_has_safe_detailed_code(status,code):
    http=Mock();http.get.return_value=response({'secret':'private'},status)
    result=connector(http).check_permissions('11',{'user_token':'private-token'})
    assert result['code']==code and not result['connected'] and 'private' not in json.dumps(result)


def test_transport_and_cross_branch_records_are_not_confirmed():
    http=Mock();http.get.side_effect=requests.Timeout('private-token')
    assert connector(http).check_permissions('11',{'user_token':'token'})['code']=='transport_error'
    http.get.side_effect=[response(permissions()),response([{'company_id':12}])]
    assert not connector(http).check_permissions('11',{'user_token':'token'})['connected']


def test_chatpush_token_needs_no_claims_or_separate_setup():
    assert validate_token('opaque-offline-token')=='opaque-offline-token'
    assert validate_token('  synthetic.header.signature  ')=='synthetic.header.signature'
    for invalid in ['',None,'Bearer synthetic-token','token\r\nheader:value','токен']:
        with pytest.raises(ValueError):validate_token(invalid)


def web(state, **providers):
    app=Flask(__name__);app.secret_key='offline-secret'
    app.register_blueprint(create_blueprint(lambda:{'service':state.service,'auth':state.auth,'branches':state.branches,**providers}))
    client=app.test_client()
    with app.app_context():fingerprint=state.auth._session_fingerprint(state.auth.get_admin('north'))
    with client.session_transaction() as cookie:cookie.update(admin_logged_in=True,admin_username='north',admin_auth_version=fingerprint)
    return client


def test_manual_yclients_token_is_blocked_for_branch_admin(identities):
    client=web(identities)
    assert client.post('/admin/api/identity/integrations/yclients',json={'branch_id':'11','credentials':{'user_token':'offline'}}).status_code==403
    assert identities.service.integration_credentials('11','yclients')=={}


def test_failed_rights_never_replace_working_credentials(identities):
    state=identities;state.service.save_integration('north','11','yclients',{'user_token':'previous-token'})
    authorizer=Mock(return_value={'user_token':'new-private-token'})
    checker=Mock(return_value={'connected':False,'code':'permissions_missing','message':'Проверьте права','checks':[]})
    client=web(state,authorize_yclients=authorizer,check_yclients=checker)
    result=client.post('/admin/api/identity/integrations/yclients/authorize',json={'branch_id':'11','login':'test','password':'offline-password'})
    assert result.status_code==400 and not result.json['configured']
    assert state.service.integration_credentials('11','yclients')['user_token']=='previous-token'
    assert 'private-token' not in result.get_data(as_text=True)


@pytest.mark.parametrize('connected',[False,True])
def test_only_verified_direct_authorization_unblocks_old_marketplace_state(identities,connected):
    state=identities
    with state.product.connect() as db:
        branches=read_document(db,'branches.json');branches['11']['yclients_marketplace_state']='frozen';write_document(db,'branches.json',branches)
    assert state.service.integration_status('north','11')['yclients']['blocked']
    client=web(state,authorize_yclients=lambda *args:{'user_token':'new-private-token'},
        check_yclients=lambda *args:{'connected':connected,'code':'ready' if connected else 'permissions_missing','checks':[]})
    result=client.post('/admin/api/identity/integrations/yclients/authorize',json={'branch_id':'11','login':'test','password':'offline-password'})
    assert result.status_code==(200 if connected else 400)
    with state.product.connect() as db:branch=read_document(db,'branches.json')['11']
    if connected:
        assert branch['yclients_connection_source']=='direct' and 'yclients_marketplace_state' not in branch
        assert not state.service.integration_status('north','11')['yclients']['blocked']
    else:assert branch['yclients_marketplace_state']=='frozen'


def test_standalone_blocks_saved_credentials_all_integration_api_and_check(identities):
    state=identities;state.service.save_integration('north','11','yclients',{'user_token':'previous-token'})
    with state.product.connect() as db:
        branches=read_document(db,'branches.json');branches['11']['yclients_enabled']=False;write_document(db,'branches.json',branches)
    authorizer=Mock();client=web(state,authorize_yclients=authorizer)
    assert state.service.integration_credentials('11','yclients')=={}
    for path in ['yclients','chatpush','yclients/check','chatpush/check','yclients/authorize']:
        assert client.post('/admin/api/identity/integrations/'+path,json={'branch_id':'11','credentials':{'user_token':'value'}}).status_code==403
    assert client.get('/admin/api/identity/integrations?branch_id=11').status_code==403
    authorizer.assert_not_called()


def test_diagnostics_are_sanitized_and_stale_check_does_not_replace_current(identities):
    service=identities.service;old={'user_token':'old-token'};new={'user_token':'new-token'}
    service.save_integration('north','11','yclients',old)
    check={'connected':True,'code':'ready','message':'private-payload','secret':'private-token','checks':[{'key':'records','state':'success','code':'verified','raw':'private'}]}
    service.record_integration_check('north','11','yclients',check,old)
    assert 'private' not in json.dumps(service.integration_status('north','11'))
    service.save_integration('north','11','yclients',new)
    assert service.record_integration_check('north','11','yclients',check,old) is False
    assert 'check' not in service.integration_status('north','11')['yclients']


def test_chatpush_old_account_check_is_hidden_and_no_longer_available(identities):
    state=identities;state.service.save_integration('north','11','chatpush',{'token':'offline-private-token'})
    with state.product.connect() as db:
        db.execute('INSERT INTO integration_checks VALUES(?,?,?,?)',('11','chatpush',json.dumps({'connected':True,'code':'ready'}),1))
    result=web(state).post('/admin/api/identity/integrations/chatpush/check',json={'branch_id':'11'})
    assert result.status_code==404
    assert 'check' not in state.service.integration_status('north','11')['chatpush']


def test_chatpush_token_only_save_is_encrypted_scoped_and_never_sends(identities,monkeypatch):
    network=Mock(side_effect=AssertionError('Saving credentials must not call provider'))
    monkeypatch.setattr('requests.sessions.Session.request',network)
    client=web(identities)
    result=client.post('/admin/api/identity/integrations/chatpush',json={'branch_id':'11','credentials':{'token':'opaque-offline-token'}})
    assert result.status_code==200 and result.json=={'configured':True}
    assert identities.service.integration_credentials('11','chatpush')=={'token':'opaque-offline-token'}
    result=client.get('/admin/api/identity/integrations?branch_id=11')
    assert result.json['integrations']['chatpush']['configured'] and 'scope' not in result.get_data(as_text=True)
    assert 'opaque-offline-token' not in result.get_data(as_text=True)
    with identities.product.connect() as db:
        ciphertext=db.execute("SELECT ciphertext FROM identity_integrations WHERE branch_id='11' AND kind='chatpush'").fetchone()['ciphertext']
    assert 'opaque-offline-token' not in ciphertext
    assert client.post('/admin/api/identity/integrations/chatpush',json={'branch_id':'12','credentials':{'token':'foreign'}}).status_code==403
    assert client.post('/admin/api/identity/integrations/chatpush',json={'branch_id':'11','credentials':{'token':'opaque','scope':'obsolete'}}).status_code==400
    network.assert_not_called()


def test_chatpush_old_encrypted_credentials_keep_token_and_ignore_setup_options(identities):
    state=identities;legacy={'token':'offline-existing-token','scope':'obsolete','idempotency_key':'obsolete'}
    encrypted=state.service._fernet().encrypt(json.dumps(legacy).encode()).decode()
    with state.product.connect() as db:
        db.execute('INSERT INTO identity_integrations VALUES(?,?,?,?)',('11','chatpush',encrypted,1))
    assert state.service.integration_credentials('11','chatpush')=={'token':'offline-existing-token'}
