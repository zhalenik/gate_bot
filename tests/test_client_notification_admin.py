"""Scoped administrative client notification editor, with fake deliveries only."""
import pytest

from client_notification_admin import ClientNotificationAdmin
from test_identity_product import identities, accept
from test_client_notification_web import preferences_web
from test_web_security import web, login, post


def test_scope_summary_and_guest_restrictions(identities):
    state=identities;service=ClientNotificationAdmin(state.service)
    state.links.bind_identity('telegram','72001',token='example-client')
    service.save('north','example-client',{'enabled':True,'channels':{'telegram':True,'max':True}})
    summary=service.summary('north','example-client')
    assert summary['effective_channels']['telegram'] and not summary['effective_channels']['max']
    foreign=state.links.create_link({'branch_id':12,'phone':'+15550100666','gate_access':True})
    for action in (lambda:service.get('north',foreign),lambda:service.save('north',foreign,{'enabled':False,'channels':{}}),
                   lambda:service.verify_email('north',foreign),lambda:service.bot_link('north',foreign,'telegram')):
        with pytest.raises(PermissionError):action()
    guest=state.links.create_link({'branch_id':11,'phone':'+15550100777','is_temporary':True,'gate_access':True})
    assert not service.summary('north',guest)['available']
    for action in (lambda:service.get('north',guest),lambda:service.save('north',guest,{'enabled':True,'channels':{'telegram':True}}),lambda:service.verify_email('north',guest),lambda:service.bot_link('north',guest,'telegram')):
        with pytest.raises(PermissionError):action()
    assert state.sent==[]


def test_standalone_modal_schema_and_one_channel_resume(identities):
    state=identities;service=ClientNotificationAdmin(state.service)
    state.branches.update_settings('11',{'yclients_enabled':False})
    state.links.bind_identity('telegram','72001',token='example-client')
    state.links.bind_identity('max','72002',token='example-client')
    prefs=service.get('north','example-client')
    assert prefs['allowed_channels']==['email','telegram','max']
    with pytest.raises(ValueError):service.save('north','example-client',{'enabled':True,'channels':{'messengers':True}})
    service.save('north','example-client',{'enabled':False,'channels':{'telegram':True,'max':True}})
    prefs=service.save('north','example-client',{'enabled':True,'channels':{'max':True}})
    assert prefs['effective_channels']['max'] and not prefs['effective_channels']['telegram']
    assert state.sent==[]


def test_admin_profile_email_guard_and_client_bot_link_assurance(identities,monkeypatch):
    state=identities;service=ClientNotificationAdmin(state.service)
    promoted,*_=accept(state);state.sent.clear()
    with pytest.raises(PermissionError):
        service.save('north','example-client',{'enabled':True,'channels':{'email':True},'email':'replacement@example.invalid'})
    result=service.save('owner','example-client',{'enabled':True,'channels':{'email':True},'email':'replacement@example.invalid'})
    assert result['email']=='replacement@example.invalid' and not result['email_verified']
    monkeypatch.setattr('storage.config.BOT_USERNAME','test_gate_bot')
    link=service.bot_link('north','example-client','telegram')
    assert link=='https://t.me/test_gate_bot?start=example-client'
    assert not state.service.bot_admin('telegram','72001')
    assert state.sent==[]


def test_admin_http_auth_csrf_scope_and_existing_email_delivery_contract(preferences_web):
    web,identity=preferences_web;client=web.app.test_client();url='/admin/api/client-notifications'
    assert client.get(url,query_string={'token':web.token_a}).status_code==401
    csrf=login(client,'tenant-a')
    assert client.get(url,query_string={'token':web.token_b}).status_code==403
    assert post(client,csrf,url,{'token':web.token_b,'enabled':False,'channels':{}}).status_code==403
    assert client.post(url,json={'token':web.token_a,'enabled':False,'channels':{}}).status_code==403
    before=web.links.load_links()[web.token_a]
    saved=post(client,csrf,url,{'token':web.token_a,'enabled':True,'channels':{'email':True},'email':'client@example.invalid'})
    assert saved.status_code==200 and saved.json['preferences']['email']=='client@example.invalid'
    calls=[];identity.sender=lambda *args:calls.append(args) or {'status':'sent'}
    sent=post(client,csrf,url+'/verify-email',{'token':web.token_a})
    assert sent.status_code==200 and calls[-1][0:2]==('email','client@example.invalid')
    assert post(client,csrf,url+'/verify-email',{'token':web.token_b}).status_code==403
    assert post(client,csrf,url+'/bot-link',{'token':web.token_b,'platform':'telegram'}).status_code==403
    assert post(client,csrf,url,{'token':web.token_a,'enabled':True,'channels':{},'username':'super'}).status_code==400
    after=web.links.load_links()[web.token_a]
    for key in ('gate_access','access_expires','branch_id','phone'):assert after.get(key)==before.get(key)
    assert web.calls==[] and len(calls)==1
