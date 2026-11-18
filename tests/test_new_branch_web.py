"""Real application routes for standalone creation and protected hardware setup."""
import pytest
from test_web_security import web, login, post
from storage.sql_documents import migrate_legacy, DocumentStore, read_document


def settings(**extra):
    return dict(name='Local parking',timezone='Europe/Moscow',enabled=True,logo='',buttons=[],**extra)


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


def test_branch_admin_can_edit_own_routes_but_not_branch_type_or_foreign_routes(web):
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


def test_standalone_manual_link_uses_sms_and_never_chatpush(web,monkeypatch):
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
    response=post(client,csrf,'/admin/api/send-link',{'token':web.token_a})
    assert response.status_code==200 and send.call_args.args[0]=='sms'
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
