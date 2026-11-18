"""Chrome settings and client hours overlay; no physical/external effects."""
import os
import re
import pytest
from branch_hours import BranchHoursService,CLOSED_MESSAGE
from test_branch_hours_web import hours_web
from test_ui_browser import browser_ui,sign_in
from test_web_security import web


pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


def test_weekly_settings_save_overnight_and_mobile(hours_web,browser_ui,tmp_path):
    web=hours_web;page,origin,expect,errors=browser_ui
    before=web.links.load_links()
    sign_in(page,origin,'tenant-a')
    page.goto(origin+'/admin/settings')
    expect(page.locator('#branchHoursPanel')).to_be_visible()
    expect(page.locator('#branchHoursEnabled')).to_be_enabled()
    page.locator('#branchHoursEnabled').check()
    monday=page.locator('.branch-hours-day').nth(0)
    monday.get_by_role('button',name='Добавить интервал',exact=True).click()
    page.get_by_label('Понедельник — начало',exact=True).fill('22:00')
    page.get_by_label('Понедельник — конец',exact=True).fill('02:00')
    page.locator('.branch-hours-day').nth(1).get_by_role('button',name='Весь день',exact=True).click()
    with page.expect_response('**/admin/api/branches/12/access-hours') as saved:
        page.get_by_role('button',name='Сохранить расписание',exact=True).click()
    assert saved.value.status==200
    expect(page.locator('#branchHoursPanel')).to_contain_text('Расписание сохранено')
    persisted=web.branches.load_branches()['12']['access_schedule']
    assert persisted['days']['0']==[{'start':'22:00','end':'02:00'}]
    assert persisted['days']['1']==[{'start':'00:00','end':'24:00'}]
    page.reload();expect(page.get_by_label('Понедельник — начало',exact=True)).to_have_value('22:00')
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#branchHoursPanel').screenshot(path=str(tmp_path/'hours-settings-mobile.png'))
    assert page.locator('#branchHoursPanel').evaluate('(panel)=>panel.scrollWidth<=panel.clientWidth')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert web.links.load_links()==before and web.calls==[] and not errors


def test_web_and_qr_closed_immediately_without_phone_or_sms(hours_web,browser_ui,tmp_path):
    web=hours_web;page,origin,expect,errors=browser_ui
    services=web.module.product_services();identity=services.identity
    web.links.toggle_user_access(web.token_a,True)
    before=web.links.load_links();sent=[]
    identity.sender=lambda *args:sent.append(args) or {'status':'sent'}
    BranchHoursService(identity).save('super','12',{'enabled':True,'days':{}})
    gate=web.branches.get_branch(12).buttons[0]
    qr_service=services.provider(web.auth,web.module.access_service)['gate_qr']
    qr=qr_service.admin_view('super','12',gate.stable_id,'create')['url'].rsplit('/',1)[1]
    response=page.goto(origin+'/opn/'+web.token_a)
    assert response.status==200 and CLOSED_MESSAGE in response.text()
    expect(page.locator('#accessDescription')).to_have_text(CLOSED_MESSAGE)
    expect(page.locator('.button-gate')).to_be_visible()
    expect(page.locator('.button-gate')).to_be_disabled()
    assert re.search(r'<button[^>]+data-button-id="[^"]+"[^>]*\bdisabled\b',response.text())
    response=page.goto(origin+'/gate/'+qr)
    assert response.status==200 and CLOSED_MESSAGE in response.text()
    expect(page.locator('#qrDescription')).to_have_text(CLOSED_MESSAGE)
    expect(page.locator('#qrPhoneForm')).to_be_hidden()
    expect(page.locator('#qrCodeForm')).to_be_hidden()
    expect(page.locator('#qrOpen')).to_be_hidden()
    page.screenshot(path=str(tmp_path/'hours-closed-qr.png'))
    assert web.links.load_links()==before and sent==[] and web.calls==[] and not errors
