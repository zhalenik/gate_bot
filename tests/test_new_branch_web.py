"""Real application routes for branch registration and scoped hardware setup."""
import re
import pytest
from test_web_security import web, login, post, TEST_PASSWORD
from storage.sql_documents import migrate_legacy, DocumentStore, read_document


def settings(**extra):
    return dict(name='Local parking',timezone='Europe/Moscow',enabled=True,logo='',buttons=[],**extra)


def test_manual_yclients_registration_can_save_own_gate_immediately(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    messages=[]
    identity.sender=lambda channel,target,text,context: messages.append(text) or {'status':'sent'}
    raw=identity.register_branch('315','New parking','New owner','+15550100911')['registration_token']
    challenge=identity.request_otp(raw,registration=True)['challenge_id']
    code=re.search(r'\b[0-9]{6}\b',messages[-1]).group()
    username='new-owner@example.invalid'
    identity.complete_registration(raw,challenge,code,username,TEST_PASSWORD)
    client=web.app.test_client()
    csrf=login(client,username)
    edits=settings(branch_id=315,yclients_enabled=True)
    edits['buttons']=[{'type':'gate','label':'Entry','phone':'+15550100912'}]
    assert post(client,csrf,'/admin/api/branch-settings',edits).status_code==200
    assert web.branches.get_branch(315).buttons[0].label=='Entry'
    assert post(client,csrf,'/admin/api/branch-settings',{**edits,'branch_id':12}).status_code==403
    assert not identity.integration_credentials('315','yclients') and not web.calls


def test_super_creates_standalone_without_external_id_and_trial_is_not_reissued(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    client=web.app.test_client();csrf=login(client,'super')
    result=post(client,csrf,'/admin/api/branch-settings',settings(create=True,yclients_enabled=False))
    assert result.status_code==200
    branch=result.json['settings'];identifier=branch['branch_id']
    assert identifier>=10**12 and branch['id_origin']=='internal' and not branch['yclients_enabled']
    service=web.module.product_services()
    assert service.billing.subscription_view(str(identifier))['available']
    with service.store.connect() as db:
        before=db.execute('SELECT count(*) FROM billing_terms WHERE branch_id=?',(str(identifier),)).fetchone()[0]
    assert post(client,csrf,'/admin/api/branch-settings',settings(branch_id=identifier,yclients_enabled=False)).status_code==200
    with service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM billing_terms WHERE branch_id=?',(str(identifier),)).fetchone()[0]==before
    assert post(client,csrf,'/admin/api/branch-settings',settings(branch_id=identifier,yclients_enabled=True)).status_code==400


def test_branch_admin_can_edit_own_routes_but_not_branch_type_or_foreign_routes(web,monkeypatch):
    monkeypatch.setattr('storage.config.CLIENT_OUTBOUND_ENABLED',True)
    with web.branches.store.transaction() as branches:
        branches['12'].update(yclients_enabled=False,hardware_routes_verified=False,buttons=[])
    client=web.app.test_client();csrf=login(client)
    assert '/admin/integrations' not in client.get('/admin/users').text
    integrations=client.get('/admin/integrations').text
    assert 'Мессенджеры ChatPush' not in integrations and 'маркетплейс YCLIENTS' not in integrations
    assert post(client,csrf,'/admin/api/branch-settings',settings(branch_id=12,yclients_enabled=True)).status_code==403
    payload=settings(branch_id=12,yclients_enabled=False)
    payload['buttons']=[{'type':'gate','label':'Entry','phone':'+15550000123'}]
    assert post(client,csrf,'/admin/api/branch-settings',payload).status_code==200
    assert web.branches.load_branches()['12']['hardware_routes_verified'] is True
    payload['buttons'][0]['label']='Updated entry'
    assert post(client,csrf,'/admin/api/branch-settings',payload).status_code==200
    payload['branch_id']=13
    assert post(client,csrf,'/admin/api/branch-settings',payload).status_code==403
    assert post(client,csrf,'/admin/api/branch-settings',settings(branch_id=12,yclients_enabled=False)).status_code==200
    assert post(client,csrf,'/admin/api/update-user',{'token':web.token_a,'id_ycl':'987'}).status_code==400
    assert post(client,csrf,'/admin/api/set-mailing',{'token':web.token_a,'enabled':True}).status_code==200


def test_super_rename_does_not_approve_empty_hardware_routes(web):
    with web.branches.store.transaction() as branches:
        branches['12'].update(yclients_enabled=False,hardware_routes_verified=False,buttons=[])
    client=web.app.test_client();csrf=login(client,'super')
    assert post(client,csrf,'/admin/api/branch-settings',settings(branch_id=12,yclients_enabled=False)).status_code==200
    assert web.branches.load_branches()['12']['hardware_routes_verified'] is False
    payload=settings(branch_id=12,yclients_enabled=False)
    payload['buttons']=[{'type':'gate','label':'Entry','phone':'+15550000123'}]
    assert post(client,csrf,'/admin/api/branch-settings',payload).status_code==200
    assert web.branches.load_branches()['12']['hardware_routes_verified'] is True


def test_standalone_manual_link_uses_paid_sms_only_after_opt_in(web,monkeypatch):
    from storage import config
    from unittest.mock import Mock
    import chatpush
    with web.branches.store.transaction() as branches:branches['12']['yclients_enabled']=False
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    forbidden=Mock(side_effect=AssertionError('ChatPush must not run'))
    monkeypatch.setattr(chatpush,'send_link',forbidden)
    send=Mock(return_value={'status':'success','delivery':'queued'})
    monkeypatch.setattr(web.module.product_services().notifications,'send',send)
    client=web.app.test_client();csrf=login(client)
    refused=post(client,csrf,'/admin/api/send-link',{'token':web.token_a})
    assert refused.status_code==503 and refused.json['delivery']['status']=='unavailable'
    send.assert_not_called()
    web.branches.update_settings(12,{'sms_notifications_enabled':True})
    response=post(client,csrf,'/admin/api/send-link',{'token':web.token_a})
    assert response.status_code==200 and send.call_args.args[0]=='sms'
    context=send.call_args.args[3]
    assert context['branch_id']=='12' and context['event']=='manual_pass' and context['preference_channel']=='sms'
    forbidden.assert_not_called()


def test_stable_gate_id_survives_round_trip_and_does_not_move_to_other_gate(web):
    client=web.app.test_client();csrf=login(client,'super')
    current=client.get('/admin/api/branch-settings?branch_id=12').json['settings']
    gate=current['buttons'][0]
    payload=settings(branch_id=12,yclients_enabled=True)
    payload['buttons']=[gate,{'type':'gate','label':'Second','phone':'+15550000456'}]
    saved=post(client,csrf,'/admin/api/branch-settings',payload)
    assert saved.status_code==200
    second=saved.json['settings']['buttons'][1]
    payload['buttons']=[second]
    saved=post(client,csrf,'/admin/api/branch-settings',payload)
    assert saved.status_code==200
    assert saved.json['settings']['buttons'][0]['stable_id']==second['stable_id']!=gate['stable_id']
