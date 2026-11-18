import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from identity_service import ADMIN_EVENTS
from access_service import AccessService
from admin_auth import AdminAuth
from application_services import ApplicationServices
from branch_manager import BranchManager
from link_manager import LinkManager, set_notify_hook
from storage import config
from storage.sql_documents import migrate_legacy


@pytest.fixture
def product(tmp_path, monkeypatch):
    branches = BranchManager(tmp_path / 'branches.json')
    branches.add_branch({'branch_id': 10, 'name': 'North', 'buttons': [
        {'type': 'gate', 'label': 'Entry', 'phone': '+70000000001'}]})
    branches.add_branch({'branch_id': 20, 'name': 'South', 'buttons': [
        {'type': 'gate', 'label': 'Entry', 'phone': '+70000000003'}]})
    links = LinkManager(tmp_path / 'links.json')
    token = links.create_link({'branch_id': 10, 'phone': '+70000000002', 'user_name': 'Client',
                              'mailing_enabled': True, 'gate_access': True})
    links.bind_identity('telegram', '77', token=token)
    auth = AdminAuth(tmp_path / 'admins.json')
    password_hash=auth.hash_password('Test-password-2026')
    auth.save_admins({'supertest':{'password_hash':password_hash,'is_super':True,'branch_id':None},
                      'branchtest':{'password_hash':password_hash,'is_super':False,'branch_id':10}})
    migrate_legacy(tmp_path, activate=True)
    services = ApplicationServices(links, branches)
    services.identity.attach_console_account('supertest', '+70000000004')
    services.identity.attach_console_account('branchtest', '+70000000005')
    # Delivery tests explicitly opt in; account defaults have separate regressions.
    for username in ('supertest', 'branchtest'):
        services.identity.save_admin_preferences(username, {'events': {event: {'telegram': True}
            for event in ADMIN_EVENTS if username == 'supertest' or event not in {'sms_received', 'call_received'}}})
    for username, platform, uid in [('supertest','telegram','90'),('branchtest','telegram','91')]:
        ticket = services.identity.issue_bind_ticket(username, platform)
        services.identity.bind_ticket(platform, uid, ticket)
    calls = []
    access = AccessService(links, branches, opener=lambda *a, **kw: calls.append((a,kw)) or
                           {'status':'success', 'delivery':'queued'}, billing=services.billing, identity=services.identity,
                           event_sink=services.notifications.event)
    monkeypatch.setattr(config, 'GATE_DRY_RUN', False)
    yield SimpleNamespace(services=services, access=access, links=links, branches=branches,
                          token=token, auth=auth, calls=calls, root=tmp_path)
    set_notify_hook(None)


def test_last_unit_guard_uses_reserved_quota_and_rechecks_revocation(product):
    p=product
    p.services.billing.grant_gift(10, 1, actor='supertest', days=10, mode='replace', reason='test')
    gate=p.branches.get_branch(10).buttons[0]
    result=p.access.open_for_token(p.token, gate.id, request_id='request-first-unit')
    assert result['status']=='success'
    assert p.services.billing.subscription_view(10)['remaining']==0
    command=p.calls[0][1].get('command_id')
    with p.services.store.connect() as db:
        command=db.execute('SELECT id FROM hardware_commands').fetchone()['id']
    assert p.access.authorize_command(command) is True
    p.links.revoke_link(p.token)
    assert p.access.authorize_command(command) is False
    assert p.services.billing.subscription_view(10)['remaining']==1


def test_shared_cooldown_and_replayed_request_do_not_submit_twice(product):
    p=product;gate=p.branches.get_branch(10).buttons[0]
    one=p.access.open_for_token(p.token,gate.id,request_id='request-one-valid')
    repeated=p.access.open_for_token(p.token,gate.id,request_id='request-one-valid')
    assert one['status']==repeated['status']=='success'
    assert p.access.open_for_identity('telegram','77','10',gate.id)['http_status']==429
    assert len(p.calls)==1


def test_queued_command_rejects_changed_gate_route(product):
    p=product;gate=p.branches.get_branch(10).buttons[0]
    p.access.open_for_token(p.token,gate.id)
    with p.services.store.connect() as db:
        command=db.execute('SELECT id FROM hardware_commands').fetchone()['id']
    p.branches.update_settings(10, {'name':'North','timezone':'Europe/Moscow','enabled':True,'logo':'',
                                   'buttons':[{'type':'gate','label':'Entry','phone':'+70000000006'}]})
    assert p.access.authorize_command(command) is False
    assert p.services.billing.subscription_view(10)['reserved']==0


def test_client_optout_after_enqueue_prevents_delivery(product, monkeypatch):
    p=product;out=p.services.notifications
    out.enqueue({**p.links.load_links()[p.token], 'token':p.token, 'event':'granted'})
    p.links.set_mailing_enabled(p.token,False)
    sender=Mock(return_value={'status':'accepted'});out.sender=sender
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    out.process_once()
    sender.assert_not_called()
    with out.store.connect() as db:
        assert db.execute('SELECT state FROM message_outbox').fetchone()['state']=='skipped'


def test_admin_events_scoped_and_role_rechecked_before_send(product, monkeypatch):
    p=product;out=p.services.notifications
    out.event('gate_requested',20,{'event_id':'south-command'})
    out.event('sms_received',None,{'event_id':'incoming-one','text':'private test message'})
    with out.store.connect() as db:
        targets=[out._decrypt(r['payload'])['target'] for r in db.execute('SELECT payload FROM message_outbox')]
    assert targets==['90','90']
    with p.auth.store.transaction() as records:
        records['supertest']['disabled']=True
    out.sender=Mock(return_value={'status':'accepted'})
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    out.process_once()
    out.sender.assert_not_called()


def test_telephony_bridge_deduplicates_ring_and_long_sms(product):
    out=product.services.notifications
    out.bridge_telephony()
    record=out.telephony.add('call','incoming',phone='+70000000008',status='ringing')
    out.bridge_telephony()
    out.telephony.update(record,status='ended')
    out.bridge_telephony()
    with out.store.connect() as db:
        assert db.execute('SELECT count(*) FROM message_outbox WHERE event="call_received"').fetchone()[0]==1


def test_otp_uses_priority_queue_and_private_superadmin_archive(product,monkeypatch):
    out=product.services.notifications
    import sim800c
    queue=SimpleNamespace(dry_run=False,journal=out.telephony,add_manual=Mock(return_value={'status':'success','delivery':'queued'}))
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    monkeypatch.setattr(sim800c,'_get_queue',lambda:queue)
    result=out.send('sms','+70000000009','one-time test code',{'purpose':'otp','idempotency_key':'code-challenge'})
    assert result['delivery']=='queued'
    record=queue.journal.snapshot()['records'][result['record_id']]
    assert record['text']=='one-time test code' and record['purpose']=='verification'
    assert record['phone']=='+70000000009' and record['status']=='queued'
    assert queue.add_manual.call_args.kwargs['sensitive'] is True
    assert queue.add_manual.call_args.kwargs['priority'] is True
    assert b'one-time test code' not in Path(out.store.path).read_bytes()
    out.send('sms','+70000000009','one-time test code',{'purpose':'otp','idempotency_key':'code-challenge'})
    assert queue.add_manual.call_count==1
    assert len(queue.journal.snapshot()['records'])==1


def test_chatpush_keeps_payload_out_of_url_and_verifies_tls():
    from chatpush import ChatPushClient
    session=Mock();session.request.return_value=SimpleNamespace(status_code=200,json=lambda:{'delivery':{'id':42,'status_id':2}})
    client=ChatPushClient('test-token',session=session)
    result=client.send('+70000000001','test body',external_id='test-effect')
    assert result['status']=='accepted' and result['confirmed'] is False
    args,kwargs=session.request.call_args
    assert args==('POST','https://api.chatpush.ru/api/v1/delivery')
    assert kwargs['verify'] is True and kwargs['allow_redirects'] is False
    assert kwargs['json']=={'text':'test body','phone':'70000000001'}
    assert kwargs['headers']['authorization']=='test-token' and 'checksum' not in kwargs['headers']


def test_bot_admin_requires_verified_binding_and_keeps_branch_scope(product):
    from bot_state import BotStateStore
    from bot_workflows import BotWorkflows
    p=product;flows=BotWorkflows(p.access,BotStateStore(p.root/'bot-test.sqlite3'))
    assert not any(a.get('callback','').startswith('x:admin') for a in flows.actions('telegram','77'))
    assert any(a.get('callback','').startswith('x:admin') for a in flows.actions('telegram','91'))
    with pytest.raises(PermissionError):
        flows.action('telegram','91','x:issue:20')
    flows.action('telegram','91','x:issue:10')
    confirm=flows.message('telegram','91','+70000000123')
    callback=confirm['actions'][0]['callback']
    before=len(p.links.load_links())
    result=flows.action('telegram','91',callback)
    assert '/opn/' in result['text'] and len(p.links.load_links())==before+1
    with pytest.raises(ValueError):
        flows.action('telegram','91',callback)
    assert len(p.links.load_links())==before+1


def test_max_menu_is_edited_without_duplicate_messages(tmp_path):
    from test_max_bot import API,Service,TOKEN,NOW,raw_message,raw_callback
    from max_bot import MaxBot
    from bot_state import BotStateStore
    class EditableAPI(API):
        def __init__(self):
            super().__init__();self.edited=[]
        def edit_message(self,mid,**body):
            self.edited.append((mid,body));return {'success':True}
    api=EditableAPI();bot=MaxBot(api,Service(),BotStateStore(tmp_path/'max-menu.sqlite3'),token=TOKEN,clock=lambda:NOW)
    bot.process_update(raw_message(mid='first'))
    bot.process_update(raw_message(mid='second'))
    bot.process_update(raw_callback())
    assert len(api.sent)==1 and len(api.edited)==2


def test_telegram_menu_is_edited_without_duplicate_messages(tmp_path):
    import asyncio
    from unittest.mock import AsyncMock
    from test_telegram_bot import Service,incoming
    from telegram_bot import TelegramBot
    from bot_state import BotStateStore
    api=SimpleNamespace(edit_message_text=AsyncMock())
    bot=TelegramBot(Service(),BotStateStore(tmp_path/'tg-menu.sqlite3'),token='test-menu-token',bot=api)
    update=incoming();update.effective_message.reply_text.return_value=SimpleNamespace(message_id=101)
    asyncio.run(bot.on_menu(update,None));asyncio.run(bot.on_menu(update,None))
    assert update.effective_message.reply_text.await_count==1
    assert api.edit_message_text.await_count==1


def test_legacy_outbox_transfer_is_per_channel_and_never_replays_sent(product):
    from runtime_state import RuntimeState
    p=product; out=p.services.notifications; state=RuntimeState(p.root/'runtime.sqlite3')
    payload={**p.links.load_links()[p.token], 'token':p.token, 'event':'granted'}
    state.enqueue('telegram',payload)
    state.enqueue('max',{**payload,'max_id':'888'})
    claimed=state.next_notification('max')
    state.finish_notification(claimed['id'],True)
    out.migrate_legacy_outbox();out.migrate_legacy_outbox()
    with out.store.connect() as db:
        rows=db.execute('SELECT channel,state FROM message_outbox').fetchall()
    assert [(r['channel'],r['state']) for r in rows]==[('telegram','pending')]
    assert state.notification_counts()=={'migrated':1,'sent':1}


def test_unknown_sms_effect_is_never_reported_as_queued_after_restart(product,monkeypatch):
    import sim800c
    out=product.services.notifications
    queue=SimpleNamespace(dry_run=False,journal=out.telephony,add_manual=Mock(return_value={
        'status':'success','delivery':'queued','command_id':'pending-hardware'}))
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    monkeypatch.setattr(sim800c,'_get_queue',lambda:queue)
    context={'purpose':'otp','idempotency_key':'restart-test'}
    assert out.send('sms','+70000000009','test code',context)['delivery']=='queued'
    out.recover()
    assert out.send('sms','+70000000009','test code',context)['delivery']=='unknown'
    assert queue.add_manual.call_count==1


def test_admin_grant_has_no_menu_expiry_when_underlying_manual_grant_expired(product):
    p=product
    with p.links.store.transaction() as links:
        client=next(v for v in links.values() if v.get('phone')=='+70000000005' and v['branch_id']==10)
        client.update(access_expires='2000-01-01T00:00:00+03:00',gate_access=True,access_source='manual')
    menu=p.access.menu_for_identity('telegram','91')
    assert menu[0]['active'] is True and menu[0]['access_expires'] is None


def test_notifications_wait_for_bot_readiness_without_consuming_delivery(product,monkeypatch):
    import telegram_bot
    p=product;out=p.services.notifications
    out.enqueue({**p.links.load_links()[p.token], 'token':p.token, 'event':'granted'})
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    monkeypatch.setattr(telegram_bot,'status',lambda:{'ready':False})
    send=Mock(return_value=True);monkeypatch.setattr(telegram_bot,'notify_text',send)
    out.process_once()
    with out.store.connect() as db:
        assert db.execute('SELECT state FROM message_outbox').fetchone()['state']=='pending'
        assert db.execute('SELECT count(*) FROM message_effects').fetchone()[0]==0
    send.assert_not_called()
    monkeypatch.setattr(telegram_bot,'status',lambda:{'ready':True})
    out.process_once()
    assert send.call_count==1


def test_incomplete_sms_at_startup_is_forwarded_when_it_completes(product):
    out=product.services.notifications
    old=out.telephony.add('sms','incoming',phone='+70000000008',text='old',status='received')
    partial=out.telephony.add('sms','incoming',phone='+70000000008',text='partial',status='partial')
    out.bridge_telephony()
    out.telephony.update(partial,text='complete',status='received')
    out.bridge_telephony();out.bridge_telephony()
    with out.store.connect() as db:
        rows=db.execute('SELECT payload FROM message_outbox WHERE event="sms_received"').fetchall()
    assert len(rows)==1 and 'complete' in out._decrypt(rows[0]['payload'])['text']


def test_stop_during_notification_batch_leaves_unsent_rows_pending(product,monkeypatch):
    out=product.services.notifications
    out.event('gate_requested',20,{'event_id':'first'})
    out.event('gate_requested',20,{'event_id':'second'})
    calls=[]
    def send(*args):
        calls.append(args);out.request_stop();return {'status':'accepted'}
    out.sender=send;monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    out.process_once()
    assert len(calls)==1
    with out.store.connect() as db:
        assert sorted(r['state'] for r in db.execute('SELECT state FROM message_outbox'))==['accepted','pending']


def test_admin_access_change_notice_is_independent_of_client_optout(product):
    out=product.services.notifications
    payload={**product.links.load_links()[product.token], 'token':product.token,'event':'granted','mailing_enabled':False}
    out.access_transition(payload);out.access_transition(payload)
    with out.store.connect() as db:
        rows=db.execute('SELECT audience,event FROM message_outbox').fetchall()
    assert len(rows)==2 and all(r['audience']=='admin' and r['event']=='access_changed' for r in rows)
