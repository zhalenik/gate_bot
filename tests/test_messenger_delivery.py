"""Offline provider selection and single-recipient YCLIENTS message delivery."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from messenger_service import MessengerService
from product_notifications import ProductNotifications
from storage import config
from storage.sql_documents import read_document, write_document
from test_identity_product import identities
from yclients_delivery import YClientsDelivery


@pytest.fixture
def messaging(identities, monkeypatch):
    state=identities
    monkeypatch.setattr(config,'YC_PARTNER_TOKEN','offline-partner')
    monkeypatch.setattr(config,'YCLIENTS_ENABLED',True)
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    state.service.save_integration('owner','11','yclients',{'user_token':'offline-user'})
    state.links.update_user('example-client',{'id_ycl':101})
    state.service.save_prefs_for_token('example-client',{'enabled':True,'channels':{'messengers':True}})
    return state,MessengerService(state.service)


@pytest.mark.parametrize('status', [200, 201, 202])
def test_yclients_single_client_contract_and_no_headers_in_result(status):
    session=Mock();session.post.return_value=SimpleNamespace(status_code=status,json=lambda:{'success':True,'meta':{'message':'Accepted'}})
    result=YClientsDelivery(session,pace=lambda:None).send('11',101,'Текст сообщения',{'partner_token':'offline-partner','user_token':'offline-user'})
    assert result=={'status':'accepted','code':'provider_accepted','http_status':status,'confirmed':False}
    session.post.assert_called_once_with('https://api.yclients.ru/api/v1/sms/clients/by_id/11',
        headers={'Authorization':'Bearer offline-partner, User offline-user','Accept':'application/vnd.yclients.v2+json','Content-Type':'application/json'},
        json={'client_ids':[101],'text':'Текст сообщения'},timeout=(5,20),allow_redirects=False,verify=True)


@pytest.mark.parametrize('status,body,expected',[(401,{},'failed'),(429,{},'failed'),(500,{},'unknown'),(408,{},'unknown'),(302,{},'failed'),(201,{},'unknown'),(201,{'success':False},'failed'),
    (200,{},'unknown'),(202,{},'unknown'),(202,{'success':'true'},'unknown'),
    (202,{'success':False},'failed')])
def test_failed_and_uncertain_results_have_no_raw_provider_payload(status,body,expected):
    session=Mock();session.post.return_value=SimpleNamespace(status_code=status,json=lambda:{**body,'private':'raw-text'})
    result=YClientsDelivery(session,pace=lambda:None).send(11,101,'message',{'partner_token':'offline-partner','user_token':'offline-user'})
    assert result['status']==expected and 'raw-text' not in json.dumps(result)
    assert result['http_status']==status
    assert session.post.call_count==1


@pytest.mark.parametrize('status', [200, 201, 202, 204])
def test_successful_http_with_unreadable_body_is_unknown_without_retry(status):
    session=Mock()
    session.post.return_value=SimpleNamespace(status_code=status,
        json=Mock(side_effect=ValueError('private provider response')))
    result=YClientsDelivery(session,pace=lambda:None).send(11,101,'message',
        {'partner_token':'offline-partner','user_token':'offline-user'})
    assert result=={'status':'unknown','code':'invalid_provider_response',
                    'http_status':status,'confirmed':False}
    assert session.post.call_count==1


@pytest.mark.parametrize('client_id',[None,False,0,-1,[], '1,2', '1/../2'])
def test_no_id_never_turns_into_a_phone_or_bulk_request(client_id):
    session=Mock()
    assert YClientsDelivery(session,pace=lambda:None).send(11,client_id,'message',{})['status']=='failed'
    session.post.assert_not_called()


def test_transport_timeout_is_unknown_without_retry():
    session=Mock();session.post.side_effect=requests.Timeout('private-details')
    result=YClientsDelivery(session,pace=lambda:None).send(11,101,'message',{'partner_token':'offline-partner','user_token':'offline-user'})
    assert result=={'status':'unknown','code':'transport_error','confirmed':False}
    assert session.post.call_count==1


def test_branch_default_and_explicit_provider_selection(messaging):
    state,service=messaging
    assert service.settings('north','11')['provider']=='yclients'
    with pytest.raises(ValueError):service.save('north','11','chatpush')
    with pytest.raises(PermissionError):service.save('north','12','yclients')
    state.service.save_integration('owner','11','chatpush',{'token':'offline-chatpush'})
    assert service.settings('north','11')['provider']=='yclients'
    assert service.save('north','11','yclients')['provider']=='yclients'
    assert service.settings('north','11')['provider']=='yclients'
    assert service.save('north','11','auto')['provider']=='yclients'


def test_guest_cannot_use_yclients_but_explicit_chatpush_link_can_use_phone(messaging,monkeypatch):
    state,service=messaging
    token=state.links.create_link({'branch_id':11,'phone':'+15550900111','is_temporary':True,'gate_access':True})
    with state.product.connect() as db:
        plan=service.plan(db,'11',token)
        assert not plan['connected'] and plan['reason']=='yclients_client_missing'
    sender=Mock(return_value={'status':'accepted'});monkeypatch.setattr('chatpush.send_message',sender)
    state.service.save_integration('owner','11','chatpush',{'token':'offline-chatpush'})
    with state.product.connect() as db:plan=service.plan(db,'11',token)
    assert plan['connected'] and plan['provider']=='chatpush'
    assert service.send('11',token,'chatpush',plan['target'],'Guest link')['status']=='accepted'
    assert sender.call_args.args[0]=='+15550900111'


def test_outbox_routes_by_client_id_and_rechecks_provider_and_recipient(messaging,monkeypatch):
    state,service=messaging
    calls=[]
    monkeypatch.setattr('yclients_delivery.YClientsDelivery.send',lambda self,*args:calls.append(args) or {'status':'accepted','confirmed':False})
    notices=ProductNotifications(state.service,state.links,state.branches,state.billing)
    payload={**state.links.load_links()['example-client'],'token':'example-client','event':'granted'}
    notices.enqueue(payload)
    with state.product.connect() as db:
        row=db.execute('SELECT * FROM message_outbox').fetchone()
        assert row['channel']=='yclients'
        assert notices._decrypt(row['payload'])['target']=='yclients:11:101'
    notices.process_once();notices.process_once()
    assert len(calls)==1 and calls[0][0:2]==('11',101)
    assert calls[0][3]=={'user_token':'offline-user','partner_token':'offline-partner'}
    state.links.update_user('example-client',{'id_ycl':102})
    notices.enqueue({**state.links.load_links()['example-client'],'token':'example-client','event':'granted'})
    state.service.save_integration('owner','11','chatpush',{'token':'offline-chatpush'})
    service.save('north','11','chatpush')
    notices.process_once()
    assert len(calls)==1
    with state.product.connect() as db:assert db.execute('SELECT count(*) FROM message_outbox WHERE state="skipped"').fetchone()[0]==1


def test_forged_target_and_cross_branch_token_never_reach_transport(messaging,monkeypatch):
    state,service=messaging;sender=Mock();monkeypatch.setattr('yclients_delivery.YClientsDelivery.send',sender)
    assert service.send('11','example-client','yclients','yclients:11:999','message')['status']=='skipped'
    assert service.send('12','example-client','yclients','yclients:11:101','message')['status']=='skipped'
    sender.assert_not_called()


def test_disabled_yclients_keeps_message_pending_without_claiming_effect(messaging,monkeypatch):
    state,service=messaging
    sender=Mock(return_value={'status':'accepted','confirmed':False})
    monkeypatch.setattr('yclients_delivery.YClientsDelivery.send',sender)
    notices=ProductNotifications(state.service,state.links,state.branches,state.billing)
    notices.enqueue({**state.links.load_links()['example-client'],'token':'example-client','event':'granted'})
    monkeypatch.setattr(config,'YCLIENTS_ENABLED',False)
    notices.process_once()
    with state.product.connect() as db:
        assert db.execute('SELECT state FROM message_outbox').fetchone()[0]=='pending'
        assert db.execute('SELECT count(*) FROM message_effects').fetchone()[0]==0
    sender.assert_not_called()
    monkeypatch.setattr(config,'YCLIENTS_ENABLED',True)
    notices.process_once();notices.process_once()
    assert sender.call_count==1


def test_legacy_pending_client_sms_is_skipped_after_channel_removal(messaging):
    state,service=messaging
    sender=Mock()
    notices=ProductNotifications(state.service,state.links,state.branches,state.billing,sender=sender)
    client=state.links.load_links()['example-client']
    with state.product.connect() as db:
        notices._enqueue('legacy-client-sms','client','access_granted','11','sms',client['phone'],
                         {'token':'example-client','revision':client.get('access_revision',0),'text':'Old notification'},db)
    notices.process_once()
    sender.assert_not_called()
    with state.product.connect() as db:
        assert db.execute('SELECT state FROM message_outbox').fetchone()[0]=='skipped'


def test_legacy_chatpush_optin_maps_to_messengers_and_sms_does_not(messaging):
    state,service=messaging
    state.service.save_integration('owner','11','chatpush',{'token':'offline-chatpush'})
    person_id=state.links.load_links()['example-client']['person_id']
    with state.product.connect() as db:
        db.execute('UPDATE notification_preferences SET payload=? WHERE person_id=? AND audience="client"',
            (json.dumps({'enabled':True,'channels':{'chatpush':True,'sms':True}}),person_id))
    prefs=state.service.prefs_for_token('example-client')
    assert prefs['channels']['messengers'] and 'sms' not in prefs['channels'] and 'chatpush' not in prefs['channels']
    assert prefs['connected']['messengers']
    state.links.set_mailing_enabled('example-client',False)
    assert not state.service.prefs_for_token('example-client')['enabled']
