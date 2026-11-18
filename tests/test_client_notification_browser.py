"""Real browser client preferences; delivery and external bot transitions are fake."""
import os
from types import SimpleNamespace

import pytest

from storage.sql_documents import DocumentStore, migrate_legacy
from test_ui_browser import browser_ui
from test_web_security import web


pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


@pytest.fixture
def client_preferences(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    sent=[];identity.sender=lambda *args:sent.append(args) or {'status':'sent'}
    monkeypatch.setattr(identity,'_client_messengers_state',lambda *args:{'connected':True,'provider':'chatpush','reason':'ready'})
    return SimpleNamespace(identity=identity,sent=sent)


def test_channels_reflect_actual_state_unsubscribe_confirmation_and_restore(web,browser_ui,client_preferences,tmp_path):
    page,origin,expect,errors=browser_ui;identity=client_preferences.identity
    web.links.bind_identity('telegram','81001',token=web.token_a)
    identity.save_prefs_for_token(web.token_a,{'enabled':True,'channels':{'telegram':True,'max':True,'messengers':True}})
    page.goto(origin+'/preferences/'+web.token_a)
    expect(page.get_by_role('switch',name='Telegram Bot',exact=True)).to_be_checked()
    expect(page.get_by_role('switch',name='MAX Bot',exact=True)).not_to_be_checked()
    expect(page.get_by_role('switch',name='Мессенджеры (сообщение от администратора)',exact=True)).to_be_checked()
    expect(page.locator('[name=sms],[name=chatpush],[name=enabled]')).to_have_count(0)
    expect(page.locator('input[name=email]')).to_have_count(1)  # Only the channel switch.
    expect(page.locator('input[type=email]')).to_have_count(0)
    expect(page.get_by_role('button',name='Подтвердить email',exact=True)).to_have_count(0)
    expect(page.get_by_role('button',name='Подключить Telegram',exact=True)).to_have_count(0)
    expect(page.locator('.header-signin,a.brand')).to_have_count(0)
    assert page.locator('#unsubscribeClientNotifications').evaluate('(button)=>Boolean(button.compareDocumentPosition(document.querySelector(".preference-channels")) & Node.DOCUMENT_POSITION_FOLLOWING)')
    page.get_by_role('button',name='Не получать уведомления о пропуске',exact=True).click()
    dialog=page.locator('#unsubscribeNotificationsDialog');expect(dialog).to_be_visible()
    expect(dialog).to_contain_text('останется действительной');expect(dialog).to_contain_text('закладки');expect(dialog).to_contain_text('заблаговременно')
    dialog.get_by_role('button',name='Оставить уведомления',exact=True).click()
    assert web.links.load_links()[web.token_a]['mailing_enabled']
    page.get_by_role('button',name='Не получать уведомления о пропуске',exact=True).click()
    with page.expect_response('**/preferences/'+web.token_a) as saved:
        dialog.get_by_role('button',name='Не получать уведомления',exact=True).click()
    assert saved.value.status==200
    expect(page.get_by_role('switch',name='Telegram Bot',exact=True)).not_to_be_checked()
    expect(page.get_by_role('switch',name='Telegram Bot',exact=True)).to_be_enabled()
    prefs=identity.prefs_for_token(web.token_a);assert not prefs['enabled'] and prefs['channels']['telegram']
    page.reload();expect(page.locator('#resumeClientNotifications')).to_have_count(0)
    expect(page.locator('#unsubscribeClientNotifications')).to_be_hidden()
    page.get_by_role('switch',name='Telegram Bot',exact=True).click()
    expect(page.get_by_role('switch',name='Telegram Bot',exact=True)).to_be_checked()
    expect(page.locator('#unsubscribeClientNotifications')).to_be_visible()
    expect(page.get_by_role('switch',name='Мессенджеры (сообщение от администратора)',exact=True)).not_to_be_checked()
    assert not identity.prefs_for_token(web.token_a)['channels']['messengers']
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(tmp_path/'client-notifications-mobile.png'),full_page=True)
    assert web.calls==[] and client_preferences.sent==[]


def test_standalone_has_no_messengers_and_no_unsubscribe_until_channel_active(web,browser_ui,client_preferences):
    page,origin,expect,errors=browser_ui;identity=client_preferences.identity
    with web.branches.store.transaction() as branches:branches['12']['yclients_enabled']=False
    web.links.bind_identity('telegram','81001',token=web.token_a)
    identity.save_prefs_for_token(web.token_a,{'enabled':True,'channels':{}})
    page.goto(origin+'/preferences/'+web.token_a)
    expect(page.get_by_role('switch')).to_have_count(3)
    expect(page.get_by_role('switch',name='Мессенджеры (сообщение от администратора)',exact=True)).to_have_count(0)
    expect(page.locator('#unsubscribeClientNotifications')).to_be_hidden()
    expect(page.locator('#resumeClientNotifications')).to_have_count(0)
    toggle=page.get_by_role('switch',name='Telegram Bot',exact=True)
    toggle.check();expect(page.locator('#unsubscribeClientNotifications')).to_be_visible()
    toggle.uncheck();expect(page.locator('#unsubscribeClientNotifications')).to_be_hidden()
    assert web.calls==[] and client_preferences.sent==[] and not errors


def test_sms_optin_is_visible_only_for_enabled_branch_and_manual_guest_form(web,browser_ui,client_preferences):
    from test_ui_browser import sign_in
    page,origin,expect,errors=browser_ui;identity=client_preferences.identity
    web.branches.update_settings(12,{'yclients_enabled':False})
    page.goto(origin+'/preferences/'+web.token_a)
    expect(page.locator('input[name=sms]')).to_have_count(0)
    web.branches.update_settings(12,{'sms_notifications_enabled':True})
    page.reload();toggle=page.get_by_role('switch',name='СМС',exact=True)
    expect(toggle).not_to_be_checked()
    with page.expect_response('**/preferences/'+web.token_a):toggle.check()
    expect(toggle).to_be_checked()
    assert identity.prefs_for_token(web.token_a)['effective_channels']['sms']
    sign_in(page,origin,'tenant-a')
    page.goto(origin+'/admin/temp-link')
    expect(page.locator('#sendLinkCheckbox')).to_be_enabled()
    expect(page.locator('#tempDeliveryChannel')).to_be_disabled()
    page.locator('#sendLinkCheckbox').check()
    expect(page.locator('#tempDeliveryChannel')).to_have_value('sms')
    expect(page.locator('#tempDeliveryChannel')).to_be_enabled()
    page.goto(origin+'/admin/settings')
    expect(page.locator('#settingsSmsNotifications')).to_be_checked()
    assert web.calls==[] and client_preferences.sent==[] and not errors


def test_unsubscribe_recovers_expired_form_session_without_losing_access(web,browser_ui,client_preferences):
    page,origin,expect,errors=browser_ui;identity=client_preferences.identity
    web.links.bind_identity('telegram','81001',token=web.token_a)
    identity.save_prefs_for_token(web.token_a,{'enabled':True,'channels':{'telegram':True}})
    before=web.links.load_links()[web.token_a]
    path='/preferences/'+web.token_a
    page.goto(origin+path)
    page.get_by_role('button',name='Не получать уведомления о пропуске',exact=True).click()
    page.context.clear_cookies()
    outcomes=[]
    page.on('response',lambda response:outcomes.append(response.status) if response.url==origin+path and response.request.method=='POST' else None)
    with page.expect_response(lambda response:response.url==origin+path and response.request.method=='POST' and response.status==200):
        page.locator('#unsubscribeNotificationsDialog').get_by_role('button',name='Не получать уведомления',exact=True).click()
    expect(page.locator('#unsubscribeNotificationsDialog')).not_to_be_visible()
    expect(page.locator('#identityNotice')).to_contain_text('Вы отписались')
    assert outcomes==[403,200]
    after=web.links.load_links()[web.token_a]
    assert not after['mailing_enabled']
    for key in ('gate_access','access_expires','phone','branch_id'):assert after.get(key)==before.get(key)
    assert web.calls==[] and client_preferences.sent==[] and not errors


def test_email_is_connected_from_switch_and_verified_state_refreshes(web,browser_ui,client_preferences):
    page,origin,expect,errors=browser_ui;identity=client_preferences.identity
    web.links.set_mailing_enabled(web.token_a,False)
    page.goto(origin+'/preferences/'+web.token_a)
    email_switch=page.get_by_role('switch',name='Email',exact=True)
    email_switch.click()
    expect(email_switch).not_to_be_checked()
    expect(page.locator('input[type=email]')).to_be_visible()
    page.get_by_label('Ваш email',exact=True).fill('client@example.invalid')
    with page.expect_response('**/verify-email') as sent:
        page.get_by_role('button',name='Получить ссылку подтверждения',exact=True).click()
    assert sent.value.status==200
    assert client_preferences.sent[-1][0:2]==('email','client@example.invalid')
    expect(email_switch).not_to_be_checked()
    assert identity.prefs_for_token(web.token_a)['channels']['email']
    token=client_preferences.sent[-1][2].rsplit('/',1)[1];identity.verify_email(token)
    page.evaluate('window.dispatchEvent(new Event("focus"))')
    expect(email_switch).to_be_checked()
    expect(page.locator('#clientChannelConnection')).to_be_hidden()
    expect(page.locator('#clientChannel-email-status')).to_contain_text('client@example.invalid')
    email_switch.uncheck();expect(email_switch).not_to_be_checked()
    page.reload();expect(email_switch).not_to_be_checked()
    assert not identity.prefs_for_token(web.token_a)['channels']['email']
    assert web.calls==[]


def test_bot_connection_flow_uses_link_and_readiness_without_external_navigation(web,browser_ui,client_preferences):
    page,origin,expect,errors=browser_ui;identity=client_preferences.identity
    web.links.set_mailing_enabled(web.token_a,False)
    def connect(route):
        assert route.request.method=='POST' and route.request.post_data_json=={'platform':'max'}
        web.links.bind_identity('max','81002',token=web.token_a)
        route.fulfill(json={'link':origin+'/preferences/'+web.token_a})
    page.route('**/preferences/'+web.token_a+'/bot-link',connect)
    page.goto(origin+'/preferences/'+web.token_a)
    toggle=page.get_by_role('switch',name='MAX Bot',exact=True)
    toggle.click();expect(toggle).not_to_be_checked()
    expect(page.get_by_role('button',name='Открыть MAX Bot',exact=True)).to_be_visible()
    page.get_by_role('button',name='Открыть MAX Bot',exact=True).click()
    expect(toggle).to_be_checked()
    assert identity.prefs_for_token(web.token_a)['effective_channels']['max']
    assert not identity.bot_admin('max','81002')
    assert web.calls==[] and client_preferences.sent==[]


def test_unavailable_messengers_does_not_pretend_selected_and_guests_get_no_settings(web,browser_ui,client_preferences,monkeypatch):
    page,origin,expect,errors=browser_ui
    monkeypatch.setattr(client_preferences.identity,'_client_messengers_state',lambda *args:{'connected':False,'provider':'yclients','reason':'yclients_client_missing'})
    page.goto(origin+'/preferences/'+web.token_a)
    toggle=page.get_by_role('switch',name='Мессенджеры (сообщение от администратора)',exact=True)
    toggle.click();expect(toggle).not_to_be_checked()
    expect(page.locator('#clientChannelConnection')).to_contain_text('Обратитесь к администратору')
    assert not client_preferences.identity.prefs_for_token(web.token_a)['channels']['messengers']
    with web.links.store.transaction() as links:links[web.token_a].update(is_temporary=True,gate_access=True)
    response=page.goto(origin+'/preferences/'+web.token_a);assert response.status==403
    page.goto(origin+'/opn/'+web.token_a);expect(page.locator('#preferencesLink')).to_be_hidden()
    assert web.calls==[] and client_preferences.sent==[]


def test_late_status_cannot_restore_old_choice_and_failed_save_rolls_back_toggle(web,browser_ui,client_preferences):
    page,origin,expect,errors=browser_ui;identity=client_preferences.identity
    web.links.bind_identity('telegram','81001',token=web.token_a)
    identity.save_prefs_for_token(web.token_a,{'enabled':True,'channels':{'telegram':True}})
    old=identity.prefs_for_token(web.token_a);pending=[]
    page.goto(origin+'/preferences/'+web.token_a)
    page.route('**/preferences/'+web.token_a+'/status',lambda route:pending.append(route))
    with page.expect_request('**/preferences/'+web.token_a+'/status'):
        page.evaluate('window.dispatchEvent(new Event("focus"))')
    toggle=page.get_by_role('switch',name='Telegram Bot',exact=True)
    with page.expect_response('**/preferences/'+web.token_a):toggle.click()
    expect(toggle).not_to_be_checked()
    pending.pop().fulfill(json={'preferences':old})
    page.wait_for_load_state('networkidle')
    expect(toggle).not_to_be_checked()
    assert not identity.prefs_for_token(web.token_a)['channels']['telegram']
    page.route('**/preferences/'+web.token_a,lambda route:route.fulfill(status=503,json={'message':'Тестовая недоступность сохранения'}))
    with page.expect_response('**/preferences/'+web.token_a):toggle.click()
    expect(page.locator('#identityNotice')).to_contain_text('Тестовая недоступность сохранения')
    expect(toggle).not_to_be_checked()
    assert not identity.prefs_for_token(web.token_a)['channels']['telegram']
    assert web.calls==[] and client_preferences.sent==[]
