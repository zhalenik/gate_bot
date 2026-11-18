"""Weekly access overlay, tenant settings and last-moment queue checks; offline."""
from datetime import datetime
import json

import pytest

from branch_hours import BranchHoursService,CLOSED_MESSAGE,normalize_schedule,schedule_state
from test_identity_product import identities
from test_product_integration import product
from test_gate_qr import qr,verify


def schedule(**days):return {'enabled':True,'days':days}
def instant(value):return datetime.fromisoformat(value)
def closed(branches,branch):
    with branches.store.transaction() as values:values[str(branch)]['access_schedule']=schedule()


def test_default_is_unrestricted_and_day_boundaries_are_half_open():
    assert schedule_state(None,at=instant('2026-11-16T05:00:00+00:00'))['open']
    value=schedule(**{'0':[{'start':'09:00','end':'18:00'}]})
    assert not schedule_state(value,at=instant('2026-11-16T05:59:59+00:00'))['open']
    opened=schedule_state(value,at=instant('2026-11-16T06:00:00+00:00'))
    assert opened['open'] and opened['next_change_at']=='2026-11-16T15:00:00+00:00'
    assert not schedule_state(value,at=instant('2026-11-16T15:00:00+00:00'))['open']


def test_overnight_sunday_crosses_week_and_breaks_are_respected():
    night=schedule(**{'6':[{'start':'22:00','end':'02:00'}]})
    assert schedule_state(night,at=instant('2026-11-16T01:59:00+03:00'))['open']
    assert not schedule_state(night,at=instant('2026-11-16T02:00:00+03:00'))['open']
    windows=schedule(**{'0':[{'start':'09:00','end':'12:00'},{'start':'13:00','end':'24:00'}]})
    assert not schedule_state(windows,at=instant('2026-11-16T12:30:00+03:00'))['open']
    assert schedule_state(windows,at=instant('2026-11-16T23:59:59+03:00'))['open']
    assert not schedule_state(windows,at=instant('2026-11-17T00:00:00+03:00'))['open']


def test_dst_uses_branch_local_wall_clock_and_transition_closes_or_opens():
    value=schedule(**{'6':[{'start':'02:30','end':'04:00'}]})
    state=schedule_state(value,'Europe/Berlin',instant('2026-03-29T00:00:00+00:00'))
    assert not state['open'] and state['next_change_at']=='2026-03-29T01:00:00+00:00'
    assert schedule_state(value,'Europe/Berlin',instant('2026-03-29T01:00:00+00:00'))['open']
    repeated=schedule(**{'6':[{'start':'02:15','end':'02:45'}]})
    for value in ('2026-10-25T00:30:00+00:00','2026-10-25T01:30:00+00:00'):
        assert schedule_state(repeated,'Europe/Berlin',instant(value))['open']


@pytest.mark.parametrize('value',[
    {'enabled':'true','days':{}},schedule(**{'7':[]}),schedule(**{'0':[{'start':'9:00','end':'18:00'}]}),
    schedule(**{'0':[{'start':'24:00','end':'23:59'}]}),schedule(**{'0':[{'start':'09:00','end':'09:00'}]}),
    schedule(**{'0':[{'start':'09:00','end':'25:00'}]}),schedule(**{'0':[{'start':'09:00','end':'18:00'}]*5}),
])
def test_invalid_schedule_fails_validation_and_runtime_closed(value):
    with pytest.raises(ValueError):normalize_schedule(value)
    assert not schedule_state(value)['open']


def test_scoped_settings_do_not_change_passes_or_enqueue_notices(identities):
    state=identities;service=BranchHoursService(state.service)
    links=state.links.load_links()
    with pytest.raises(PermissionError):service.save('north','12',schedule())
    service.save('north','11',schedule())
    assert service.read('north','11')['state']['open'] is False
    assert state.links.load_links()==links and state.sent==[]
    service.save('owner','12',{'enabled':False,'days':{}})


def test_web_and_both_bot_identity_paths_deny_without_mutating_permanent_pass(product):
    p=product;gate=p.branches.get_branch(10).buttons[0]
    p.links.bind_identity('max','88',token=p.token)
    before=p.links.load_links()
    closed(p.branches,10)
    for result in (p.access.open_for_token(p.token,gate.id),p.access.open_for_identity('telegram','77','10',gate.id),p.access.open_for_identity('max','88','10',gate.id)):
        assert result['http_status']==403 and result['code']=='outside_working_hours'
        assert result['message']==CLOSED_MESSAGE
    assert p.links.load_links()==before and p.calls==[]
    with p.services.store.connect() as db:assert db.execute('SELECT count(*) FROM hardware_commands').fetchone()[0]==0


def test_queued_command_is_cancelled_when_hours_close_without_charging(product):
    p=product;gate=p.branches.get_branch(10).buttons[0]
    result=p.access.open_for_token(p.token,gate.id,request_id='hours-queued-command')
    assert result['status']=='success'
    closed(p.branches,10)
    with p.services.store.connect() as db:command_id=db.execute('SELECT id FROM hardware_commands').fetchone()[0]
    assert p.access.authorize_command(command_id,start=True) is False
    view=p.services.billing.subscription_view(10)
    assert view['spent']==0 and view['reserved']==0
    with p.services.store.connect() as db:
        row=db.execute('SELECT status,error_code FROM hardware_commands').fetchone()
        assert row['status']=='cancelled' and row['error_code']=='outside_working_hours'


def test_qr_closed_never_asks_phone_creates_challenge_or_sends_sms(qr):
    closed(qr.base.branches,'11')
    assert qr.svc.state(qr.raw)['state']=='closed'
    assert qr.svc.request_code(qr.raw,None,'browser')['message']==CLOSED_MESSAGE
    assert qr.svc.open(qr.raw,None,'request-closed')['code']=='outside_working_hours'
    assert qr.svc.challenge_status(qr.raw,'browser')['ready'] is False
    with qr.base.product.connect() as db:assert db.execute('SELECT count(*) FROM identity_otp').fetchone()[0]==0
    assert qr.base.sent==[] and qr.calls==[]


def test_remembered_qr_device_stays_remembered_but_cannot_bypass_hours(qr):
    device=verify(qr);before=qr.base.links.load_links();sent=len(qr.base.sent)
    closed(qr.base.branches,'11')
    assert qr.svc.state(qr.raw,device)['state']=='closed'
    assert qr.svc.open(qr.raw,device,'closed-device')['http_status']==403
    with qr.base.branches.store.transaction() as values:values['11']['access_schedule']={'enabled':False,'days':{}}
    assert qr.svc.state(qr.raw,device)['state']=='active'
    assert len(qr.base.sent)==sent and qr.base.links.load_links()==before and qr.calls==[]
