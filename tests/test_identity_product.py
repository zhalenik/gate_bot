"""Offline acceptance for identities, invitations, migration and account isolation."""
from registration_samples import agreements
import json
import re
from pathlib import Path
from types import SimpleNamespace
from flask import Flask, session
import pytest

from admin_auth import AdminAuth
from branch_manager import BranchManager
from identity_service import IdentityService
from link_manager import LinkManager, access_is_active, set_notify_hook
from storage.json_store import JsonStore
from storage.product_store import ProductStore
from storage.sql_documents import migrate_legacy, read_document, write_document
from billing_service import BillingService


PASSWORD='offline-example-password-123'
PHONE='+15550100101'


@pytest.fixture
def identities(tmp_path,monkeypatch):
    from storage import config
    # This fixture owns a synthetic store and a recording delivery transport.
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    branches={'11':{'branch_id':11,'name':'North','buttons':[]},'12':{'branch_id':12,'name':'South','buttons':[]}}
    admins={'owner':{'name':'Owner','password_hash':AdminAuth.hash_password(PASSWORD),'is_super':True,'branch_id':None,'session_version':1},
            'north':{'name':'North admin','password_hash':AdminAuth.hash_password(PASSWORD),'is_super':False,'branch_id':11,'session_version':1}}
    links={'example-client':{'branch_id':11,'user_name':'Example','phone':PHONE,'gate_access':True,'access_expires':None,'mailing_enabled':True,'created':'2024-01-01T12:00:00+00:00'}}
    for name,data in [('branches.json',branches),('admins.json',admins),('links.json',links)]:JsonStore(tmp_path/name).replace(data)
    originals={name:(tmp_path/name).read_bytes() for name in ('branches.json','admins.json','links.json')}
    assert migrate_legacy(tmp_path)['status']=='preview'
    migrate_legacy(tmp_path,activate=True)
    product=ProductStore(tmp_path/'application.sqlite3');billing=BillingService(product)
    sent=[];clock=[2000000000.0]
    def send(channel,target,text,context):sent.append((channel,target,text,context));return {'status':'sent'}
    service=IdentityService(product,send,lambda:clock[0]);service.import_legacy()
    service.attach_console_account('owner','+15550100999')
    service.attach_console_account('north','+15550100888')
    return SimpleNamespace(service=service,product=product,billing=billing,path=tmp_path,sent=sent,clock=clock,originals=originals,
        links=LinkManager(tmp_path/'links.json'),branches=BranchManager(tmp_path/'branches.json'),auth=AdminAuth(str(tmp_path/'admins.json')))


def accept(state,username='new-admin@example.invalid',client=True,branch='11',phone=PHONE):
    result=state.service.invite('owner',branch,client_token='example-client' if client else None,phone=phone)
    token=result['link'].rsplit('/',1)[1]
    otp=state.service.request_otp(token,ip='127.0.0.1', documents=agreements(state.service))
    code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    result=state.service.accept_invitation(token,otp['challenge_id'],code,'Example admin',username,PASSWORD, documents=agreements(state.service))
    return result,token,otp,code


def test_invitation_acceptance_emits_event_once_inside_transaction(identities):
    events=[]
    identities.service.event_sink=lambda *args: events.append(args)
    result,token,otp,code=accept(identities)
    assert len(events)==1 and events[0][0:2]==('invitation_accepted','11')
    with pytest.raises(ValueError):
        identities.service.accept_invitation(token,otp['challenge_id'],code,'Again','other',PASSWORD, documents=agreements(identities.service))
    assert len(events)==1


def test_sql_migration_preserves_original_bytes_and_opaque_tokens(identities):
    state=identities
    assert migrate_legacy(state.path,activate=True)['status']=='already_active'
    for name,value in state.originals.items():assert (state.path/name).read_bytes()==value
    before=state.links.load_links()['example-client']
    assert before['phone']==PHONE and before['person_id']
    state.links.update_user('example-client',{'user_name':'Updated'})
    assert state.links.load_links()['example-client']['user_name']=='Updated'
    assert (state.path/'links.json').read_bytes()==state.originals['links.json']
    with state.product.connect() as db:
        assert db.execute('SELECT count(*) FROM client_branch_events WHERE branch_client_id="example-client"').fetchone()[0]==1
        assert db.execute('SELECT created FROM client_branch_events WHERE branch_client_id="example-client"').fetchone()[0]<state.clock[0]


def test_invitation_promotes_same_client_and_revocation_preserves_manual_grant(identities):
    state=identities;before=set(state.links.load_links());result,token,otp,code=accept(state)
    assert set(state.links.load_links())==before
    client=state.links.load_links()['example-client'];assert client['admin_role_active'] and access_is_active(client)
    assert state.auth.verify_admin('new-admin@example.invalid',PASSWORD)
    with pytest.raises(ValueError):state.service.accept_invitation(token,otp['challenge_id'],code,'Again','other',PASSWORD, documents=agreements(state.service))
    state.service.revoke_admin('owner',result['person_id'],'11')
    client=state.links.load_links()['example-client'];assert not client['admin_role_active'] and client['gate_access'] and access_is_active(client)
    assert not state.auth.verify_admin('new-admin@example.invalid',PASSWORD)
    state.service.import_legacy()
    assert not state.auth.verify_admin('new-admin@example.invalid',PASSWORD)


def test_tenant_scope_inviter_revocation_expiry_and_otp_attempt_budget(identities):
    state=identities
    with pytest.raises(PermissionError):state.service.invite('north','12',phone=PHONE)
    with pytest.raises(ValueError):state.service.invite('owner','11',phone=PHONE,channel='telegram')
    result=state.service.invite('north','11',phone=PHONE);token=result['link'].rsplit('/',1)[1]
    otp=state.service.request_otp(token,ip='127.0.0.1', documents=agreements(state.service));correct=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    wrong='999999' if correct!='999999' else '000000'
    for _ in range(5):
        with pytest.raises(ValueError):state.service.accept_invitation(token,otp['challenge_id'],wrong,'Test','new',PASSWORD, documents=agreements(state.service))
    with pytest.raises(ValueError):state.service.accept_invitation(token,otp['challenge_id'],correct,'Test','new',PASSWORD, documents=agreements(state.service))
    with pytest.raises(ValueError):state.service.request_otp(token,ip='127.0.0.1', documents=agreements(state.service))
    state.clock[0]+=3601
    with pytest.raises(ValueError):state.service.invitation_info(token)


def test_dry_run_otp_never_completes_registration(identities):
    state=identities;state.service.sender=lambda *args:{'status':'success','delivery':'simulated'}
    reg=state.service.register_branch('99','Example','New owner',PHONE)
    otp=state.service.request_otp(reg['registration_token'],registration=True, documents=agreements(state.service))
    assert not otp['ready']
    with pytest.raises(ValueError):state.service.complete_registration(reg['registration_token'],otp['challenge_id'],'000000','new',PASSWORD, documents=agreements(state.service))
    assert state.branches.get_branch('99') is None


def test_sms_confirmed_manual_registration_immediately_activates_branch_and_trial(identities):
    state=identities;reg=state.service.register_branch('99','Example','New owner',PHONE)
    otp=state.service.request_otp(reg['registration_token'],registration=True, documents=agreements(state.service));code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    result=state.service.complete_registration(reg['registration_token'],otp['challenge_id'],code,'new-branch@example.invalid',PASSWORD, documents=agreements(state.service))
    assert result['status']=='active' and state.branches.get_branch('99').enabled
    assert state.auth.verify_admin('new-branch@example.invalid',PASSWORD)
    assert state.service.web_admin(result['username'])['branch_ids']==['99']
    assert not state.service.integration_credentials('99','yclients')
    with state.product.connect() as db:
        row=db.execute('SELECT * FROM billing_terms WHERE branch_id="99" AND source="trial"').fetchone()
        assert row is not None and row['source']=='trial' and row['quota'] is None


def test_bearer_client_binding_cannot_elevate_and_verified_ticket_is_one_time(identities):
    state=identities;accepted,_,_,_=accept(state)
    state.links.bind_identity('telegram','55101',token='example-client')
    assert state.service.bot_admin('telegram','55101') is None
    ticket=state.service.issue_bind_ticket('new-admin@example.invalid','telegram')
    with pytest.raises(ValueError):state.service.bind_ticket('max','55101',ticket)
    state.service.bind_ticket('telegram','55101',ticket)
    assert state.service.bot_admin('telegram','55101')['person_id']==accepted['person_id']
    with pytest.raises(ValueError):state.service.bind_ticket('telegram','55101',ticket)
    state.links.update_user('example-client',{'phone':'+15550100777'})
    assert state.service.bot_admin('telegram','55101') is None
    with pytest.raises(ValueError):state.service.issue_bind_ticket('new-admin@example.invalid','telegram')


def test_disabling_account_removes_role_grant_without_touching_manual_rights(identities):
    state=identities;accepted,_,_,_=accept(state)
    with state.auth.store.transaction() as admins:admins['new-admin@example.invalid']['disabled']=True
    client=state.links.load_links()['example-client']
    assert client['admin_role_active'] is False and client['gate_access'] is True
    assert not state.auth.verify_admin('new-admin@example.invalid',PASSWORD)


def test_existing_account_cannot_accept_invitation_to_another_branch(identities):
    state=identities
    second=state.links.create_link({'branch_id':12,'user_name':'Same owner','phone':PHONE})
    invite=state.service.invite('owner','12',client_token=second)
    accepted,_,_,_=accept(state);before=state.links.load_links();state.clock[0]+=61
    raw=invite['link'].rsplit('/',1)[1];otp=state.service.request_otp(raw, documents=agreements(state.service))
    code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    with pytest.raises(ValueError,match='другим филиалом'):
        state.service.accept_invitation(raw,otp['challenge_id'],code,'Existing','new-admin@example.invalid',PASSWORD,existing_password=PASSWORD, documents=agreements(state.service))
    assert state.links.load_links()==before


def test_preferences_survive_expired_grant_and_never_enable_gate(identities):
    state=identities;link=state.service.preferences_link_for_token('example-client');token=link.rsplit('/',1)[1]
    state.links.set_expiry_at('example-client','2020-01-01T00:00:00+00:00')
    state.links.cleanup_expired_accesses();state.links.cleanup_guest_users()
    result=state.service.save_prefs_for_token(token,{'enabled':True,'email':'offline@example.invalid','channels':{'email':True}})
    assert result['enabled'] and not result['email_verified']
    assert state.links.verify_token('example-client') is None
    assert state.service.admin_preferences('north')['events'].get('sms_received') is None
    state.service.save_admin_preferences('north',state.service.admin_preferences('north'))


def test_shared_transaction_rolls_back_state_and_outbox_hook(identities):
    state=identities
    def fail(payload):
        with state.product.connect() as db:
            assert not read_document(db,'links.json')['example-client']['gate_access']
        raise RuntimeError('synthetic outbox failure')
    set_notify_hook(fail)
    try:
        with pytest.raises(RuntimeError):state.links.toggle_user_access('example-client',False)
        assert state.links.load_links()['example-client']['gate_access']
    finally:set_notify_hook(None)


def test_integration_encryption_and_admin_email_verification(identities):
    state=identities;secret='offline-test-user-token'
    state.service.save_integration('north','11','yclients',{'user_token':secret})
    assert state.service.integration_credentials('11','yclients')['user_token']==secret
    assert secret not in json.dumps(state.service.integration_status('north','11'))
    with state.product.connect() as db:assert secret not in db.execute('SELECT ciphertext FROM identity_integrations').fetchone()[0]
    with pytest.raises(PermissionError):state.service.integration_status('north','12')
    state.service.admin_email('owner','owner@example.invalid',verify=True)
    assert not state.service.admin_preferences('owner')['email_verified']
    token=state.sent[-1][2].rsplit('/',1)[1];state.service.verify_email(token)
    assert state.service.admin_preferences('owner')['email_verified']
    state.service.save_admin_preferences('owner', {'events': {'gate_requested': {'email': True}}})
    assert any(c['channel']=='email' for c in state.service.contacts_for_admin_events('12','gate_requested'))


def test_existing_secondary_membership_cannot_expand_primary_scope(identities):
    state=identities;accepted,_,_,_=accept(state)
    with state.product.connect() as db:
        db.execute('INSERT INTO branch_memberships VALUES(?,?,"active",?)',(accepted['person_id'],'12',state.clock[0]))
    app=Flask(__name__);app.secret_key='offline-secret'
    with app.test_request_context():
        assert state.auth.login_admin('new-admin@example.invalid',PASSWORD)
        session['active_branch_id']='12'
        assert state.auth.get_current_admin()['branch_id']==11
        assert state.service.web_admin('new-admin@example.invalid')['branch_ids']==['11']
        with pytest.raises(PermissionError):state.service.list_admins('new-admin@example.invalid','12')
        state.service.revoke_admin('owner',accepted['person_id'],'11')
        assert state.auth.get_current_admin() is None


def test_password_reset_invalidates_unconsumed_elevated_binding_ticket(identities):
    state=identities;ticket=state.service.issue_bind_ticket('owner','max')
    with state.auth.store.transaction() as admins:admins['owner']['password_hash']=AdminAuth.hash_password('changed-offline-password')
    with pytest.raises(ValueError):state.service.bind_ticket('max','55101',ticket)


def test_phone_less_legacy_client_is_retained_without_new_bot_identity(identities):
    state=identities
    with state.links.store.transaction() as links:links['quarantined']={'branch_id':11,'phone':'','user_name':'Legacy','gate_access':False,'access_expires':None}
    state.service.import_legacy()
    assert 'quarantined' in state.links.load_links()
    with pytest.raises(ValueError):state.links.bind_identity('max','55101',token='quarantined')
    with pytest.raises(ValueError):state.service.invite('owner','11',client_token='quarantined')


def test_yclients_connection_contract_and_no_redirect_or_secret_error_leak():
    from yclients_connection import YClientsConnector
    calls=[];responses=[SimpleNamespace(status_code=200,json=lambda:{'success':True,'data':{'uuid':'test-uuid-123','flow':'login'}}),
        SimpleNamespace(status_code=201,json=lambda:{'user_token':'offline-user-token'})]
    class Fake:
        def post(self,url,**kwargs):calls.append((url,kwargs));return responses.pop(0)
    connector=YClientsConnector(Fake(),partner_token='offline-partner')
    first=connector.authorize('name@example.invalid','password')
    second=connector.authorize('name@example.invalid','password','123456',first['challenge'])
    assert second['user_token']=='offline-user-token'
    assert calls[1][1]['json']['2fa']=={'uuid':'test-uuid-123','secret':'123456'}
    assert calls[0][1]['allow_redirects'] is False
    with pytest.raises(ValueError):YClientsConnector(base_url='https://example.invalid/api/v1')


def test_blueprint_invitation_and_connection_session_are_scoped(identities):
    from identity_web import create_blueprint
    state=identities;calls=[]
    def authorize(login,password,code=None,challenge=None):
        calls.append((login,password,code,challenge))
        return {'requires_2fa':True,'challenge':'offline-uuid-123'} if not code else {'user_token':'offline-user-token'}
    app=Flask(__name__,template_folder=str(Path(__file__).resolve().parents[1]/'templates'));app.secret_key='offline-session-secret'
    app.register_blueprint(create_blueprint(lambda:{'service':state.service,'auth':state.auth,'branches':state.branches,'billing':state.billing,'authorize_yclients':authorize,'check_yclients':lambda branch,credentials:{'status':'success','connected':True,'code':'ready','checks':[]}}))
    from legal_documents_web import create_blueprint as legal_blueprint
    from legal_documents import ADMIN_DOCS,current
    app.register_blueprint(legal_blueprint(lambda:{'service':state.service}))
    with state.product.connect() as db:
        documents={d['kind']:d['version'] for d in current(db,ADMIN_DOCS)}
    app.jinja_env.globals['csrf_token']=lambda:'offline-csrf'
    client=app.test_client()
    with app.app_context():fingerprint=state.auth._session_fingerprint(state.auth.get_admin('owner'))
    with client.session_transaction() as cookie:
        cookie.update(admin_logged_in=True,admin_username='owner',admin_auth_version=fingerprint)
    created=client.post('/admin/invitations/new',json={'branch_id':'11','phone':PHONE}).get_json()
    assert created['status']=='success'
    raw=created['link'].rsplit('/',1)[1]
    assert client.get('/invite/'+raw).status_code==200
    otp=client.post('/invite/'+raw+'/code',json={'documents':documents}).get_json();code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    accepted=client.post('/invite/'+raw+'/accept',json={'documents':documents,'challenge_id':otp['challenge_id'],'code':code,'username':'web-invite@example.invalid','password':PASSWORD,'name':'Web example'})
    assert accepted.status_code==200
    payload={'branch_id':'11','login':'example@example.invalid','password':'offline-password'}
    assert client.post('/admin/api/identity/integrations/yclients/authorize',json=payload).get_json()['requires_2fa']
    with client.session_transaction() as cookie:
        assert 'password' not in json.dumps(dict(cookie))
        assert cookie['yclients_connection']['branch_id']=='11'
    assert client.post('/admin/api/identity/integrations/yclients/authorize',json={**payload,'branch_id':'12','code':'123456'}).status_code==400
    response=client.post('/admin/api/identity/integrations/yclients/authorize',json={**payload,'code':'123456'})
    assert response.status_code==200 and response.get_json()['configured']
    assert calls[-1][3]=='offline-uuid-123'
    assert 'offline-user-token' not in response.get_data(as_text=True)
