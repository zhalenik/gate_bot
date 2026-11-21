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


def test_portable_settings_include_credentials_without_printing_them(system_web):
    web, store = system_web
    client = web.app.test_client()
    route = '/admin/api/system-settings/'
    key = 'synthetic-backup-passphrase'
    csrf = login(client, 'tenant-a')
    assert post(client, csrf, route+'export', {'password':key}).status_code == 403
    csrf = login(client, 'super')
    archive = post(client, csrf, route+'export', {'password':key})
    assert archive.status_code == 200 and 'old-synthetic-token' not in archive.text
    document = archive.json
    document['values']['PASS_LINK_TEMPLATE'] = 'Въезд\n{link}'
    restore = {'document':document, 'revision':'0', 'password':'incorrect-password'}
    assert post(client, csrf, route+'import', restore).status_code == 400
    assert not store.path.exists()
    restore['password'] = key
    assert post(client, csrf, route+'import', restore).status_code == 200
    values = store.read()['values']
    assert values['BOT_TOKEN'] == 'old-synthetic-token'
    assert values['PASS_LINK_TEMPLATE'] == 'Въезд\n{link}'
    assert post(client, csrf, route+'import', restore).status_code == 400


def test_revoked_role_and_stale_revision_cannot_write(system_web):
    web,store=system_web;client=web.app.test_client();csrf=login(client,'super')
    assert post(client,csrf,'/admin/api/system-settings',{'revision':'0','values':{'PORT':5001}}).status_code==200
    assert post(client,csrf,'/admin/api/system-settings',{'revision':'0','values':{'PORT':5002}}).status_code==400
    with web.auth.store.transaction() as admins:admins['super']['disabled']=True
    assert post(client,csrf,'/admin/api/system-settings',{'revision':store.read()['revision'],'values':{'PORT':5003}}).status_code in {401,403}
    assert store.read()['values']['PORT']=='5001'


def test_restart_after_save_and_private_yaml(system_web, monkeypatch):
    from unittest.mock import Mock
    from wsgiref.util import is_hop_by_hop
    import runtime_control
    web,store=system_web
    client=web.app.test_client();csrf=login(client,'tenant-a')
    route='/admin/api/system-settings/'
    assert client.get(route+'compose.yaml').status_code==403
    assert post(client,csrf,route+'restart',{'revision':'0'}).status_code==403
    csrf=login(client,'super')
    callback=Mock()
    monkeypatch.setattr(runtime_control,'_restart',callback)
    reply=post(client,csrf,'/admin/api/system-settings',{'revision':'0','values':{
        'TELEGRAM_PROXY_URL':'http://name:pass@proxy.invalid:8080','REQUESTS_CA_BUNDLE':'/state/ca.pem'}})
    assert reply.status_code==200 and 'name:pass' not in reply.get_data(as_text=True)
    assert post(client,csrf,route+'restart',{'revision':'0'}).status_code==409
    result=post(client,csrf,route+'restart',{'revision':store.read()['revision']})
    assert result.status_code==202
    assert not any(is_hop_by_hop(name) for name in result.headers.keys())
    result.close()
    callback.assert_called_once()
    yaml=client.get(route+'compose.yaml?network=private-vpn')
    assert yaml.status_code==200 and 'private-vpn' in yaml.text and 'name:pass' not in yaml.text


def test_production_hides_development_sms_but_preserves_values(system_web):
    web, store = system_web
    client = web.app.test_client()
    csrf = login(client, 'super')
    path = '/admin/api/system-settings'
    phone = '+15550999999'
    saved = post(client, csrf, path, {'revision':'0','values':{
        'DEV_REGISTRATION_SMS_PHONE':phone,'GATEBOT_ENV':'production'}})
    assert saved.status_code == 200 and phone not in saved.text
    assert not any(field.get('development_only') for field in client.get(path).json['settings']['fields'])
    preview = client.get(path+'?edit_environment=development')
    assert any(field.get('value') == phone for field in preview.json['settings']['fields'])
    assert store.read()['values']['GATEBOT_ENV'] == 'production'


@pytest.mark.parametrize('target,key', [('session','SECRET_KEY'),('telegram','TELEGRAM_WEBHOOK_SECRET'),('max','MAX_WEBHOOK_SECRET')])
def test_rotate_is_private_scoped_and_restart_uses_selected_mode(system_web,monkeypatch,target,key):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from bot_webhooks import EXTENSION
    web,store=system_web
    callback=Mock()
    monkeypatch.setattr('runtime_control._restart',callback)
    client=web.app.test_client()
    route='/admin/api/system-settings/rotate/'+target
    payload={'revision':'0'}
    assert post(client,login(client,'tenant-a'),route,payload).status_code==403
    csrf=login(client,'super')
    assert client.post(route,json=payload).status_code==403
    response=post(client,csrf,route,payload)
    first=store.read()['values'][key]
    assert len(first)>=48 and first not in response.text and first.encode() not in store.path.read_bytes()
    assert response.status_code==(202 if target=='session' else 200)
    response.close()
    assert callback.call_count==(1 if target=='session' else 0)
    assert post(client,csrf,route,payload).status_code==400
    if target=='session':
        return
    prefix='BOT' if target=='telegram' else 'MAX_BOT'
    values={prefix+'_ENABLED':True,prefix+'_TOKEN':'synthetic-token',prefix+'_MODE':'webhook','BASE_URL':'https://example.invalid'}
    payload={'revision':store.read()['revision'],'values':values}
    response=post(client,csrf,route,payload)
    second=store.read()['values'][key]
    assert response.status_code==202 and second!=first and second not in response.text
    assert response.json['settings']['webhooks'][target]['state']=='pending_restart'
    response.close()
    callback.assert_called_once()
    monkeypatch.setattr(config,'SYSTEM_ACTIVE_ENVIRONMENT',{**config.SYSTEM_BASE_ENVIRONMENT,**store.read()['values']})
    read=lambda: client.get('/admin/api/system-settings').json['settings']['webhooks'][target]
    assert read()['state']=='unconfirmed'
    monkeypatch.setitem(web.app.extensions,EXTENSION,{target:SimpleNamespace(registration_confirmed=True)})
    assert read()['state']=='registered'
    monkeypatch.setitem(web.app.extensions,'gatebot_bot_startup_errors',{target:{'error_code':'forbidden','action':'Проверьте права'}})
    assert read()['state']=='error' and 'forbidden' in read()['message']
    assert not web.calls


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
