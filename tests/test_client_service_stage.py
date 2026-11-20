"""Synthetic end-to-end checks for the client notification, review and settings changes."""
from datetime import timedelta
from unittest.mock import Mock

from billing_rules import publish, read, sections
from client_feedback import due, prepare
from deployment_yaml import compose
from pass_notifications import timely_grant
from scheduler import Scheduler
from storage import config
from test_appointment_timer import clock
from test_product_integration import product
from test_scheduler import AT, CRM, record
from yclients_api import eligible_clients
import pytest
from test_web_security import csrf, login, post, web
from storage.sql_documents import DocumentStore, migrate_legacy


def test_service_duration_and_fallback_with_multiple_visits():
    morning = record(1, 8, seance_length=3600)
    later = record(1, 11, length='7200')
    for records in ([morning,later], [later,morning]):
        eligible = eligible_clients(records, 1, AT)['1']
        assert eligible['_notice_until'] == (AT+timedelta(hours=1,minutes=30)).timestamp()
        assert eligible['_access_expires'].startswith('2026-11-05T23:59:')
    for ignored in ('', 0, -1, None):
        client = eligible_clients([record(1, 11, seance_length=ignored)], 1, AT)['1']
        assert client['_notice_until'] == AT.timestamp()
        card = {'access_source':'appointment', 'appointment_notice_until':client['_notice_until']}
        assert timely_grant('granted',card,at=AT.timestamp())
        assert not timely_grant('granted',card,at=AT.timestamp()+1)


@pytest.mark.parametrize('late_before_queue', [True, False])
def test_late_recovery_grants_quietly_or_skips_queued_delivery(product, monkeypatch, late_before_queue):
    p = product
    out = p.services.notifications
    from link_manager import access_is_active, set_notify_hook
    set_notify_hook(out.access_transition)
    clock(monkeypatch, AT)
    monkeypatch.setattr(config, 'OUTBOUND_ENABLED', True)
    with p.links.store.transaction() as links:
        links[p.token].update(gate_access=False, id_ycl='1', access_source='appointment')
    item = record(1, 8 if late_before_queue else 12, branch=10, seance_length=1800)
    item['client']['phone'] = p.links.load_links()[p.token]['phone']
    Scheduler(p.links,p.branches,CRM({10:[item]})).run_once()
    assert access_is_active(p.links.load_links()[p.token], AT)
    clock(monkeypatch, AT+timedelta(hours=2))
    out.sender = Mock(return_value={'status':'accepted'})
    out.process_once()
    assert not any(call.args[3]['event']=='access_granted' for call in out.sender.call_args_list)
    with out.store.connect() as db:
        rows = [r[0] for r in db.execute("SELECT state FROM message_outbox WHERE audience='client'")]
    assert rows == ([] if late_before_queue else ['skipped'])
    p.links.revoke_link(p.token)
    with out.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM message_outbox WHERE audience='client' AND event='access_revoked'").fetchone()[0] == 0


def test_review_prompts_follow_confirmed_command_across_page_sessions(product):
    p = product
    gate = p.branches.get_branch(10).buttons[0]
    person = p.access.person_key(p.links.load_links()[p.token])
    for index in range(1,8):
        key = 'review-synthetic-request-'+str(index)
        with p.services.store.connect() as db:
            db.execute('DELETE FROM billing_cooldowns')
        p.access.open_for_token(p.token, gate.id, request_id=key)
        with p.services.store.connect() as db:
            command = db.execute('SELECT command_id FROM gate_requests WHERE id=?',(key,)).fetchone()[0]
            p.services.billing.finalize(command,'command_sent',db=db)
            prepare(db)
            assert due(db, person, '10', 'another-token', key) is False
            assert due(db, person, '10', p.token, key) is (index in (2,6))
            assert due(db, person, '10', p.token, key) is False
            if index == 6:
                db.execute('INSERT INTO client_feedback(person_id,branch_id,stars,message,created) VALUES(?,?,?,?,?)',(person,'10',5,'',0))


def test_billing_rules_version_and_isolated_compose(product):
    with product.services.store.connect() as db:
        revision, text = read(db)
        assert len(sections(text)) >= 6
        publish(db,str(revision),'## Terms\nPlain text')
        with pytest.raises(ValueError):
            publish(db,str(revision),'## Other\nStale update')
        assert read(db) == (revision+1,'## Terms\nPlain text')
    fields = {'PORT':5001,'GATEBOT_DATA_DIR':'/state','BOT_TOKEN':'never-in-yaml'}
    document = compose({'network':'vpn-net','serial_gid':'20'},fields,
                       [{'port':'/dev/ttyUSB0','host_device':'/dev/serial/by-id/test:modem'}])
    for part in ('31500:5001','vpn-net','source: "/dev/serial/by-id/test:modem"','target: "/dev/ttyUSB0"','group_add','gatebot.env'):
        assert part in document
    assert fields['BOT_TOKEN'] not in document


def test_feedback_and_guide_web_entry_points_are_scoped_and_escaped(web, tmp_path, monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    client=web.app.test_client()
    token=csrf(client, '/opn/'+web.token_a)
    assert post(client,token,'/api/client/feedback/send',{'token':web.token_a,'stars':5,'message':'<sample>'}).status_code==200
    assert post(client,token,'/api/client/feedback/send',{'token':'missing','message':'hello'}).status_code==404
    token=login(client)
    assert client.get('/admin/feedback').status_code==403
    assert client.post('/admin/billing-rules',data={'csrf_token':token,'revision':'0','document':'## Title\nLine'}).status_code==403
    token=login(client,'super')
    assert '&lt;sample&gt;' in client.get('/admin/feedback').text
    assert client.post('/admin/billing-rules',data={'csrf_token':token,'revision':'0','document':'## New heading\n<sample>'}).status_code==303
    login(client)
    page=client.get('/admin/billing-rules').text
    assert 'billing-nav' in page and 'New heading' in page and '&lt;sample&gt;' in page and 'id="rulesText"' not in page
