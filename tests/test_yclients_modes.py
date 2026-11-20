"""Distinct registration/auth modes and a private, scoped connected account."""
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask
import pytest

from identity_web import create_blueprint
from yclients_marketplace_web import create_blueprint as marketplace_blueprint
from yclients_connection import YClientsConnector
from storage.sql_documents import read_document, write_document
from test_identity_product import identities, PASSWORD, PHONE
from test_yclients_marketplace import marketplace


def site(state):
    app=Flask(__name__,template_folder=str(Path(__file__).resolve().parents[1]/'templates'))
    app.secret_key='offline-marketplace-session-secret'
    authorizer=Mock(return_value={'user_token':'synthetic-user-token'})
    provider=lambda:{'service':state.identity.service,'auth':state.identity.auth,'branches':state.identity.branches,
        'billing':state.identity.billing,'marketplace':state.service,'authorize_yclients':authorizer}
    app.register_blueprint(create_blueprint(provider));app.register_blueprint(marketplace_blueprint(provider))
    app.jinja_env.globals['csrf_token']=lambda:'offline-csrf'
    return app.test_client(),authorizer


def sign_in(client,state,username):
    with client.application.app_context(),client.session_transaction() as session:
        session.update(admin_logged_in=True,admin_username=username,
            admin_auth_version=state.identity.auth._session_fingerprint(state.identity.auth.get_admin(username)))


def test_auth_captures_only_documented_account_fields():
    http=Mock();http.post.return_value=SimpleNamespace(status_code=201,json=lambda:{'success':True,'data':{
        'id':123,'name':'Sample YC user','email':'user@example.invalid','phone':'+15550100222','login':'user@example.invalid',
        'user_token':'synthetic-user-token','avatar':'https://example.invalid/avatar','password':'must-not-persist'}})
    result=YClientsConnector(http,partner_token='synthetic-partner').authorize('user@example.invalid','password')
    assert result['account']=={'id':'123','name':'Sample YC user','email':'user@example.invalid','phone':'+15550100222','login':'user@example.invalid'}
    assert 'must-not-persist' not in json.dumps(result) and 'avatar' not in result['account']


def test_account_profile_is_encrypted_and_not_visible_to_another_branch(identities):
    state=identities;account={'id':777,'name':'Private profile name','email':'private-profile@example.invalid'}
    state.service.save_integration('north','11','yclients',{'user_token':'synthetic-token'},verified_direct=True,account=account)
    status=state.service.integration_status('north','11')['yclients']
    assert status['mode']=='direct' and status['manual_allowed']
    assert status['account']['name']==account['name']
    with state.product.connect() as db:
        raw=db.execute('SELECT ciphertext FROM integration_profiles').fetchone()[0]
        assert account['name'] not in raw and account['email'] not in raw
    with pytest.raises(PermissionError):state.service.integration_status('north','12')
    state.service.save_integration('north','11','yclients',{'user_token':'different-token'},verified_direct=True)
    assert 'account' not in state.service.integration_status('north','11')['yclients']


def test_unsigned_marketplace_handoff_cannot_create_an_account_or_connect_the_system_user(marketplace):
    state=marketplace;client,authorizer=site(state)
    assert client.get('/yclients/marketplace/connect?salon_id=99').status_code==303
    assert client.get('/register-branch').location.endswith('/yclients/marketplace')
    details={'branch_name':'From marketplace','name':'New admin','phone':PHONE}
    assert client.post('/register-branch',json={**details,'branch_id':'98'}).status_code==400
    assert client.post('/register-branch',json=details).status_code==400
    assert state.identity.branches.get_branch('99') is None
    with state.service.store.connect() as db:
        assert not db.execute('SELECT 1 FROM branch_registrations').fetchone()
    authorizer.assert_not_called()
    state.api.install.assert_not_called()


def test_expired_handoff_cannot_silently_become_manual_registration(marketplace):
    state=marketplace;client,_=site(state)
    client.get('/yclients/marketplace/connect?salon_id=99')
    state.identity.clock[0]+=3601
    result=client.post('/register-branch',json={'branch_id':99,'branch_name':'Expired','name':'Name','phone':PHONE})
    assert result.status_code==400
