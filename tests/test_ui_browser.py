"""Real-browser UI regressions with synthetic local state and mocked hardware."""
import os
from pathlib import Path
import threading

import pytest

from test_web_security import web, TEST_PASSWORD

pytestmark = pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')


@pytest.fixture
def browser_ui(web, tmp_path, monkeypatch):
    import sim800c
    monkeypatch.setattr(sim800c,'_queue',sim800c.CallQueue(enabled=False,dry_run=True))
    playwright=pytest.importorskip('playwright.sync_api')
    executable=next((p for p in (Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')) if p.exists()),None)
    if executable is None:pytest.skip('Chrome/Edge not installed')
    from werkzeug.serving import make_server
    server=make_server('127.0.0.1',0,web.app)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    origin='http://127.0.0.1:'+str(server.server_port)
    try:
        with playwright.sync_playwright() as p:
            browser=p.chromium.launch(executable_path=str(executable),headless=True,args=['--disable-background-networking','--disable-component-update','--no-first-run'])
            page=browser.new_page(viewport={'width':1360,'height':900})
            errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
            yield page,origin,playwright.expect,errors
            assert not errors
            browser.close()
    finally:server.shutdown();thread.join(3)


def sign_in(page,origin,username='super'):
    page.goto(origin+'/admin/login');page.locator('[name=username]').fill(username);page.locator('[name=password]').fill(TEST_PASSWORD)
    page.locator('button[type=submit]').click();page.wait_for_url('**/admin/users')


def test_client_table_actions_filters_sorting_and_shared_layout(web,browser_ui,tmp_path,monkeypatch):
    page,origin,expect,errors=browser_ui
    from storage.sql_documents import migrate_legacy,DocumentStore
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    monkeypatch.setattr('storage.config.CLIENT_OUTBOUND_ENABLED',True)
    web.links.bind_identity('telegram','81001',token=web.token_a)
    identity.save_prefs_for_token(web.token_a,{'enabled':True,'channels':{'telegram':True}})
    web.links.update_user(web.token_a,{'id_ycl':1234})
    web.links.toggle_user_access(web.token_a,False)
    sign_in(page,origin)
    expect(page.locator('[data-shared-header]')).to_have_count(1)
    expect(page.locator('#usersTableBody tr')).to_have_count(2)
    page.locator('#searchInput').fill('Client A');expect(page.locator('#usersTableBody tr')).to_have_count(1)
    page.locator('#filterBranch').select_option('123');expect(page.locator('#usersTableBody tr')).to_have_count(0)
    page.locator('#filterBranch').select_option('12');expect(page.locator('#usersTableBody tr')).to_have_count(1)
    page.locator('#usersTableBody .btn-pass').click()
    page.locator('[name=accessType][value=temporary]').check();page.locator('#daysInput').fill('3')
    page.locator('#accessForm button[type=submit]').click()
    expect(page.locator('#usersTableBody .btn-pass')).to_have_text('3 дня')
    page.locator('#filterStatus').select_option('Временный');expect(page.locator('#usersTableBody tr')).to_have_count(1)
    page.locator('#usersTableBody [title=Редактировать]').click()
    page.get_by_label('Имя',exact=True).fill('Анна Соколова')
    expect(page.get_by_label('Telegram — подтвержденная привязка')).to_have_attribute('readonly','')
    page.get_by_role('button',name='Сохранить',exact=True).click()
    expect(page.locator('#usersTableBody tr')).to_have_count(0)  # Search retained after mutation.
    page.locator('#searchInput').fill('Анна');expect(page.locator('#usersTableBody tr')).to_have_count(1)
    status=page.locator('#usersTableBody .client-notifications-status');expect(status).to_have_attribute('data-active-channels','telegram');status.click()
    page.get_by_role('switch',name='Telegram Bot',exact=True).uncheck()
    expect(status).to_have_attribute('data-active-channels','')
    page.get_by_role('dialog',name='Уведомления клиента',exact=True).get_by_role('button',name='Закрыть',exact=True).click()
    page.locator('#usersTableBody [title="Копировать ссылку"]').click()
    expect(page.locator('.modal input[readonly]')).to_have_value('http://localhost:5000/opn/'+web.token_a)
    expect(page.get_by_role('button',name='Отправить ссылку',exact=True)).to_be_visible()
    page.get_by_role('button',name='Закрыть',exact=True).click()
    page.locator('#searchInput').fill('');page.locator('#filterBranch').select_option('');page.locator('#filterStatus').select_option('')
    page.locator('th[onclick="sortUsersBy(\'user_name\')"]').focus();page.keyboard.press('Enter')
    expect(page.locator('th[onclick="sortUsersBy(\'user_name\')"]')).to_have_attribute('aria-sort','ascending')
    with page.expect_download() as download:page.locator('#exportCsvBtn').click()
    assert download.value.suggested_filename.endswith('.csv')
    page.screenshot(path=str(tmp_path/'clients-desktop.png'),full_page=True)
    for route in ('/admin/add-user','/admin/temp-link','/admin/settings','/admin/logs','/admin/change-password','/admin/telephony'):
        page.goto(origin+route);expect(page.locator('[data-shared-header]')).to_have_count(1)
        assert page.locator('link[href="/static/css/ui.css"]').count()==1
    page.goto(origin+'/admin/add-user');page.locator('#userName').fill('Тестовый клиент');page.locator('#phone').fill('+1234567');page.locator('#branchId').select_option('12')
    page.locator('#createLinkForm button[type=submit]').click();expect(page.locator('#linkResult')).to_be_visible()
    page.goto(origin+'/admin/users');page.locator('#searchInput').fill('Тестовый клиент');expect(page.locator('#usersTableBody tr')).to_have_count(1)
    page.locator('#usersTableBody [title=Удалить]').click();page.locator('.modal').get_by_role('button',name='Удалить',exact=True).click();expect(page.locator('#usersTableBody tr')).to_have_count(0)
    page.locator('#searchInput').fill('');page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path/'clients-mobile.png'),full_page=True)
    assert web.calls==[]


def test_branch_admin_table_remains_scoped_and_controls_survive_mobile(web,browser_ui):
    page,origin,expect,errors=browser_ui
    web.links.toggle_user_access(web.token_a,False)
    sign_in(page,origin,'tenant-a');expect(page.locator('#usersTableBody tr')).to_have_count(1)
    expect(page.locator('.card-sort-controls')).to_be_hidden()
    expect(page.locator('#exportCsvBtn')).to_be_visible()
    expect(page.locator('#filterBranch')).to_be_hidden()
    expect(page.locator('.app-navigation a[href="/admin/telephony"]')).to_have_count(0)
    expect(page.locator('#usersTableBody [title="Назначить администратором"]')).to_be_visible()
    page.set_viewport_size({'width':390,'height':844})
    assert page.locator('#usersTableBody tr').first.evaluate("row => getComputedStyle(row).display")=='grid'
    page.locator('#mobileClientSort').select_option('user_name')
    page.locator('#usersTableBody .btn-pass').click();page.locator('#accessForm button[type=submit]').click()
    expect(page.locator('#usersTableBody .btn-pass')).to_have_text('Постоянный')
    page.locator('#usersTableBody .btn-pass').click();page.get_by_role('button',name='Запретить',exact=True).click()
    expect(page.locator('#usersTableBody .btn-pass')).to_have_text('Запрещен')


def test_guest_expiry_and_mobile_cabinet_navigation(web,browser_ui,tmp_path,monkeypatch):
    from storage.sql_documents import DocumentStore,migrate_legacy
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    web.module.product_services().identity.attach_console_account('tenant-a','+15550100901')
    web.links.create_link({'phone':'+15550100902','branch_id':12,'user_name':'Short visit','is_temporary':True,'gate_access':True},expires_days=1)
    page,origin,expect,errors=browser_ui
    sign_in(page,origin,'tenant-a')
    row=page.locator('#usersTableBody tr').filter(has_text='Short visit')
    expect(row.locator('[data-label="Истекает"] time')).to_be_visible()
    expect(row.get_by_title('Назначить администратором')).to_have_count(0)
    expect(row.get_by_title('Копировать ссылку').locator('svg path')).to_have_count(1)
    page.set_viewport_size({'width':390,'height':844})
    page.set_extra_http_headers({'Sec-CH-UA-Mobile':'?1'})
    page.add_init_script("Object.defineProperty(navigator,'userAgent',{get:()=> 'Android Mobile'});")
    page.goto(origin+'/admin/users')
    expect(page).to_have_url(origin+'/office/clients')
    expect(page.locator('.office-nav > a')).to_have_count(3)
    expect(page.locator('[data-install-hint]')).to_be_visible()
    page.locator('[data-install-hint] [data-install-office]').click()
    expect(page.locator('[data-office-install-dialog]')).to_be_visible()
    page.get_by_role('button',name='Готово').click()
    page.locator('.office-menu summary').click()
    page.get_by_role('link',name='Подписка',exact=True).click()
    expect(page).to_have_url(origin+'/office/billing')
    page.locator('.office-menu summary').click()
    page.get_by_role('link',name='Полная версия').click()
    expect(page.locator('[data-shared-header]')).to_be_visible()
    assert not errors


def test_client_access_cooldown_expiry_and_no_second_command(web,browser_ui,tmp_path):
    page,origin,expect,errors=browser_ui
    web.links.toggle_user_access(web.token_a,True)
    state={'active':True,'retry_after':0,'expires_at':None}
    offline={'enabled':False}
    page.route('**/api/access/status',lambda route:route.abort() if offline['enabled'] else route.fulfill(json=state))
    page.goto(origin+'/opn/'+web.token_a)
    expect(page.locator('.gate-action')).to_be_visible()
    with page.expect_response('**/open-gate') as opened:page.locator('.gate-action').click()
    assert opened.value.status==200
    expect(page.locator('.gate-action')).to_be_disabled()
    expect(page.locator('#gateCooldown')).to_contain_text('20')
    page.locator('.gate-action').dispatch_event('click')
    assert len(web.calls)==1
    page.screenshot(path=str(tmp_path/'client-pass.png'),full_page=True)
    state['active']=False
    page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
    expect(page.locator('#gateActions')).to_be_hidden()
    expect(page.locator('#accessHeading')).to_have_text('Пропуск сейчас неактивен')
    offline['enabled']=True
    page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
    expect(page.locator('#statusMessage')).to_contain_text('Проверяем соединение')
    offline['enabled']=False;state['active']=True
    page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
    expect(page.locator('#gateActions')).to_be_visible()
    expect(page.locator('#statusMessage')).to_be_empty()
    assert len(web.calls)==1


def test_new_product_and_identity_pages_share_layout_and_remain_usable(web,browser_ui,tmp_path,monkeypatch):
    page,origin,expect,errors=browser_ui
    from storage.sql_documents import DocumentStore,migrate_legacy
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    sign_in(page,origin)
    for name,route in [('subscription','/admin/subscription'),('statistics','/admin/statistics'),('invitations','/admin/invitations'),('notifications','/admin/notification-settings'),('integrations','/admin/integrations'),('registration','/register-branch')]:
        response=page.goto(origin+route)
        assert response.status==200
        expect(page.locator('[data-shared-header]')).to_have_count(1)
        if name=='subscription':
            expect(page.locator('#planCards')).to_have_count(0)
            expect(page.locator('#purchaseDialog')).to_have_count(0)
            page.locator('#billingBranch').select_option('12')
            page.locator('#openGift').click()
            expect(page.locator('#giftForm')).to_be_visible()
            page.locator('#giftDialog [data-dismiss]').first.click()
        if name=='statistics':
            expect(page.locator('#statisticsSummary .stat-card')).to_have_count(6)
            grouping=page.locator('.product-filter-field').filter(has=page.locator('select[name=group_by]'))
            grouping.locator('summary').click();grouping.get_by_label('По филиалу',exact=True).check()
            grouping.locator('summary').click()
            with page.expect_response('**/admin/api/statistics/query') as report:page.get_by_role('button',name='Построить отчёт').click()
            assert report.value.status==200
            assert report.value.request.post_data_json['group_by']==['day','branch']
            with page.expect_download() as downloaded:page.locator('#statisticsExport').click()
            assert downloaded.value.suggested_filename.endswith('.csv')
        if name=='registration':
            expect(page.get_by_label('Ваше имя',exact=True)).to_be_visible()
            expect(page.get_by_role('button',name='Получить SMS-код')).to_be_visible()
        page.screenshot(path=str(tmp_path/(name+'-desktop.png')),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),name
        if name=='notifications':
            assert page.locator('#identityContent .users-table').evaluate('table=>table.scrollWidth <= table.parentElement.clientWidth + 1')
        if name=='statistics':
            for picker in page.locator('.multi-select').all():
                picker.locator('summary').click()
                expect(picker.locator('input[type=search]')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                picker.locator('summary').click()
        page.screenshot(path=str(tmp_path/(name+'-mobile.png')),full_page=True)
        page.set_viewport_size({'width':1360,'height':900})
    assert web.calls==[]


def test_admin_has_no_branch_switch_and_keeps_primary_scope(web,browser_ui,tmp_path,monkeypatch):
    page,origin,expect,errors=browser_ui
    from storage.sql_documents import DocumentStore,migrate_legacy
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    person=identity.attach_console_account('tenant-a',web.links.load_links()[web.token_a]['phone'],client_token=web.token_a)
    with identity.product.connect() as db:
        db.execute('INSERT OR REPLACE INTO branch_memberships(person_id,branch_id,status,created) VALUES(?,?,"active",?)',(person,'123',identity.clock()))
    sign_in(page,origin,'tenant-a')
    expect(page.locator('.brand-scope')).to_have_text('Branch 12')
    page.locator('.account-menu summary').click()
    expect(page.locator('#activeBranchSelect')).to_have_count(0)
    response=page.request.get(origin+'/admin/api/users')
    assert {str(user['branch_id']) for user in response.json()['users']}=={'12'}
    expect(page.locator('[data-shared-header]')).to_have_count(1)


def test_notification_inbox_filters_safe_text_and_read_action(web,browser_ui,tmp_path):
    page,origin,expect,errors=browser_ui
    items=[{'id':'test-message','event':'sms_received','created':1794864000,'read':False,'text':'Входящее SMS\n<img src=x onerror=alert(1)>\n'+'Длинный текст сообщения. '*25},
           {'id':'test-gate','event':'gate_requested','created':1794863900,'read':True,'text':'Запрос открытия ворот · Тестовый филиал\nТестовый клиент'}]
    marked=[]
    page.route('**/admin/api/notification-inbox',lambda route:route.fulfill(json={'items':items}))
    def read(route):
        assert route.request.headers.get('x-csrf-token')
        marked.append(route.request.method);items[0]['read']=True;route.fulfill(json={'status':'success'})
    page.route('**/admin/api/notification-inbox/test-message/read',read)
    sign_in(page,origin)
    page.goto(origin+'/admin/notifications')
    expect(page.locator('#notificationList article')).to_have_count(2)
    expect(page.locator('#notificationList img')).to_have_count(0)
    expect(page.locator('#notificationCount')).to_have_text('1')
    page.screenshot(path=str(tmp_path/'notification-inbox-desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path/'notification-inbox-mobile.png'),full_page=True)
    page.locator('#showUnreadNotifications').click();expect(page.locator('#notificationList article')).to_have_count(1)
    page.get_by_role('button',name='Отметить прочитанным: Входящее SMS').click()
    expect(page.locator('#notificationList')).to_contain_text('Всё прочитано')
    page.locator('#showAllNotifications').click();expect(page.locator('#notificationList article')).to_have_count(2)
    assert marked==['POST'] and web.calls==[]


def test_branch_logo_upload_preview_public_pass_and_removal(web,browser_ui):
    from test_branch_logos import image_bytes
    page,origin,expect,errors=browser_ui
    web.links.toggle_user_access(web.token_a,True)
    sign_in(page,origin,'tenant-a');page.goto(origin+'/admin/settings')
    expect(page.locator('#branchSettingsForm')).to_have_attribute('data-ready','true')
    expect(page.locator('#settingsLogo')).to_have_attribute('type','hidden')
    with page.expect_response('**/admin/api/branches/12/logo') as result:
        page.locator('#settingsLogoUpload').set_input_files({'name':'logo.png','mimeType':'image/png','buffer':image_bytes()})
    assert result.value.status==200
    expect(page.locator('#settingsLogoPreview')).to_be_visible()
    expect(page.locator('#settingsLogoStatus')).to_have_text('Логотип сохранён')
    logo=web.branches.get_branch(12).logo
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.goto(origin+'/opn/'+web.token_a)
    expect(page.locator('img[src="'+logo+'"]')).to_be_visible()
    page.goto(origin+'/admin/settings');expect(page.locator('#settingsLogoRemove')).to_be_visible()
    page.locator('#settingsLogoRemove').click();expect(page.locator('#settingsLogoPreview')).to_be_hidden()
    assert web.branches.get_branch(12).logo=='' and web.calls==[]
