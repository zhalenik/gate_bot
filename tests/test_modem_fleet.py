"""Offline fleet scheduling, physical ambiguity and SIM ownership regressions."""
import threading
import time
import uuid
from unittest.mock import Mock

import pytest

import sim800c
from modem_settings import default_modem, inventory, save_modems
from sim800c import CallQueue, ModemError, Sim800Modem
from sms_pdu import decode_deliver
from storage.product_store import ProductStore
from telephony_store import TelephonyStore, chat_key
from test_sim800c import GOLDEN_PDU
from test_telephony import SMSSerial
from test_web_security import web, login, post
from test_billing_sms_v2 import billing, reserve, AT
from test_product_integration import product


def row(name, **fields):
    return {**default_modem(), 'id':uuid.uuid4().hex, 'name':name, 'port':'/dev/gatebot-'+name, **fields}


class Driver:
    port = 'MOCK'
    def __init__(self, error=None, block=False):
        self.error = error
        self.calls = self.sms = 0
        self.entered, self.release = threading.Event(), threading.Event()
        if not block:
            self.release.set()

    def call(self, phone, duration, **kwargs):
        self.entered.set()
        assert self.release.wait(3)
        if self.error:
            raise self.error
        if 'before_dial' in kwargs:
            kwargs['before_dial']()
        self.calls += 1
        return {'delivery': 'command_sent'}

    def send_sms(self, *args, **kwargs):
        self.sms += 1
        return {'status': 'network_accepted'}

    def request_stop(self):
        self.release.set()

    def close(self):
        pass


def fleet(tmp_path, devices):
    queue = CallQueue(devices=devices, enabled=True, dry_run=False)
    queue.journal = TelephonyStore(tmp_path/'archive.json')
    return queue


def finish(queue):
    with queue.lock:
        workers = list(queue.workers)
    for worker in workers:
        worker.join(4)
        assert not worker.is_alive()
    assert not queue.processing


def test_priority_rotation_capability_and_busy_failover(tmp_path):
    primary, peer, sms = Driver(block=True), Driver(), Driver()
    queue = fleet(tmp_path, [(row('first',call_priority=9),primary), (row('peer',call_priority=9),peer),
        (row('text',call_enabled=False,sms_priority=9),sms)])
    queue.add_call('1234567')
    assert primary.entered.wait(1)
    queue.add_call('1234568')
    assert peer.entered.wait(1)
    primary.release.set()
    finish(queue)
    for _ in range(2):
        queue.add_call('1234567')
        finish(queue)
    assert primary.calls == peer.calls == 2
    queue.add_manual('sms','1234567',text='test')
    finish(queue)
    assert sms.sms == 1 and sms.calls == primary.sms == peer.sms == 0
    names = {r['modem_name'] for r in queue.journal.list_records()['records']}
    assert names == {'first','peer'}
    assert queue.shutdown()


@pytest.mark.parametrize('uncertain,expected', [(False,'command_sent'), (True,'unknown')])
def test_call_failover_charges_once_and_unknown_is_final(tmp_path, uncertain, expected):
    first, second = Driver(ModemError('port_unavailable', uncertain=uncertain)), Driver()
    queue = fleet(tmp_path, [(row('primary',call_priority=8),first), (row('secondary'),second)])
    guard, observer = Mock(return_value=True), Mock()
    queue.command_guard, queue.command_observer = guard, observer
    result = queue.add_call('1234567',command_id='one-command',gate_id='one-gate')
    finish(queue)
    assert queue.status()['recent'][0]['status'] == expected
    assert second.calls == int(not uncertain)
    assert guard.call_count == int(not uncertain)
    assert [args[0][1] for args in observer.call_args_list] == (['queued','unknown'] if uncertain else ['queued','started','command_sent'])
    assert queue.add_call('1234567',command_id=result['command_id'])['coalesced']
    assert queue.shutdown()


@pytest.mark.parametrize('fail_part,unknown,expected,secondary_parts', [
    (1,False,'network_accepted',3), (2,False,'partial',0), (None,True,'unknown',0)])
def test_multipart_retries_only_confirmed_first_part_rejection(tmp_path, billing, fail_part, unknown, expected, secondary_parts):
    first, second = SMSSerial(fail_part), SMSSerial()
    primary = Sim800Modem('test-first',serial_factory=lambda **_: first,startup_delay=0)
    secondary = Sim800Modem('test-second',serial_factory=lambda **_: second,startup_delay=0)
    if unknown:
        read = primary._line_locked
        primary._line_locked = lambda *a, **kw: None if first.payloads else read(*a, **kw)
    queue = fleet(tmp_path, [(row('primary',sms_priority=10),primary),(row('secondary'),secondary)])
    record = queue.journal.add('sms','outgoing',phone='1234567')
    assert reserve(billing,key=record,parts=3)['allowed']
    queue.add_manual('sms','1234567',text='Я'*150,record_id=record,
                     before_submit=lambda:billing.sms.authorize_sms(record,at=AT))
    finish(queue)
    message = queue.journal.snapshot()['records'][record]
    assert message['status'] == expected
    assert message['modem_name'] == ('secondary' if secondary_parts else 'primary')
    assert len(second.payloads) == secondary_parts
    assert len(first.payloads) == (fail_part or 1)
    assert billing.sms.finish_sms(record,message['status'],segments=message['segments'],at=AT)
    balance = billing.subscription_view('12',at=AT)['sms']
    assert balance['reserved'] == 0 and balance['spent'] == (3 if secondary_parts else 1)
    detail = billing.sms.details(['12'])['items']
    assert len(detail) == 1 and 'modem_id' not in detail[0]
    assert queue.shutdown()


def test_managed_gate_fails_over_with_one_durable_charge(tmp_path, product, monkeypatch):
    first, second = Driver(ModemError('port_unavailable')), Driver()
    queue = fleet(tmp_path, [(row('primary',call_priority=8),first), (row('secondary'),second)])
    queue.command_guard = lambda command:product.access.authorize_command(command,start=True)
    queue.command_observer = product.access.command_finished
    monkeypatch.setattr(sim800c,'_get_queue',lambda:queue)
    product.access.opener = None
    gate = product.branches.get_branch(10).buttons[0]
    result = product.access.open_for_token(product.token,gate.id,request_id='managed-failover')
    finish(queue)
    assert result['status'] == 'success' and first.calls == 0 and second.calls == 1
    view = product.services.billing.subscription_view(10)
    assert view['spent'] == 1 and view['reserved'] == 0
    assert queue.shutdown()


def test_expiry_while_waiting_for_sms_editor_prevents_first_submit(monkeypatch):
    port = SMSSerial()
    modem = Sim800Modem('test',serial_factory=lambda **_:port,startup_delay=0)
    clock = Mock(wraps=time)
    clock.monotonic.side_effect = time.monotonic
    monkeypatch.setattr(sim800c,'time',clock)
    deadline = time.monotonic()+5
    prompt = modem._sms_prompt_locked
    def expire_after_prompt():
        prompt()
        clock.monotonic.side_effect = lambda:deadline+1
    monkeypatch.setattr(modem,'_sms_prompt_locked',expire_after_prompt)
    result = modem.send_sms('1234567','текст',progress=lambda _:None,not_after=deadline)
    assert result['status'] == 'expired' and not port.payloads
    modem.close()


def test_shutdown_of_both_ports_cancels_queued_without_fallback(tmp_path):
    first, second = Driver(block=True), Driver(block=True)
    queue = fleet(tmp_path,[(row('a'),first),(row('b'),second)])
    first.error = second.error = ModemError('stopping')
    queue.add_call('1234567')
    queue.add_call('1234568')
    assert first.entered.wait(1) and second.entered.wait(1)
    queue.add_call('1234569')
    assert queue.shutdown()
    assert sorted(c['status'] for c in queue.recent) == ['cancelled','failed','failed']
    assert queue.add_call('1234567')['status'] == 'error' and first.calls == second.calls == 0


def test_unreleased_port_prevents_reexec_until_closed(tmp_path):
    modem = Sim800Modem('MOCK',command_timeout=300,serial_factory=Mock())
    queue = fleet(tmp_path,[(row('a'),modem)])
    ready,release = threading.Event(),threading.Event()
    def hold():
        with modem._lock:
            ready.set()
            assert release.wait(2)
    reader = threading.Thread(target=hold)
    reader.start()
    try:
        assert ready.wait(1)
        assert queue.shutdown(timeout=0.01) is False
    finally:
        release.set()
        reader.join(2)
    assert queue.shutdown() is True


def test_archives_same_slot_and_identical_pdu_per_modem_and_removes_only_selected(tmp_path):
    path = tmp_path/'archive.json'
    a, b = TelephonyStore(path), TelephonyStore(path,modem_id='b',modem_name='second')
    pdu = decode_deliver(GOLDEN_PDU,1)
    key = chat_key(pdu.sender)
    one, two = a.ingest([pdu])[0], b.ingest([pdu])[0]
    assert one['part_key'] != two['part_key']
    a.acknowledge_sim(one)
    assert b.messages(key,modem_id='b')['messages'][0]['sim_locations'] == [two]
    assert a.chats()['chats'][0]['count'] == 2 and a.chats(modem_id='legacy')['chats'][0]['count'] == 1
    a.read_chat(key,'legacy')
    assert b.chats(modem_id='b')['chats'][0]['unread'] == 1
    assert b.clear_sms(key,'b') == 1
    assert a.messages(key)['total'] == 1
    assert a.messages(key)['messages'][0]['modem_id'] == 'legacy'


def test_inventory_persists_empty_set_and_rejects_conflicts(tmp_path):
    db = ProductStore(tmp_path/'app.sqlite3')
    assert inventory(db)['modems'][0]['id'] == 'legacy'
    first = row('one')
    second = row('two')
    with pytest.raises(ValueError):
        save_modems(db,0,[first,dict(second,port=first['port'])])
    with pytest.raises(ValueError):
        save_modems(db,0,[dict(first,host_device='/dev/serial/by-id/..')])
    saved = save_modems(db,0,[first,second])
    assert inventory(db) == saved
    with pytest.raises(ValueError):
        save_modems(db,0,[])
    save_modems(db,1,[])
    assert inventory(db)['modems'] == []


def test_system_settings_modems_require_super_and_csrf(web, monkeypatch):
    client = web.app.test_client()
    url = '/admin/api/system-settings/modems'
    csrf = login(client)
    assert client.get(url).status_code == 403 and post(client,csrf,url,{}).status_code == 403
    csrf = login(client,'super')
    assert client.post(url,json={}).status_code == 403
    driver = Driver()
    queue = CallQueue(devices=[(row('one'),driver)], enabled=True, dry_run=False)
    driver.diagnostics = Mock(return_value={'state':'ok','operator':'Test'})
    monkeypatch.setattr(sim800c,'_get_queue',lambda:queue)
    response = client.get(url)
    assert response.status_code == 200
    assert post(client,csrf,url,{'revision':response.json['revision'],'modems':[row('saved')]}).status_code == 200
    probe = url+'/'+next(iter(queue.devices))+'/probe'
    assert post(client,csrf,probe,{}).status_code == 400 and not driver.diagnostics.called
    queue.monitor_started = True
    assert post(client,csrf,probe,{}).json['result']['operator'] == 'Test'
    queue.shutdown()
