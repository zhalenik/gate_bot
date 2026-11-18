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
