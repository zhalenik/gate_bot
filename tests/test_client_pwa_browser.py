"""PWA scope, offline safety and explicit Push consent; browser Push is fake."""
import base64
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from storage.sql_documents import DocumentStore,migrate_legacy
from test_ui_browser import browser_ui,sign_in
from test_web_security import web


pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


@pytest.fixture
def pwa_client(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    services=web.module.product_services()
    services.pwa.transport=lambda *args:pytest.fail('No Push delivery is permitted in browser tests')
    return services


def synthetic_subscription():
    public=ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
    encode=lambda value:base64.urlsafe_b64encode(value).decode().rstrip('=')
    return {'endpoint':'https://fcm.googleapis.com/fcm/send/synthetic-browser-device',
                  'keys':{'p256dh':encode(public),'auth':encode(b'0123456789abcdef')},'expirationTime':None}


def mock_push(page,permission='granted'):
    subscription=synthetic_subscription()
    script="""(() => {
        const nativeMatch=window.matchMedia.bind(window);
        window.matchMedia=query=>query==='(display-mode: standalone)'?{matches:true,addEventListener(){},removeEventListener(){}}:nativeMatch(query);
        let state=sessionStorage.getItem('fake-push-permission')||'default';
        window.fakePermissionCalls=0;window.fakeSubscribeCalls=0;
        Object.defineProperty(Notification,'permission',{configurable:true,get:()=>state});
        Notification.requestPermission=()=>{window.fakePermissionCalls++;state=PERMISSION;sessionStorage.setItem('fake-push-permission',state);return Promise.resolve(state);};
        const subscription=SUBSCRIPTION;
        function value(){return {endpoint:subscription.endpoint,toJSON:()=>subscription,unsubscribe:async()=>true};}
        PushManager.prototype.getSubscription=async()=>sessionStorage.getItem('fake-push-active')?value():null;
        PushManager.prototype.subscribe=async()=>{window.fakeSubscribeCalls++;sessionStorage.setItem('fake-push-active','1');return value();};
    })();""".replace('PERMISSION',json.dumps(permission)).replace('SUBSCRIPTION',json.dumps(subscription))
    page.add_init_script(script)
    return subscription


def test_personal_worker_never_controls_admin_and_offline_never_replays_gates(web,browser_ui,pwa_client):
    page,origin,expect,errors=browser_ui;scope='/pwa/'+web.token_a+'/'
    page.goto(origin+scope+'gate')
    page.wait_for_function("scope=>navigator.serviceWorker.getRegistration(scope).then(reg=>reg?.active?.state==='activated'&&Boolean(navigator.serviceWorker.controller))",arg=scope)
    page.reload();page.wait_for_function('Boolean(navigator.serviceWorker.controller)')
    assert page.evaluate('async()=> (await navigator.serviceWorker.getRegistrations()).map(r=>new URL(r.scope).pathname)')==[scope]
    assert page.evaluate('async()=>await caches.keys()')==[]
    page.goto(origin+'/admin/login')
    assert page.evaluate('navigator.serviceWorker.controller===null')
    assert page.locator('link[rel=manifest]').count()==0
    page.goto(origin+scope+'gate');page.wait_for_function('Boolean(navigator.serviceWorker.controller)')
    page.context.set_offline(True)
    expect(page.locator('#clientOfflineNotice')).to_be_visible()
    page.evaluate("document.querySelector('[data-button-id]')?.click()")
    assert web.calls==[]
    page.reload(wait_until='domcontentloaded')
    assert page.locator('body').inner_text()=='Отсутствует подключение к интернету'
    assert page.locator('button,a').count()==0
    page.context.set_offline(False)
    expect(page.locator('#gateActions')).to_be_visible()
    assert page.evaluate('async()=>await caches.keys()')==[] and web.calls==[] and not errors


def test_installed_push_needs_click_and_preferences_can_disable_it(web,browser_ui,pwa_client,tmp_path):
    page,origin,expect,errors=browser_ui;mock_push(page)
    scope='/pwa/'+web.token_a+'/'
    page.goto(origin+scope+'gate')
    prompt=page.get_by_role('dialog',name='Уведомлять о пропуске?')
    expect(prompt).to_be_visible()
    assert page.evaluate('fakePermissionCalls')==0
    assert pwa_client.pwa.status(web.token_a)['subscription_count']==0
    page.context.clear_cookies();outcomes=[]
    page.on('response',lambda response:outcomes.append(response.status) if response.url.endswith('/push/subscribe') and response.request.method=='POST' else None)
    with page.expect_response(lambda response:response.url.endswith('/push/subscribe') and response.status==200) as saved:prompt.get_by_role('button',name='Включить уведомления').click()
    assert saved.value.status==200 and page.evaluate('fakePermissionCalls')==1
    assert outcomes==[403,200]
    expect(prompt).to_have_count(0)
    assert pwa_client.identity.prefs_for_token(web.token_a)['effective_channels']['push']
    page.set_viewport_size({'width':390,'height':844})
    expect(page.locator('.client-pwa-tabs a')).to_have_count(2)
    expect(page.locator('#preferencesLink')).to_be_hidden()
    page.locator('.client-pwa-tabs').get_by_role('link',name='Уведомления').click()
    page.wait_for_url('**'+scope+'preferences')
    toggle=page.get_by_role('switch',name='Push',exact=True);expect(toggle).to_be_checked()
    bottom = page.locator('.client-pwa-tabs').bounding_box()['y']
    page.locator('main').evaluate('(main) => {main.scrollTop = main.scrollHeight}')
    assert abs(page.locator('.client-pwa-tabs').bounding_box()['y']-bottom) < 1
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(tmp_path/'pwa-preferences-mobile.png'),full_page=True)
    with page.expect_response('**/preferences/'+web.token_a):toggle.uncheck()
    assert not pwa_client.identity.prefs_for_token(web.token_a)['effective_channels']['push']
    page.reload();expect(toggle).not_to_be_checked()
    assert page.get_by_role('dialog',name='Уведомлять о пропуске?').count()==0
    with page.expect_response('**/push/subscribe'):toggle.check()
    expect(toggle).to_be_checked()
    assert page.evaluate('fakePermissionCalls')==0 and pwa_client.pwa.status(web.token_a)['subscription_count']==1
    web.links.toggle_user_access(web.token_a,False)
    page.goto(origin+scope+'gate')
    expect(page.get_by_role('heading',name='Доступ невозможен')).to_be_visible()
    expect(page.locator('.client-pwa-tabs a')).to_have_count(2)
    assert page.locator('link[rel=manifest]').get_attribute('href')==scope+'manifest.webmanifest'
    assert web.calls==[] and not errors


def test_denied_push_is_not_requested_on_every_launch(web,browser_ui,pwa_client):
    page,origin,expect,errors=browser_ui;mock_push(page,'denied')
    scope='/pwa/'+web.token_a+'/'
    page.goto(origin+scope+'preferences')
    prompt=page.get_by_role('dialog',name='Уведомлять о пропуске?');expect(prompt).to_be_visible()
    prompt.get_by_role('button',name='Включить уведомления').click()
    expect(prompt.locator('[role=status]')).to_contain_text('не включены')
    assert page.evaluate('fakePermissionCalls')==1 and page.evaluate('fakeSubscribeCalls')==0
    prompt.get_by_role('button',name='Не сейчас').click()
    page.reload();expect(page.get_by_role('switch',name='Push',exact=True)).not_to_be_checked()
    assert page.get_by_role('dialog',name='Уведомлять о пропуске?').count()==0
    page.get_by_role('switch',name='Push',exact=True).click()
    expect(page.locator('#identityNotice')).to_contain_text('настройках устройства')
    assert page.evaluate('fakePermissionCalls')==0 and pwa_client.pwa.status(web.token_a)['subscription_count']==0
    assert not errors


def test_customer_can_send_feedback_and_owner_reads_it(web,browser_ui,pwa_client):
    page,origin,expect,errors=browser_ui
    page.goto(origin+'/opn/'+web.token_a)
    page.get_by_role('button',name='Пожелания и отзывы').click()
    review=page.get_by_role('dialog',name='Вам нравится приложение?')
    review.get_by_role('radio',name='4 из 5').check()
    review.locator('textarea').fill('Тестовый отзыв')
    review.get_by_role('button',name='Отправить').click()
    expect(review.get_by_role('status')).to_have_text('Спасибо за отзыв!')
    sign_in(page,origin)
    page.goto(origin+'/admin/feedback')
    expect(page.get_by_text('Тестовый отзыв')).to_be_visible()


def test_uninstalled_has_install_guide_but_no_push_toggle_and_admin_cannot_subscribe_client(web,browser_ui,pwa_client):
    page,origin,expect,errors=browser_ui
    page.goto(origin+'/preferences/'+web.token_a)
    expect(page.get_by_role('switch',name='Push',exact=True)).to_have_count(0)
    expect(page.get_by_role('button',name='Установить GateBot')).to_be_visible()
    page.evaluate("""()=>{window.fakeInstallPrompts=0;const event=new Event('beforeinstallprompt',{cancelable:true});event.prompt=async()=>{window.fakeInstallPrompts++;};event.userChoice=Promise.resolve({outcome:'dismissed'});dispatchEvent(event);} """)
    page.get_by_role('button',name='Установить GateBot').click()
    assert page.evaluate('fakeInstallPrompts')==1
    page.evaluate("Object.defineProperty(navigator,'userAgent',{value:'iPhone',configurable:true})")
    page.get_by_role('button',name='Установить GateBot').click()
    guide=page.get_by_role('dialog',name='GateBot на главном экране');expect(guide).to_contain_text('Safari');expect(guide).to_contain_text('На экран Домой')
    guide.get_by_role('button',name='Понятно').click()
    sign_in(page,origin)
    page.get_by_role('button',name='Уведомления: Client A',exact=True).click()
    modal=page.get_by_role('dialog',name='Уведомления клиента',exact=True)
    expect(modal.get_by_role('switch',name='Push',exact=True)).to_be_disabled()
    expect(modal).to_contain_text('Клиент подключает Push в установленном приложении')
    pwa_client.pwa.subscribe(web.token_a,synthetic_subscription(),consent=True)
    modal.get_by_role('button',name='Проверить подключения').click()
    expect(modal.get_by_role('switch',name='Push',exact=True)).to_be_checked()
    expect(page.get_by_role('button',name='Уведомления: Client A',exact=True)).to_have_attribute('data-active-channels','push')
    with page.expect_response('**/admin/api/client-notifications'):modal.get_by_role('switch',name='Push',exact=True).uncheck()
    assert not pwa_client.identity.prefs_for_token(web.token_a)['effective_channels']['push']
    assert page.evaluate('navigator.serviceWorker.controller===null') and not errors


def test_worker_push_targets_only_its_client_and_post_is_never_intercepted(browser_ui):
    page,origin,expect,errors=browser_ui
    script=Path('static/pwa/sw.js').read_text(encoding='utf-8')
    result=page.evaluate("""async source=>{
        const handlers={},shown=[],opened=[],navigation=[];
        const self={location:{origin:'https://example.invalid'},registration:{scope:'https://example.invalid/pwa/synthetic-client/',showNotification:async(title,options)=>shown.push({title,...options})},
            addEventListener:(type,handler)=>handlers[type]=handler,clients:{matchAll:async()=>[{url:'https://example.invalid/pwa/other-client/gate',navigate:async url=>navigation.push(url),focus:async()=>{}}],openWindow:async url=>opened.push(url)}};
        new Function('self','fetch','Response',source)(self,async()=>{throw new Error('offline')},Response);
        let pending;const waitUntil=promise=>pending=promise;
        handlers.push({data:{json:()=>({url:'/admin/login',body:'foreign message'})},waitUntil});await pending;
        handlers.notificationclick({notification:{close(){},data:{url:'https://other.invalid/'}},waitUntil});await pending;
        let postIntercepted=false;handlers.fetch({request:{url:'https://example.invalid/open-gate',method:'POST',mode:'cors'},respondWith:()=>postIntercepted=true});
        handlers.fetch({request:{url:'https://example.invalid/pwa/synthetic-client/gate',method:'GET',mode:'navigate'},respondWith:promise=>pending=promise});
        const offline=await pending;
        return {shown,opened,navigation,postIntercepted,offline:await offline.text(),status:offline.status};
    }""",script)
    assert result['opened']==['https://example.invalid/pwa/synthetic-client/gate'] and result['navigation']==[]
    assert result['shown'][0]['data']['url']=='/pwa/synthetic-client/gate' and 'foreign' not in result['shown'][0]['body']
    assert not result['postIntercepted'] and result['status']==503 and 'Отсутствует подключение к интернету' in result['offline']
    assert not errors
