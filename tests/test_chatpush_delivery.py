"""The delivery API needs one opaque token; no real provider calls are made."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from chatpush import ChatPushClient, DELIVERY_URL, send_message


def session_for(body=None, status=200):
    session = Mock()
    session.request.return_value = SimpleNamespace(status_code=status, json=lambda:body)
    return session


@pytest.mark.parametrize('phone,expected',[
    ('+7 (900) 000-00-01','79000000001'),('8 (900) 000-00-01','79000000001'),
    ('9000000001','79000000001'),('+15550100200','15550100200')])
def test_delivery_uses_raw_authorization_fixed_url_and_only_text_phone(phone, expected):
    session = session_for({'delivery':{'id':42,'status_id':2}})
    result = ChatPushClient('synthetic-offline-token',session=session).send(phone,'Пример сообщения',external_id='local-event')
    assert result == {'status':'accepted','provider_id':'42','provider_status':2,'confirmed':False}
    session.request.assert_called_once_with('POST',DELIVERY_URL,
        headers={'authorization':'synthetic-offline-token','content-type':'application/json','accept':'application/json'},
        json={'text':'Пример сообщения','phone':expected},timeout=15,allow_redirects=False,verify=True)


@pytest.mark.parametrize('status,expected',[(301,'failed'),(401,'failed'),(403,'failed'),(422,'failed'),(429,'failed'),(408,'unknown'),(409,'unknown'),(500,'unknown')])
def test_rejected_or_ambiguous_results_do_not_leak_provider_response(status,expected):
    session=session_for({'private':'raw-provider-payload'},status)
    result=ChatPushClient('synthetic-offline-token',session=session).send('+15550100200','message')
    assert result['status']==expected and not result['confirmed'] and result['http_status']==status
    assert 'private' not in json.dumps(result)
    assert session.request.call_count==1


@pytest.mark.parametrize('body',[None,[],{}, {'delivery':{}},{'delivery':{'id':None}}, {'delivery':{'id':True}}, {'delivery':{'id':''}}])
def test_unrecognized_success_body_never_claims_delivery(body):
    session=session_for(body)
    result=ChatPushClient('synthetic-offline-token',session=session).send('+15550100200','message')
    assert result=={'status':'unknown','code':'invalid_provider_response','confirmed':False}


def test_meta_failure_overrides_a_delivery_object():
    session=session_for({'meta':{'status':'error','message':'private-provider-payload'},'delivery':{'id':42}})
    result=ChatPushClient('synthetic-offline-token',session=session).send('+15550100200','message')
    assert result=={'status':'failed','code':'provider_rejected','confirmed':False}


def test_timeout_and_invalid_json_have_no_blind_retry():
    session=session_for()
    session.request.side_effect=requests.Timeout('private-network-detail')
    client=ChatPushClient('synthetic-offline-token',session=session)
    assert client.send('+15550100200','message')=={'status':'unknown','code':'transport_error','confirmed':False}
    assert session.request.call_count==1
    session.request.side_effect=None
    session.request.return_value.json=Mock(side_effect=ValueError('private-response'))
    assert client.send('+15550100200','message')['code']=='invalid_provider_response'
    assert session.request.call_count==2


@pytest.mark.parametrize('phone,text',[('invalid','text'),('+15550100200',''),('+15550100200',None),('+15550100200','a'*3501),(None,'text')])
def test_invalid_message_never_calls_provider(phone,text):
    session=session_for()
    assert ChatPushClient('synthetic-offline-token',session=session).send(phone,text)['code']=='invalid_message'
    session.request.assert_not_called()


def test_outbound_switch_and_missing_token_never_call_provider(monkeypatch):
    from storage import config
    request=Mock(side_effect=AssertionError('No provider call expected'))
    monkeypatch.setattr('requests.sessions.Session.request',request)
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',False)
    assert send_message('+15550100200','message',credentials={'token':'synthetic-offline-token'})['skipped']
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    assert send_message('+15550100200','message',credentials={})['skipped']
    request.assert_not_called()


def test_sender_ignores_legacy_setup_fields(monkeypatch):
    from storage import config
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    session=session_for({'delivery':{'id':'offline-result'}})
    monkeypatch.setattr('requests.Session',lambda:session)
    result=send_message('+15550100200','message',credentials={
        'token':'synthetic-offline-token','scope':'obsolete','idempotency_key':'obsolete'},external_id='local-event')
    assert result['status']=='accepted'
    assert session.request.call_args.args==('POST',DELIVERY_URL)
    assert set(session.request.call_args.kwargs['json'])=={'text','phone'}
