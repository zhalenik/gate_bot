"""Anonymous preference writes and CSRF renewal with isolated state only."""
import pytest

from storage.sql_documents import DocumentStore,migrate_legacy
from test_web_security import web,post


@pytest.fixture
def preferences_web(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    return web,identity


def test_status_issues_session_csrf_only_for_valid_bearer_without_changing_preferences(preferences_web):
    web,identity=preferences_web
    before=identity.prefs_for_token(web.token_a)
    client=web.app.test_client()
    bad=client.get('/preferences/missing-client/status')
    assert bad.status_code==400 and 'csrf_token' not in bad.json
    with client.session_transaction() as session:assert 'csrf_token' not in session
    response=client.get('/preferences/'+web.token_a+'/status')
    assert response.status_code==200 and response.json['preferences']==before
    token=response.json['csrf_token'];assert isinstance(token,str) and len(token)>=32
    with client.session_transaction() as session:
        assert session['csrf_token']==token and not session.get('admin_logged_in')
    repeated=client.get('/preferences/'+web.token_a+'/status')
    assert repeated.json['csrf_token']==token
    assert identity.prefs_for_token(web.token_a)==before and web.calls==[]


def test_anonymous_standalone_unsubscribe_and_explicit_resume_use_json(preferences_web):
    web,identity=preferences_web
    web.branches.update_settings(12,{'yclients_enabled':False})
    web.links.bind_identity('telegram','81001',token=web.token_a)
    web.links.bind_identity('max','81002',token=web.token_a)
    identity.save_prefs_for_token(web.token_a,{'enabled':True,'channels':{'telegram':True,'max':True}})
    before=web.links.load_links()[web.token_a]
    client=web.app.test_client();path='/preferences/'+web.token_a
    assert client.get(path).status_code==200
    status=client.get(path+'/status').json
    assert status['preferences']['allowed_channels']==['email','telegram','max']
    token=status['csrf_token']
    forged=post(client,token,path,{'enabled':True,'channels':{'messengers':True}})
    assert forged.status_code==400 and forged.is_json
    assert web.links.load_links()[web.token_a]==before
    unsubscribed=post(client,token,path,{'enabled':False,'channels':{'telegram':True,'max':True,'messengers':True}})
    assert unsubscribed.status_code==200 and unsubscribed.is_json
    assert not unsubscribed.json['preferences']['enabled']
    resumed=post(client,token,path,{'enabled':True,'channels':{'max':True}})
    assert resumed.status_code==200 and resumed.is_json
    assert resumed.json['preferences']['effective_channels']['max']
    assert not resumed.json['preferences']['effective_channels']['telegram']
    after=web.links.load_links()[web.token_a]
    for key in ('gate_access','access_expires','branch_id','phone'):assert after.get(key)==before.get(key)
    assert web.calls==[]
