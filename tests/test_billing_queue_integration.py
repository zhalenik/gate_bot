"""End-to-end queue guards with a fake modem: no serial or external calls."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_product_integration import product
import sim800c


class Modem:
    port='FAKE'
    last_status={}
    def __init__(self):self.ready=threading.Event();self.release=threading.Event();self.attempts=0;self.on_dial=None
    def call(self,phone,duration,*,not_after,before_dial):
        self.ready.set()
        if not self.release.wait(3):raise RuntimeError('test synchronization failed')
        before_dial()
        self.attempts+=1
        if self.on_dial:self.on_dial()
        return {'delivery':'command_sent'}
    def request_stop(self):self.release.set()
    def close(self):pass


def queue_for(product,monkeypatch):
    modem=Modem();queue=sim800c.CallQueue(modem,enabled=True,dry_run=False,ttl=30)
    queue.journal=Mock();queue.command_guard=lambda command:product.access.authorize_command(command,start=True)
    queue.command_observer=product.access.command_finished
    monkeypatch.setattr(sim800c,'_get_queue',lambda:queue);product.access.opener=None
    return queue,modem


def finish(queue,modem):
    modem.release.set();queue._thread.join(4)
    assert not queue._thread.is_alive()


def test_queue_rechecks_revocation_before_fake_atd_and_releases_reserve(product,monkeypatch):
    p=product;queue,modem=queue_for(p,monkeypatch)
    p.services.billing.grant_gift(10,1,actor='owner',days=2,mode='replace')
    gate=p.branches.get_branch(10).buttons[0]
    assert p.access.open_for_token(p.token,gate.id)['status']=='success'
    assert modem.ready.wait(2)
    p.links.revoke_link(p.token);finish(queue,modem)
    assert modem.attempts==0
    view=p.services.billing.subscription_view(10)
    assert view['reserved']==0 and view['spent']==0
    assert p.access.cooldown_remaining(p.links.load_links()[p.token])==0
    queue.shutdown()


def test_durable_started_precedes_attempt_and_callbacks_charge_only_once(product,monkeypatch):
    p=product;queue,modem=queue_for(p,monkeypatch)
    modem.on_dial=lambda:assert_started(p)
    gate=p.branches.get_branch(10).buttons[0]
    result=p.access.open_for_token(p.token,gate.id,request_id='queued-stable-id')
    assert modem.ready.wait(2)
    duplicate=p.access.open_for_token(p.token,gate.id,request_id='queued-stable-id')
    assert duplicate['command_id']==result['command_id']
    finish(queue,modem)
    assert modem.attempts==1 and p.services.billing.subscription_view(10)['spent']==1
    p.access.command_finished(result['command_id'],'command_sent')
    assert p.services.billing.subscription_view(10)['spent']==1
    queue.shutdown()


def assert_started(product):
    with product.services.store.connect() as db:
        row=db.execute('SELECT status,charged FROM hardware_commands').fetchone()
        assert row['status']=='started' and row['charged']==1


def test_terminal_accounting_failure_stops_queue_with_diagnostic(monkeypatch):
    modem=Modem();queue=sim800c.CallQueue(modem,enabled=True,dry_run=False,ttl=30)
    queue.journal=Mock();queue.command_guard=lambda _:True
    def observer(command,status,code):
        if status=='command_sent':raise OSError('private diagnostic that must not leak')
    queue.command_observer=observer
    first=queue.add_call('+70000000001',branch_id=10,command_id='managed-command',gate_id='gate')
    assert first['status']=='success' and modem.ready.wait(2)
    queue.add_call('+70000000002',branch_id=10,command_id='queued-command',gate_id='other')
    finish(queue,modem)
    assert modem.attempts==1 and queue.processing is False
    assert queue.status()['error_code']=='accounting_unavailable'
    assert queue.add_call('+70000000003')['status']=='error'
    queue.shutdown()


def test_route_revision_never_coalesces_new_request_into_cancelled_route(product):
    p=product;old=p.branches.get_branch(10).buttons[0]
    p.access.open_for_token(p.token,old.id)
    p.branches.update_settings(10,{'buttons':[{'type':'gate','label':'Entry','phone':'+70000000007','stable_id':old.stable_id}]})
    new=p.branches.get_branch(10).buttons[0]
    assert new.stable_id==old.stable_id and new.id!=old.id
    assert p.access.open_for_token(p.token,new.id)['status']=='success'
    with p.services.store.connect() as db:
        rows=db.execute('SELECT status FROM hardware_commands ORDER BY created').fetchall()
        assert [row['status'] for row in rows]==['cancelled','queued']
    assert len(p.calls)==2
