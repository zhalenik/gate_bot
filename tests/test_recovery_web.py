"""The real Flask boundary retains CSRF and never logs in on an SMS proof."""
import re
from test_web_security import web,csrf,post,TEST_PASSWORD
from storage.sql_documents import migrate_legacy,DocumentStore


def prepare(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    identity.attach_console_account('tenant-a','+15550400101')
    sent=[];identity.sender=lambda *args:sent.append(args) or {'status':'success','delivery':'queued'}
    return identity,sent

def test_recovery_http_csrf_limited_session_and_phone_proof(web,tmp_path,monkeypatch):
    identity,sent=prepare(web,tmp_path,monkeypatch)
    client=web.app.test_client();token=csrf(client,'/admin/recover')
    assert client.post('/admin/recover/code',json={'phone':'+15550400101'}).status_code==403
    requested=post(client,token,'/admin/recover/code',{'phone':'+15550400101'}).json
    code=re.search(r'\b[0-9]{6}\b',sent[-1][2]).group()
    thief=web.app.test_client();thief_token=csrf(thief,'/admin/recover')
    assert post(thief,thief_token,'/admin/recover/verify',{'request_token':requested['request_token'],'code':code}).status_code==400
    assert post(client,token,'/admin/recover/verify',{'request_token':requested['request_token'],'code':code}).status_code==200
    assert client.get('/admin/api/users').status_code==401
    with client.session_transaction() as cookie:
        assert cookie.get('password_reset_token') and not cookie.get('admin_logged_in')
    new='offline-reset-password-123'
    assert post(client,token,'/admin/recover/complete',{'password':new,'confirmation':new}).status_code==200
    assert client.get('/admin/api/users').status_code==401
    assert web.auth.verify_admin('tenant-a',new) and not web.auth.verify_admin('tenant-a',TEST_PASSWORD)
    assert post(client,token,'/admin/recover/complete',{'password':new,'confirmation':new}).status_code==400
    assert web.calls==[]

def test_primary_branch_cannot_be_expanded_by_switch_endpoint(web,tmp_path,monkeypatch):
    from test_web_security import login
    identity,sent=prepare(web,tmp_path,monkeypatch)
    account=identity.web_admin('tenant-a')
    with identity.product.connect() as db:
        db.execute('INSERT INTO branch_memberships VALUES(?,?,"active",?)',(account['person_id'],'123',identity.clock()))
    client=web.app.test_client();token=login(client)
    assert post(client,token,'/admin/api/identity/branch',{'branch_id':'123'}).status_code==403
    assert post(client,token,'/admin/invitations/new',{'branch_id':'123','phone':'+15550001111'}).status_code==403
    assert {str(row['id']) for row in client.get('/admin/api/branches').json['branches']}=={'12'}
