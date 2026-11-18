"""All outgoing SMS use the private journal, with truthful terminal states."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from storage import config
from test_product_integration import product


@pytest.mark.parametrize('outcome,status',[
    ({'status':'error','code':'queue_full'},'failed'),
    ({'status':'error','code':'modem_disabled'},'failed'),
    (RuntimeError('synthetic failure'),'unknown'),
])
def test_failed_otp_attempt_is_visible_and_never_replayed(product,monkeypatch,outcome,status):
    import sim800c
    out=product.services.notifications
    queue=SimpleNamespace(dry_run=False,journal=out.telephony,add_manual=Mock())
    if isinstance(outcome,Exception):queue.add_manual.side_effect=outcome
    else:queue.add_manual.return_value=outcome
    monkeypatch.setattr(sim800c,'_get_queue',lambda:queue)
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    context={'purpose':'otp','idempotency_key':'archive-failure'}
    result=out.send('sms','+15550100555','GateBot: 123456. Test code.',context)
    record=out.telephony.snapshot()['records'][result['record_id']]
    assert record['status']==status and '123456' in record['text']
    out.send('sms','+15550100555','GateBot: 123456. Test code.',context)
    assert queue.add_manual.call_count==1


def test_dry_run_code_is_visible_as_simulated_without_hardware(product,monkeypatch):
    import sim800c
    out=product.services.notifications
    queue=SimpleNamespace(dry_run=True,journal=out.telephony,add_manual=Mock())
    monkeypatch.setattr(sim800c,'_get_queue',lambda:queue)
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    result=out.send('sms','+15550100555','GateBot: 123456. Test code.',{'purpose':'otp'})
    assert result['delivery']=='simulated'
    assert out.telephony.snapshot()['records'][result['record_id']]['status']=='simulated'
    queue.add_manual.assert_not_called()


def test_fast_hardware_completion_is_not_overwritten_and_deleted_code_stays_deleted(product,monkeypatch):
    import sim800c
    out=product.services.notifications
    def accept(*args,**kwargs):
        out.telephony.update(kwargs['record_id'],status='network_accepted')
        return {'status':'success','delivery':'queued','command_id':'synthetic-command'}
    queue=SimpleNamespace(dry_run=False,journal=out.telephony,add_manual=Mock(side_effect=accept))
    monkeypatch.setattr(sim800c,'_get_queue',lambda:queue)
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    context={'purpose':'otp','idempotency_key':'archive-once'}
    result=out.send('sms','+15550100555','GateBot: 123456. Test code.',context)
    record=result['record_id']
    assert out.telephony.snapshot()['records'][record]['status']=='network_accepted'
    out.telephony.delete_message(record)
    out.send('sms','+15550100555','GateBot: 123456. Test code.',context)
    assert record not in out.telephony.snapshot()['records'] and queue.add_manual.call_count==1
