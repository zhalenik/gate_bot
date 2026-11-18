"""Real auth/CSRF and temporary bootstrap state; no external calls."""
import pytest

from storage import config
from storage.sql_documents import DocumentStore,migrate_legacy
from system_config_bootstrap import SystemConfigStore
from test_web_security import web,login,post


@pytest.fixture
def system_web(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    service=web.module.product_services().identity
    service.attach_console_account('super','+15550100200')
    monkeypatch.setattr('system_settings.SystemConfigStore',lambda **kwargs:SystemConfigStore(tmp_path/'system-config'))
    base={'GATEBOT_ENV':'development','SECRET_KEY':'synthetic-session-key-at-least-32-characters','BOT_TOKEN':'old-synthetic-token'}
    monkeypatch.setattr(config,'SYSTEM_BASE_ENVIRONMENT',dict(base));monkeypatch.setattr(config,'SYSTEM_ACTIVE_ENVIRONMENT',dict(base))
    for key in ('MAILING_SMTP_SERVER','MAILING_SMTP_USER','MAILING_SMTP_PASSWORD','MAILING_SMTP_FROM_EMAIL'):
        monkeypatch.setattr(config,key,'')
    return web,SystemConfigStore(tmp_path/'system-config')


def test_only_super_can_read_save_and_csrf_is_required(system_web):
    web,store=system_web;client=web.app.test_client()
    assert client.get('/admin/api/system-settings').status_code==401
    csrf=login(client,'tenant-a')
    assert client.get('/admin/system-settings').status_code==403
    assert client.get('/admin/api/system-settings').status_code==403
    assert post(client,csrf,'/admin/api/system-settings',{'revision':'0','values':{'PORT':5001}}).status_code==403
    csrf=login(client,'super')
    assert client.get('/admin/system-settings').status_code==200
    assert client.post('/admin/api/system-settings',json={'revision':'0','values':{'PORT':5001}}).status_code==403
    saved=post(client,csrf,'/admin/api/system-settings',{'revision':'0','values':{'PORT':5001,'BOT_TOKEN':'new-synthetic-token'}})
    assert saved.status_code==200 and 'new-synthetic-token' not in saved.get_data(as_text=True)
    assert 'old-synthetic-token' not in saved.get_data(as_text=True)
    assert saved.headers['Cache-Control']=='no-store'
    assert store.read()['values']['PORT']=='5001' and web.calls==[]


def test_revoked_role_and_stale_revision_cannot_write(system_web):
    web,store=system_web;client=web.app.test_client();csrf=login(client,'super')
    assert post(client,csrf,'/admin/api/system-settings',{'revision':'0','values':{'PORT':5001}}).status_code==200
    assert post(client,csrf,'/admin/api/system-settings',{'revision':'0','values':{'PORT':5002}}).status_code==400
    with web.auth.store.transaction() as admins:admins['super']['disabled']=True
    assert post(client,csrf,'/admin/api/system-settings',{'revision':store.read()['revision'],'values':{'PORT':5003}}).status_code in {401,403}
    assert store.read()['values']['PORT']=='5001'


def test_official_com_api_address_does_not_break_settings_page_or_saving(system_web,monkeypatch):
    web,store=system_web;client=web.app.test_client();csrf=login(client,'super')
    url='https://api.yclients.com/api/v1'
    monkeypatch.setitem(config.SYSTEM_BASE_ENVIRONMENT,'YC_BASE_URL',url)
    monkeypatch.setitem(config.SYSTEM_ACTIVE_ENVIRONMENT,'YC_BASE_URL',url)
    page=client.get('/admin/system-settings')
    assert page.status_code==200 and page.mimetype=='text/html'
    result=client.get('/admin/api/system-settings')
    assert result.status_code==200 and result.headers['Cache-Control']=='no-store'
    field=next(item for item in result.json['settings']['fields'] if item['key']=='YC_BASE_URL')
    assert field['value']==url
    saved=post(client,csrf,'/admin/api/system-settings',{'revision':'0','values':{'PORT':5001}})
    assert saved.status_code==200 and store.read()['values']['PORT']=='5001'
    assert web.calls==[]


def test_invalid_legacy_environment_can_be_repaired_from_the_settings_page(system_web,monkeypatch):
    web,store=system_web;client=web.app.test_client();csrf=login(client,'super')
    legacy='https://legacy.example.invalid/api/v1'
    monkeypatch.setitem(config.SYSTEM_BASE_ENVIRONMENT,'YC_BASE_URL',legacy)
    monkeypatch.setitem(config.SYSTEM_ACTIVE_ENVIRONMENT,'YC_BASE_URL',legacy)
    assert client.get('/admin/system-settings').status_code==200
    before=client.get('/admin/api/system-settings')
    assert before.status_code==200
    assert 'YC_BASE_URL' in before.json['settings']['invalid_keys']
    assert not store.directory.exists()
    unchanged=post(client,csrf,'/admin/api/system-settings',{'revision':before.json['settings']['revision'],'values':{}})
    assert unchanged.status_code==400 and not store.path.exists()
    saved=post(client,csrf,'/admin/api/system-settings',{'revision':before.json['settings']['revision'],'values':{'YC_BASE_URL':'https://api.yclients.com/api/v1'}})
    assert saved.status_code==200
    after=client.get('/admin/api/system-settings')
    assert after.status_code==200 and 'YC_BASE_URL' in after.json['settings']['restart_keys']
    assert 'YC_BASE_URL' not in after.json['settings']['invalid_keys']
    assert 'YC_BASE_URL' in after.json['settings']['active_invalid_keys']
    assert client.get('/admin/system-settings').status_code==200
    assert config.SYSTEM_ACTIVE_ENVIRONMENT['YC_BASE_URL']==legacy and web.calls==[]
