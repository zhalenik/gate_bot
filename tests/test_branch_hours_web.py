"""Working-hours API uses existing scope and CSRF, with temporary SQL state."""
import pytest
from storage.sql_documents import DocumentStore,migrate_legacy
from test_web_security import web,login,post


@pytest.fixture
def hours_web(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    web.module.product_services()
    return web


def test_hours_api_scoped_csrf_and_schedule_persists_without_pass_mutation(hours_web):
    web=hours_web;client=web.app.test_client();path='/admin/api/branches/12/access-hours'
    assert client.get(path).status_code==401
    csrf=login(client)
    assert client.get('/admin/api/branches/123/access-hours').status_code==403
    before=web.links.load_links()
    assert client.post(path,json={'schedule':{'enabled':True,'days':{}}}).status_code==403
    saved=post(client,csrf,path,{'schedule':{'enabled':True,'days':{}}})
    assert saved.status_code==200 and saved.json['state']['open'] is False
    assert client.get(path).json['schedule']['enabled']
    assert web.links.load_links()==before and web.calls==[]
    invalid=post(client,csrf,path,{'schedule':{'enabled':True,'days':{'0':[{'start':'oops','end':'18:00'}]}}})
    assert invalid.status_code==400
    assert client.get(path).json['schedule']['days']['0']==[]
    login(client,'super')
    assert client.get('/admin/api/branches/123/access-hours').status_code==200


def test_notification_switch_scope_and_partial_update(hours_web):
    web=hours_web
    client=web.app.test_client()
    token=login(client)
    path='/admin/api/branches/{}/notification-settings'
    setting={'auto_approve_pass_requests': True}
    assert client.post(path.format(12),json=setting).status_code==403
    assert post(client,token,path.format(123),setting).status_code==403
    assert post(client,token,path.format(12),{'modem_ids': []}).status_code==400
    assert post(client,token,path.format(12),{'auto_approve_pass_requests': '1'}).status_code==400
    buttons=web.branches.get_branch(12).buttons
    assert post(client,token,path.format(12),setting).status_code==200
    updated=web.branches.get_branch(12)
    assert updated.auto_approve_pass_requests and not web.branches.get_branch(123).auto_approve_pass_requests
    assert updated.buttons==buttons
    web.links.revoke_link(web.token_a)
    request=post(client,token,'/api/pass/'+web.token_a+'/desk',{'kind':'request','key':'tab-request-one'})
    assert request.status_code==200 and request.json['messages'][0]['approved']
    assert web.links.verify_token(web.token_a) and not web.calls
