"""Offline telecom acceptance: fake serial, private temporary JSON, scoped HTTP."""
from dataclasses import replace
import json
import threading
import uuid

import pytest

from sim800c import CallQueue, Sim800Modem
from sms_pdu import decode_deliver
from sms_submit import encode_submit
from telephony_store import TelephonyStore
from telephony_service import TelephonyService
from test_sim800c import FakeSerial, cmgl, GOLDEN_PDU
from test_sms_pdu import deliver
from test_web_security import web, login, post


def test_submit_golden_cyrillic_and_modem_length():
    part = encode_submit('+1234567', 'Привет', reference=1)[0]
    assert part['pdu'] == '0001000791214365F700080C041F04400438043204350442'
    assert part['tpdu_length'] == len(bytes.fromhex(part['pdu'])) - 1


@pytest.mark.parametrize('text,count', [('Я'*70, 1), ('Я'*71, 2), ('Я'*132, 2), ('Я'*133, 3), ('Я'*65+'😀'+'Ю'*80, 3)])
def test_multipart_boundaries_and_utf16_surrogates(text, count):
    parts = encode_submit('+1234567', text, reference=0xABCD)
    assert len(parts) == count
    chunks = []
    for i, part in enumerate(parts, 1):
        raw = bytes.fromhex(part['pdu'])
        # Independently inspect TP-UDL and TP-UD after 4 BCD address octets.
        ud = raw[12:]
        assert raw[11] == len(ud) <= 140
        if count > 1:
            assert ud[:7] == bytes([6, 8, 4, 0xAB, 0xCD, count, i])
            ud = ud[7:]
        chunks.append(ud.decode('utf-16-be'))
    assert ''.join(chunks) == text


def test_protocol_limit_not_silent_truncation():
    assert len(encode_submit('+1234567', 'Я' * (66*255))) == 255
    with pytest.raises(ValueError, match='255'):
        encode_submit('+1234567', 'Я' * (66*255+1))
    for phone, text in [('+1234567\rATD', 'Hi'), ('+1234567', ''), ('+1234567', '\ud800')]:
        with pytest.raises(ValueError):
            encode_submit(phone, text)


def test_archive_reassembles_across_restart_deduplicates_and_keeps_timestamp(tmp_path):
    path = tmp_path / 'telephony.json'
    first = decode_deliver(deliver('Начало', udh=bytes.fromhex('06080412340201')), 1)
    second = decode_deliver(deliver('Конец', udh=bytes.fromhex('06080412340202')), 2)
    store = TelephonyStore(path)
    store.ingest([second])
    row = store.list_records()['records'][0]
    assert row['text'] is None and row['missing_parts'] == [1]
    record_id = row['id']
    store.update(record_id, read=True)
    store = TelephonyStore(path)
    store.ingest([first, replace(second, storage_index=9)])
    rows = store.list_records()['records']
    assert len(rows) == 1 and rows[0]['id'] == record_id
    assert rows[0]['text'] == 'НачалоКонец' and rows[0]['complete']
    assert rows[0]['read'] is False
    assert rows[0]['timestamp'] == first.timestamp.isoformat()
    before = path.read_bytes()
    store.ingest([first, second])
    assert path.read_bytes() == before


def test_ring_clip_repeats_one_call_without_private_terminal_output(tmp_path, capsys):
    store = TelephonyStore(tmp_path/'telephony.json')
    modem = Sim800Modem('FAKE', inbox=store)
    for line in ['RING', '+CLIP: "1234567",145', 'RING', '+CLCC: 1,1,4,0,0,"1234567",145']:
        modem._capture_notice(line)
        modem._report_notice(line)
    rows = store.list_records()['records']
    assert len(rows) == 1 and rows[0]['phone'] == '+1234567'
    assert '1234567' not in capsys.readouterr().out
    modem._capture_notice('NO CARRIER')
    modem._capture_notice('RING')
    assert store.list_records()['total'] == 2


class SMSSerial(FakeSerial):
    def __init__(self, fail_part=None):
        super().__init__(chunk=1)
        self.payloads = []
        self.fail_part = fail_part

    def write(self, data):
        if data.endswith(b'\x1a'):
            self.payloads.append(data[:-1])
            reply = '\r\n+CMS ERROR: Requested facility not subscribed\r\n' if len(self.payloads) == self.fail_part else '\r\n+CMGS: 42\r\nOK\r\n'
            self.incoming.extend(reply.encode())
            return len(data)
        if data.startswith(b'AT+CMGS='):
            self.commands.append(data.decode().strip())
            self.incoming.extend(b'\r\n> ')
            return len(data)
        if data == b'\x1b':
            return 1
        return super().write(data)


@pytest.mark.parametrize('fail_part', [None, 1, 2])
def test_multipart_submission_partial_rejection_and_no_retry(fail_part):
    port = SMSSerial(fail_part)
    modem = Sim800Modem('FAKE', serial_factory=lambda **kw: port, startup_delay=0)
    updates = []
    try:
        result = modem.send_sms('+1234567', 'Я'*150, progress=lambda p: updates.append(p))
    finally:
        modem.close()
    assert result['status'] == ({None: 'network_accepted', 1: 'failed', 2: 'partial'}[fail_part])
    assert len(port.payloads) == (fail_part or 3)
    assert result['confirmed'] is False
    if fail_part:
        assert result['code'] == 'sms_service_not_subscribed'
        assert result['segments'][fail_part-1]['status'] == 'failed'
    else:
        assert all(p['status'] == 'network_accepted' for p in result['segments'])


def test_sms_result_timeout_is_unknown_not_retried():
    port = SMSSerial()
    modem = Sim800Modem('FAKE', serial_factory=lambda **kw: port, startup_delay=0)
    original = modem._line_locked
    modem._line_locked = lambda deadline, **kw: None if port.payloads else original(deadline, **kw)
    try:
        result = modem.send_sms('+1234567', 'Привет', progress=lambda parts: None)
    finally:
        modem.close()
    assert result['status'] == 'unknown' and len(port.payloads) == 1 and port.closed


def test_incoming_notice_is_archived_between_outgoing_segments(tmp_path):
    port = SMSSerial()
    port.responses['AT+CMGL=4,1'] = '\r\n' + cmgl() + 'OK\r\n'
    journal = TelephonyStore(tmp_path/'telephony.json')
    modem = Sim800Modem('FAKE', serial_factory=lambda **kw: port, startup_delay=0, inbox=journal)
    observed = []
    def progress(parts):
        accepted = sum(p['status'] == 'network_accepted' for p in parts)
        if accepted == 1 and not observed:
            observed.append(True)
            modem._capture_notice('+CMTI: "SM",1')
        if accepted == 2:
            assert journal.list_records()['total'] == 1
    try:
        result = modem.send_sms('+1234567', 'Я'*150, progress=progress)
        assert result['status'] == 'network_accepted'
        assert any(c.startswith('AT+CMGD=') for c in port.commands)
    finally:
        modem.close()


def test_read_sms_archives_before_any_sim_deletion(tmp_path):
    port = FakeSerial(responses={'AT+CMGL=4,1': '\r\n'+cmgl()+'OK\r\n'})
    store = TelephonyStore(tmp_path/'telephony.json')
    modem = Sim800Modem('FAKE', serial_factory=lambda **kw: port, startup_delay=0, inbox=store)
    try:
        modem.read_sms()
        modem.read_sms()
    finally:
        modem.close()
    rows = store.list_records()['records']
    assert len(rows) == 1 and rows[0]['text'] == 'Привет'
    assert any(c == 'AT+CMGD=1,0' for c in port.commands)
    assert rows[0]['sim_locations'] == []  # Removed from SIM, still fully readable.


def test_manual_queue_idempotency_dry_run_and_no_phone_text_in_public_status(tmp_path):
    store = TelephonyStore(tmp_path/'telephony.json')
    queue = CallQueue(enabled=True, dry_run=True)
    queue.journal = store
    service = TelephonyService(store, queue)
    data = {'kind': 'sms', 'phone': '+1234567', 'text': 'Частное сообщение', 'request_id': uuid.uuid4().hex}
    try:
        first = service.submit({'is_super': True}, 'super', data)
        second = service.submit({'is_super': True}, 'super', data)
        assert first['delivery'] == 'simulated' and second['duplicate']
        assert len(queue.recent) == 1 and store.list_records()['total'] == 1
        public = json.dumps(queue.status())
        assert data['text'] not in public and data['phone'] not in public
        with pytest.raises(PermissionError):
            service.submit({'is_super': False}, 'tenant', data)
    finally:
        queue.shutdown()


def test_restart_marks_unfinished_outgoing_unknown_never_replays(tmp_path):
    store = TelephonyStore(tmp_path/'telephony.json')
    store.add('sms', 'outgoing', status='processing', phone='+1234567')
    store.recover_outgoing()
    assert store.list_records()['records'][0]['status'] == 'unknown'


def test_private_sms_and_caller_never_enter_audit_or_terminal(tmp_path, monkeypatch, capsys):
    from storage import config
    import logger
    monkeypatch.setattr(config, 'LOG_DIR', str(tmp_path/'logs'))
    store = TelephonyStore(tmp_path/'telephony.json')
    port = SMSSerial()
    modem = Sim800Modem('FAKE', serial_factory=lambda **kw: port, startup_delay=0, inbox=store)
    try:
        modem._capture_notice('+CLIP: "1234567",145')
        modem.send_sms('+1234567', 'Синтетическое личное сообщение', progress=lambda parts: None)
    finally:
        modem.close()
    public = capsys.readouterr().out + json.dumps(logger.read_events(), ensure_ascii=False)
    assert '1234567' not in public and 'Синтетическое личное сообщение' not in public
    assert store.list_records()['records'][0]['phone'] == '+1234567'


def test_delete_archived_sms_compares_current_slot_before_deleting(tmp_path):
    from telephony_store import part_key
    part = decode_deliver(GOLDEN_PDU, 1)
    port = FakeSerial(responses={'AT+CMGL=4,1': '\r\n'+cmgl()+'OK\r\n'})
    modem = Sim800Modem('FAKE', serial_factory=lambda **kw: port, startup_delay=0)
    try:
        result = modem.delete_archived_sms([
            {'storage': 'SM', 'index': 1, 'part_key': 'different-message'},
            {'storage': 'SM', 'index': 2, 'part_key': part_key(part)},
            {'storage': 'SM', 'index': 1, 'part_key': part_key(part)},
        ])
    finally:
        modem.close()
    assert result['deleted_parts'] == 1 and result['skipped_parts'] == 2
    assert [c for c in port.commands if c.startswith('AT+CMGD')] == ['AT+CMGD=1,0']


def test_super_manual_delete_is_dry_run_and_keeps_archived_content(tmp_path):
    store = TelephonyStore(tmp_path/'telephony.json')
    store.ingest([decode_deliver(GOLDEN_PDU, 1)])
    source = store.list_records()['records'][0]
    queue = CallQueue(enabled=False, dry_run=True)
    queue.journal = store
    try:
        result = TelephonyService(store, queue).submit({'is_super': True}, 'super',
            {'kind': 'sms_delete', 'source_id': source['id'], 'request_id': uuid.uuid4().hex})
        assert result['delivery'] == 'simulated'
        assert store.store.read()['records'][source['id']]['text'] == 'Привет'
    finally:
        queue.shutdown()


def test_calls_and_manual_sms_use_one_worker_and_shutdown_cancels_pending(tmp_path):
    class Modem:
        port = 'FAKE'
        def __init__(self):
            self.started, self.release = threading.Event(), threading.Event()
            self.calls, self.sms = [], []
        def call(self, phone, duration, **kw):
            self.calls.append(phone)
            self.started.set()
            assert self.release.wait(2)
            return {'delivery': 'command_sent'}
        def send_sms(self, *args, **kw):
            self.sms.append(args)
            return {'status': 'network_accepted'}
        def request_stop(self):
            self.release.set()
        def close(self):
            pass
    modem = Modem()
    queue = CallQueue(modem, enabled=True, dry_run=False)
    store = TelephonyStore(tmp_path/'telephony.json')
    queue.journal = store
    try:
        queue.add_call('1234567')
        assert modem.started.wait(1)
        result = TelephonyService(store, queue).submit({'is_super': True}, 'super',
            {'kind': 'sms', 'phone': '+1234567', 'text': 'Привет', 'request_id': uuid.uuid4().hex})
        assert result['delivery'] == 'queued' and not modem.sms
        assert queue.shutdown()
        row = store.store.read()['records'][result['record_id']]
        assert row['status'] == 'cancelled' and not modem.sms
    finally:
        queue.shutdown()


@pytest.mark.parametrize('path', ['/admin/telephony', '/admin/api/telephony/records', '/admin/api/telephony/status', '/admin/api/telephony/sms/chats/'+('a'*64)])
def test_telephony_reads_are_superadmin_only(web, path):
    client = web.app.test_client()
    assert client.get(path).status_code in {302, 401}
    login(client)
    assert client.get(path).status_code == 403
    login(client, 'super')
    assert client.get(path).status_code == 200


@pytest.mark.parametrize('path', ['/admin/api/telephony/commands', '/admin/api/telephony/preview', '/admin/api/telephony/records', '/admin/api/telephony/records/absent/read', '/admin/api/telephony/contacts/resolve', '/admin/api/telephony/sms/messages/'+('a'*32)+'/delete'])
def test_telephony_mutations_enforce_csrf_and_current_role(web, path):
    client = web.app.test_client()
    csrf = login(client)
    assert post(client, csrf, path, {}).status_code == 403
    csrf = login(client, 'super')
    assert client.post(path, json={}).status_code == 403
    admins = web.module.admin_auth.load_admins()
    admins['super']['is_super'] = False
    admins['super']['branch_id'] = 12
    web.module.admin_auth.save_admins(admins)
    assert post(client, csrf, path, {}).status_code in {401, 403}


def test_super_ui_and_arbitrary_manual_phone_are_separate_from_gate_route(web, monkeypatch):
    from storage import config
    monkeypatch.setattr(config, 'GATE_DRY_RUN', True)
    import sim800c
    queue = CallQueue(enabled=False, dry_run=True)
    monkeypatch.setattr(sim800c, '_queue', queue)
    client = web.app.test_client()
    csrf = login(client, 'super')
    assert 'Звонки и SMS' in client.get('/admin/users').text
    for kind in ('call', 'sms'):
        data = {'kind': kind, 'phone': '+1234567', 'text': 'Привет\nмир', 'request_id': uuid.uuid4().hex}
        response = post(client, csrf, '/admin/api/telephony/commands', data)
        assert response.status_code == 202 and response.json['delivery'] == 'simulated'
    records = client.get('/admin/api/telephony/records').json['records']
    assert len(records) == 2 and any(r['text'] == 'Привет\nмир' for r in records)
    assert web.calls == []
    queue.shutdown()


def test_archive_commit_precedes_deletion_and_failure_never_deletes(tmp_path, monkeypatch):
    store = TelephonyStore(tmp_path/'telephony.json')
    class Serial(FakeSerial):
        def write(self, data):
            if data.startswith(b'AT+CMGD='):
                rows = store.list_records()['records']
                assert rows[0]['text'] == 'Привет'
                assert store.store.read()['sms_parts']
            return super().write(data)
    port = Serial(responses={'AT+CMGL=4,1': '\r\n'+cmgl()+'OK\r\n'})
    modem = Sim800Modem('FAKE', serial_factory=lambda **kw: port, startup_delay=0, inbox=store)
    try:
        modem.read_sms()
        port.commands.clear()
        def fail(*args, **kwargs):
            raise OSError('Synthetic disk full')
        monkeypatch.setattr(store.store, '_write', fail)
        # Force a new location: an existing archive must still not authorize deletion
        # if this scan cannot be committed.
        port.responses['AT+CMGL=4,1'] = '\r\n'+cmgl(index=2)+'OK\r\n'
        with pytest.raises(OSError):
            modem.read_sms()
        assert not any(c.startswith('AT+CMGD=') for c in port.commands)
    finally:
        modem.close()


def test_auto_deletion_of_partial_parts_preserves_assembly_after_restart(tmp_path):
    path = tmp_path/'telephony.json'
    first = decode_deliver(deliver('Начало', udh=bytes.fromhex('06080412340201')), 1)
    second = decode_deliver(deliver('Конец', udh=bytes.fromhex('06080412340202')), 1)
    store = TelephonyStore(path)
    receipt = store.ingest([first])[0]
    store.acknowledge_sim(receipt)
    row = store.list_records()['records'][0]
    assert row['parts'][0]['text'] == 'Начало' and row['sim_locations'] == []
    store = TelephonyStore(path)
    receipt = store.ingest([second])[0]
    store.acknowledge_sim(receipt)
    rows = store.list_records()['records']
    assert len(rows) == 1 and rows[0]['text'] == 'НачалоКонец'
    assert not rows[0]['sim_locations']


def test_clear_chat_removes_parts_without_resurrection_and_keeps_calls_and_dedupe(tmp_path):
    from telephony_store import chat_key
    store = TelephonyStore(tmp_path/'telephony.json')
    part = decode_deliver(GOLDEN_PDU, 1)
    store.ingest([part])
    call_id = store.add('call', 'incoming', phone=part.sender)
    queue = CallQueue(enabled=False, dry_run=True)
    queue.journal = store
    data = {'kind':'sms', 'phone':'+1234567', 'text':'Тест', 'request_id':uuid.uuid4().hex}
    service = TelephonyService(store, queue)
    try:
        result = service.submit({'is_super':True}, 'super', data)
        store.clear_sms(chat_key(part.sender))
        assert store.messages(chat_key(part.sender))['total'] == 0
        assert not store.store.read()['sms_parts']
        store.ingest([part])
        assert store.messages(chat_key(part.sender))['total'] == 0
        store.clear_sms()
        assert service.submit({'is_super':True}, 'super', data)['duplicate']
        assert len(queue.recent) == 1
        assert list(store.snapshot()['records']) == [call_id]
        assert result['record_id'] in store.snapshot()['requests']
    finally:
        queue.shutdown()


def test_migration_hides_cleanup_rows_without_hiding_sms_text(tmp_path):
    store = TelephonyStore(tmp_path/'telephony.json')
    incoming = store.add('sms', 'incoming', phone='+1234567', text='Сохранённый текст')
    cleanup = store.add('sms', 'outgoing', action='sms_delete', phone='+1234567', text='', status='sim_cleanup_complete')
    state = store.snapshot()
    assert cleanup in state['maintenance'] and cleanup not in state['records']
    assert state['records'][incoming]['text'] == 'Сохранённый текст'
    assert store.chats()['total'] == 1 and store.chats()['chats'][0]['count'] == 1


def test_chats_merge_number_formats_and_sort_actual_instants(tmp_path):
    from telephony_store import chat_key
    store = TelephonyStore(tmp_path/'telephony.json')
    store.add('sms','incoming',phone='89000000000', text='Раньше', timestamp='2026-11-16T14:00:00+03:00')
    store.add('sms','outgoing',phone='+79000000000', text='Позже', timestamp='2026-11-16T12:00:00+00:00')
    store.add('sms','incoming',phone='Sender', text='Новое', timestamp='2026-11-16T13:00:00+00:00')
    chats = store.chats()['chats']
    assert len(chats) == 2 and chats[0]['phone'] == 'Sender'
    assert chats[1]['count'] == 2 and chats[1]['preview'] == 'Позже'
    assert store.messages(chat_key('89000000000'))['messages'][0]['text'] == 'Позже'


@pytest.mark.parametrize('endpoint', ['sms/chats','sms/clear','sms/chats/'+('a'*64)+'/clear','sms/chats/'+('a'*64)+'/read'])
def test_new_chat_endpoints_enforce_role_and_csrf(web, endpoint):
    client = web.app.test_client()
    token = login(client)
    assert post(client,token,'/admin/api/telephony/'+endpoint,{}).status_code == 403
    token = login(client,'super')
    assert client.post('/admin/api/telephony/'+endpoint,json={}).status_code == 403


def test_chat_api_clear_only_sms_history_not_calls(web):
    from telephony_store import chat_key
    store = TelephonyStore()
    store.add('sms','incoming',phone='+1234567',text='Пример')
    store.add('call','incoming',phone='+1234567')
    client=web.app.test_client(); token=login(client,'super')
    assert post(client,token,'/admin/api/telephony/sms/chats',{}).json['total'] == 1
    response=client.get('/admin/api/telephony/sms/chats/'+chat_key('+1234567'))
    assert response.json['messages'][0]['text'] == 'Пример'
    assert post(client,token,'/admin/api/telephony/sms/clear',{}).json['deleted'] == 1
    assert store.list_records(kind='call')['total'] == 1
    assert store.chats()['total'] == 0


def test_new_message_in_reused_slot_is_archived_without_cmti(tmp_path):
    pdu2 = deliver('Новое сообщение')
    class MemorySerial(FakeSerial):
        def __init__(self):
            super().__init__()
            self.slot = GOLDEN_PDU
            self.deleted = 0
        def write(self, data):
            command = data.decode().rstrip('\r')
            if command == 'AT+CMGL=4,1':
                self.responses[command] = '\r\n'+(cmgl(pdu=self.slot) if self.slot else '')+'OK\r\n'
            if command == 'AT+CMGD=1,0':
                self.deleted += 1
                self.slot = pdu2 if self.deleted == 1 else None
            return super().write(data)
    port = MemorySerial()
    store = TelephonyStore(tmp_path/'telephony.json')
    modem = Sim800Modem('FAKE', serial_factory=lambda **kw: port, startup_delay=0, inbox=store)
    try:
        modem.read_sms()
        modem.read_sms()
    finally:
        modem.close()
    rows = store.list_records()['records']
    assert {r['text'] for r in rows} == {'Привет','Новое сообщение'}
    assert port.deleted == 2 and not modem.sms_pending


def test_background_scan_reads_both_stores_without_notifications(monkeypatch):
    import sim800c
    from types import SimpleNamespace
    stop = threading.Event()
    calls = []
    class Modem:
        port='FAKE'
        baudrate=9600
        sms_pending=False
        pending_sms_storages=set()
        sms_storages={'SM','ME'}
        last_sms_status={}
        def diagnostics(self):return {'state':'ok'}
        def poll_events(self):pass
        def read_sms(self,storage):
            calls.append(storage)
            self.last_sms_status={'state':'ok','parts_read':1,'deleted_parts':1}
            if len(calls)==2:stop.set()
    modem=Modem()
    monkeypatch.setattr(sim800c,'_get_queue',lambda:SimpleNamespace(enabled=True,dry_run=False,modem=modem))
    sim800c.monitor_status(stop)
    assert set(calls)=={'SM','ME'}
    assert modem.last_sms_status['deleted_parts']==2


def test_cli_read_mode_can_archive_without_deleting(tmp_path):
    store = TelephonyStore(tmp_path/'telephony.json')
    port = FakeSerial(responses={'AT+CMGL=4,1':'\r\n'+cmgl()+'OK\r\n'})
    modem = Sim800Modem('FAKE',serial_factory=lambda **kw:port,startup_delay=0,inbox=store)
    try:
        modem.read_sms(cleanup=False)
    finally:
        modem.close()
    assert store.list_records()['records'][0]['text']=='Привет'
    assert not any(c.startswith('AT+CMGD=') for c in port.commands)


def test_repeat_same_reference_survives_archive_and_legacy_reassembly(tmp_path):
    from dataclasses import replace, asdict
    from datetime import timedelta
    from telephony_store import part_key
    first = decode_deliver(deliver('Один', udh=bytes.fromhex('0500037A0201')),1)
    second = decode_deliver(deliver(' текст', udh=bytes.fromhex('0500037A0202')),2)
    store=TelephonyStore(tmp_path/'telephony.json')
    store.ingest([first,second])
    original=store.list_records()['records'][0]['id']
    again=[replace(p,timestamp=p.timestamp+timedelta(minutes=10)) for p in (first,second)]
    # Reproduce the old journal: two sets incorrectly attached to one record.
    with store.store.transaction() as state:
        for part in again:
            data=asdict(part);data.update(timestamp=part.timestamp.isoformat(),payload=part.payload.hex(),extra_headers=part.extra_headers.hex())
            state['sms_parts'][part_key(part)]=data
            state['records'][original]['part_keys'].append(part_key(part))
        state.pop('assembly_version',None)
    rows=store.list_records()['records']
    assert len(rows)==2 and len({r['id'] for r in rows})==2
    assert all(r['text']=='Один текст' and len(r['part_keys'])==2 for r in rows)
    assert original in {r['id'] for r in rows}
    store.ingest(again)
    assert store.list_records()['total']==2


def test_clear_never_discards_an_unseen_delayed_sms(tmp_path):
    from dataclasses import replace
    from datetime import timedelta
    from telephony_store import chat_key
    store=TelephonyStore(tmp_path/'telephony.json')
    old=decode_deliver(GOLDEN_PDU,1)
    store.ingest([old]);store.clear_sms(chat_key(old.sender))
    # Legacy v2 cutoff also must not destroy future, delayed deliveries.
    with store.store.transaction() as state:state['sms_cleared_at']='2099-01-01T00:00:00+00:00'
    delayed=replace(old,timestamp=old.timestamp-timedelta(days=1))
    store.ingest([old,delayed])
    rows=store.list_records()['records']
    assert len(rows)==1 and rows[0]['timestamp']==delayed.timestamp.isoformat()


def test_single_sms_delete_preserves_other_messages_and_dedupe(tmp_path):
    from dataclasses import replace
    from datetime import timedelta
    store=TelephonyStore(tmp_path/'telephony.json')
    first=decode_deliver(GOLDEN_PDU,1)
    second=replace(first,timestamp=first.timestamp+timedelta(minutes=10))
    store.ingest([first,second])
    rows=store.list_records()['records']
    assert store.delete_message(rows[0]['id'])
    store.ingest([first,second])
    assert [r['id'] for r in store.list_records()['records']]==[rows[1]['id']]
    assert len(store.snapshot()['sms_parts'])==1 and len(store.snapshot()['sms_observations'])==1
    queued=store.add('sms','outgoing',status='processing',text='Пример')
    with pytest.raises(ValueError):store.delete_message(queued)
    call=store.add('call','incoming')
    assert not store.delete_message(call)


def test_message_delete_endpoint_is_scoped_csrf_protected_and_keeps_calls(web):
    store=TelephonyStore();message=store.add('sms','incoming',text='Пример');call=store.add('call','incoming')
    client=web.app.test_client();token=login(client)
    endpoint='/admin/api/telephony/sms/messages/'+message+'/delete'
    assert post(client,token,endpoint,{}).status_code==403
    token=login(client,'super')
    assert client.post(endpoint,json={}).status_code==403
    assert post(client,token,'/admin/api/telephony/sms/messages/'+call+'/delete',{}).status_code==404
    assert post(client,token,endpoint,{}).status_code==200
    assert post(client,token,endpoint,{}).status_code==404
    assert list(store.snapshot()['records'])==[call]


def test_local_client_names_resolve_and_search_without_leaking_tokens(web):
    from telephony_store import chat_key
    phone=web.links.load_links()[web.token_a]['phone']
    store=TelephonyStore();store.add('sms','incoming',phone=phone,text='Пример');store.add('call','incoming',phone=phone)
    client=web.app.test_client();token=login(client,'super')
    response=post(client,token,'/admin/api/telephony/sms/chats',{'search':'Client A'})
    assert response.json['total']==1 and response.json['chats'][0]['contact_name']=='Client A'
    assert web.token_a not in response.text
    contact=post(client,token,'/admin/api/telephony/contacts/resolve',{'phone':phone}).json
    assert contact['id']==chat_key(phone) and contact['contact_name']=='Client A'
    assert post(client,token,'/admin/api/telephony/contacts/resolve',{'phone':'not a phone'}).status_code==400
    assert client.get('/admin/api/telephony/sms/chats/'+chat_key(phone)).json['contact_name']=='Client A'
    assert post(client,token,'/admin/api/telephony/records',{'kind':'call','search':'Client A'}).json['records'][0]['contact_name']=='Client A'
    web.links.update_user(web.token_a,{'user_name':'Новое имя'})
    assert post(client,token,'/admin/api/telephony/sms/chats',{}).json['chats'][0]['contact_name']=='Новое имя'


def test_contact_matching_handles_phone_formats_and_ambiguous_names():
    from telephony_contacts import contact_names
    from telephony_store import chat_key
    result=contact_names({'a':{'phone':'89000000000','user_name':'Анна'},'b':{'phone':'+79000000000','user_name':'Ирина'},'c':{'phone':'+79000000000','user_name':'Анна'}})
    assert result=={chat_key('+79000000000'):'Анна / Ирина'}


def test_notification_time_and_archive_time_survive_restart(tmp_path):
    store=TelephonyStore(tmp_path/'telephony.json')
    port=FakeSerial(responses={'AT+CMGL=4,1':'\r\n'+cmgl()+'OK\r\n'})
    modem=Sim800Modem('FAKE',serial_factory=lambda **kw:port,startup_delay=0,inbox=store)
    modem._capture_notice('+CMTI: "SM",1')
    received=modem.sms_notifications[('SM',1)]
    try:modem.read_sms()
    finally:modem.close()
    part=TelephonyStore(tmp_path/'telephony.json').list_records()['records'][0]['parts'][0]
    assert part['received_at']==received and part['archived_at']>=received
    assert part['smsc_at']!=received and not modem.sms_notifications


def test_urc_burst_is_drained_without_one_line_per_poll_delay():
    port=FakeSerial(chunk=4096)
    modem=Sim800Modem('FAKE',serial_factory=lambda **kw:port,startup_delay=0)
    modem._open_locked()
    port.incoming.extend(b'\r\n\r\n+CMTI: "SM",1\r\n+CMTI: "ME",2\r\n')
    try:modem.poll_events(timeout=.03)
    finally:modem.close()
    assert modem.pending_sms_storages=={'SM','ME'}
    assert len(modem.sms_notifications)==2
