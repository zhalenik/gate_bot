"""A real browser countdown, with a fake SMS transport and isolated database."""
import os
import re
import pytest
from test_web_security import web
from test_ui_browser import browser_ui

pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


def test_otp_button_countdown_reenable_reload_and_delivery_state(web,browser_ui,tmp_path,monkeypatch):
    from storage.sql_documents import migrate_legacy,DocumentStore
    page,origin,expect,errors=browser_ui
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    identity.attach_console_account('super','+15550300303')
    clock=[2000000000.0];identity.clock=lambda:clock[0]
    sends=[]
    identity.sender=lambda *args:sends.append(args) or {'status':'success','delivery':'queued'}
    delivery={'status':'queued','delivery':'queued'}
    identity.delivery_status=lambda *args:delivery.copy()
    raw=identity.invite('super','12',client_token=web.token_a)['link'].rsplit('/',1)[1]
    page.clock.install()
    page.goto(origin+'/invite/'+raw)
    page.locator('[name=name]').fill('Example admin')
    page.locator('[name=username]').fill('example@example.invalid')
    expect(page.locator('#otpResend')).to_be_enabled()
    page.locator('#otpResend').click()
    expect(page.locator('#otpResend')).to_be_disabled()
    expect(page.locator('#otpResend')).to_contain_text('60 с')
    expect(page.locator('#otpDeliveryStatus')).to_contain_text('в очереди')
    count=len(sends)
    page.locator('#otpResend').dispatch_event('click')
    assert len(sends)==count
    clock[0]+=12;page.reload()
    expect(page.locator('[name=name]')).to_have_value('')
    expect(page.locator('[name=username]')).to_have_value('')
    expect(page.locator('#otpResend')).to_be_enabled()
    page.locator('[name=name]').fill('Example admin')
    page.locator('[name=username]').fill('example@example.invalid')
    with page.expect_response('**/code') as throttled:page.locator('#otpResend').click()
    assert throttled.value.status==429
    expect(page.locator('#otpResend')).to_be_disabled()
    expect(page.locator('#otpResend')).to_contain_text('48 с')
    clock[0]+=49;delivery.update(status='network_accepted',delivery='network_accepted')
    page.clock.fast_forward(49000)
    expect(page.locator('#otpResend')).to_be_enabled()
    expect(page.locator('#otpDeliveryStatus')).to_be_empty()
    with page.expect_response('**/code') as repeated:page.locator('#otpResend').click()
    assert repeated.value.status==200
    expect(page.locator('#otpResend')).to_be_disabled()
    assert len(sends)==count+1 and web.calls==[]


def test_branch_registration_reload_clears_all_fields_and_context(web,browser_ui,tmp_path,monkeypatch):
    from storage.sql_documents import migrate_legacy,DocumentStore
    page,origin,expect,errors=browser_ui
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    sent=[];identity.sender=lambda *args:sent.append(args) or {'status':'success','delivery':'queued'}
    page.goto(origin+'/register-branch')
    for field,value in [('branch_id','76543'),('branch_name','Test branch'),('name','Test owner'),('phone','+15550400404'),('username','new-owner@example.invalid'),('password','test-not-persisted-password')]:
        page.locator('[name='+field+']').fill(value)
    with page.expect_response('**/register-branch/code') as response:page.locator('#otpResend').click()
    assert response.value.status==200
    expect(page.locator('[name=phone]')).to_be_editable()
    expect(page.locator('[name=name]')).to_be_editable()
    expect(page.locator('[name=branch_name]')).to_be_editable()
    assert 'test-not-persisted-password' not in page.evaluate('JSON.stringify(sessionStorage)')
    page.reload()
    for field in ['branch_id','branch_name','name','phone','username','password','code']:
        expect(page.locator('[name='+field+']')).to_have_value('')
    expect(page.locator('#otpResend')).to_be_enabled()
    expect(page.locator('#otpDeliveryStatus')).to_be_empty()
    assert page.evaluate('sessionStorage.getItem("gatebot.registration.pending")') is None
    assert len(sent)==1 and web.calls==[]



@pytest.mark.parametrize('transition',['resend','change_details'])
def test_late_otp_status_cannot_restore_previous_registration_state(web,browser_ui,tmp_path,monkeypatch,transition):
    from storage.sql_documents import migrate_legacy,DocumentStore
    page,origin,expect,errors=browser_ui
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    clock=[2000000000.0];identity.clock=lambda:clock[0]
    sent=[];identity.sender=lambda *args:sent.append(args) or {'status':'success','delivery':'queued'}
    page.clock.install()
    held=[]
    page.route('**/register-branch/code-status',lambda route:held.append(route))
    page.goto(origin+'/register-branch')
    for field,value in [('branch_id','76543'),('branch_name','Test branch'),('name','Test owner'),('phone','+15550400404'),('username','new-owner@example.invalid'),('password','test-not-persisted-password')]:
        page.locator('[name='+field+']').fill(value)
    with page.expect_response('**/register-branch/code') as first:
        page.locator('#otpResend').click()
    old=first.value.json()
    expect(page.locator('#otpDeliveryStatus')).to_contain_text('в очереди')
    # A polling request remains pending while a newer UI action completes.
    with page.expect_request('**/register-branch/code-status') if not held else _no_wait():
        page.clock.fast_forward(2000)
    assert len(held)==1
    if transition=='resend':
        clock[0]+=61;page.clock.fast_forward(61000)
        expect(page.locator('#otpResend')).to_be_enabled()
        with page.expect_response('**/register-branch/code') as second:
            page.locator('#otpResend').click()
        newest=second.value.json()
        assert newest['challenge_id']!=old['challenge_id']
        expect(page.locator('#otpResend')).to_be_disabled()
    else:
        page.locator('[name=name]').fill('Changed owner')
        expect(page.locator('[name=phone]')).to_be_editable()
        expect(page.locator('#otpDeliveryStatus')).to_be_empty()
    # This earlier response must neither change challenge nor restart its timer.
    held[0].fulfill(json={**old,'retry_after':0,'delivery':{'status':'network_accepted','delivery':'network_accepted'}})
    page.wait_for_load_state('networkidle')
    if transition=='resend':
        expect(page.locator('#otpResend')).to_be_disabled()
        expect(page.locator('#otpDeliveryStatus')).to_contain_text('в очереди')
        code=re.search(r'\b[0-9]{6}\b',sent[-1][2]).group()
        page.locator('[name=code]').fill(code)
        with page.expect_response('**/register-branch/complete') as completed:
            page.get_by_role('button',name='Завершить регистрацию',exact=True).click()
        assert completed.value.status==200
        assert completed.value.request.post_data_json['challenge_id']==newest['challenge_id']
        expect(page.get_by_role('link',name='Войти',exact=True)).to_be_visible()
    else:
        expect(page.locator('#otpDeliveryStatus')).to_be_empty()
        expect(page.locator('[name=name]')).to_have_value('Changed owner')
        assert page.evaluate('sessionStorage.getItem("gatebot.registration.pending")') is None
    assert web.calls==[]


def _no_wait():
    from contextlib import nullcontext
    return nullcontext()
