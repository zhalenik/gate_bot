"""Offline acceptance of branch correspondence and delegated guest gate access."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os

import pytest

from bot_state import BotStateStore
from bot_workflows import BotWorkflows
from desk_bot import client_key
from link_manager import access_is_active
from storage.sql_documents import migrate_legacy
from test_product_integration import product
from test_web_security import web, login, csrf, post
from test_ui_browser import browser_ui, sign_in
from test_pwa_push import subscription
from timeutil import now, parse_datetime


def test_approval_is_scoped_idempotent_and_replies_follow_current_identity(product, monkeypatch):
    p=product
    monkeypatch.setattr('storage.config.CLIENT_OUTBOUND_ENABLED', True)
    desk=p.services.desk
    p.links.revoke_link(p.token)
    desk.client_send(p.token,'request','','request-test-one')
    desk.client_send(p.token,'request','','request-test-two')
    thread,=desk.threads('branchtest')
    assert thread['pending']
    with pytest.raises(PermissionError):
        desk.threads('branchtest','20')
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:desk.approve('branchtest',thread['id'],thread['pending']),range(2)))
    data=p.links.load_links()[p.token]
    assert access_is_active(data) and data['access_expires']
    history=desk.client_view(p.token)['messages']
    assert len(history)==2 and history[0]['approved']
    with p.services.store.connect() as db:
        replies=db.execute("SELECT * FROM message_outbox WHERE audience='desk_reply'").fetchall()
        assert len(replies)==1
        row=replies[0]
        payload=p.services.notifications._decrypt(row['payload'])
        assert p.services.notifications._allowed(row,payload)
        assert db.execute("SELECT count(*) FROM notification_inbox WHERE event='pass_requested'").fetchone()[0]==2
    p.links.update_user(p.token,{'phone':'+15550100997'})
    assert not p.services.notifications._allowed(row,payload)
    assert desk.client_view(p.token)['messages']==[]
    with pytest.raises(ValueError,match='Диалог не найден'):
        desk.admin_view('branchtest',thread['id'])


@pytest.mark.parametrize('change',['expiry','revision','phone','gate'])
def test_pre_upgrade_one_use_links_rechecked_before_hardware(product,change,monkeypatch):
    p=product
    p.services.attach(p.access)
    desk=p.services.desk
    gate=p.branches.get_branch(10).buttons[0]
    raw='legacy-test-share'
    owner=p.links.load_links()[p.token]
    with desk.store.connect() as db:
        db.execute('INSERT INTO desk_shares VALUES(?,?,?,?,?,?,?,?,NULL)',(desk._digest(raw),'10',p.token,
                   owner['phone'],owner.get('access_revision',0),gate.stable_id,gate.id,desk.identity.clock()+3600))
    assert desk.shared(raw)=={'branch':'North','branch_id':'10','gate':'Entry'}
    assert desk.shared(raw,p.access)['status']=='success'
    with pytest.raises(PermissionError):
        desk.shared(raw,p.access)
    with p.services.store.connect() as db:
        command=db.execute('SELECT id FROM hardware_commands').fetchone()[0]
    assert p.access.authorize_command(command)
    if change=='expiry':
        monkeypatch.setattr(desk.identity,'clock',lambda:10**12)
    elif change=='revision':
        p.links.revoke_link(p.token)
        p.links.toggle_user_access(p.token,True)
    elif change=='phone':
        p.links.update_user(p.token,{'phone':'+15550100996'})
    else:
        p.branches.update_settings(10,{'buttons':[{'type':'gate','label':'Replacement','phone':'+15550100995'}]})
    assert not p.access.authorize_command(command,start=True)
    assert len(p.calls)==1


def test_browser_client_branch_and_guest_boundaries(web):
    migrate_legacy(web.links.store.path.parent,activate=True)
    services=web.module.product_services()
    services.attach(web.module.access_service)
    client=web.app.test_client()
    token=web.token_b
    key=csrf(client,'/opn/'+token)
    path='/api/pass/'+token
    assert client.post(path+'/desk',json={'kind':'request'}).status_code==403
    assert post(client,key,path+'/desk',{'kind':'message','body':'Тестовое обращение','key':'test-message'}).status_code==200
    share=post(client,key,path+'/share',{'no_phone':True,'key':'guest-test'}).get_json()['path']
    stranger=web.app.test_client()
    page=stranger.get(share)
    assert page.status_code==200 and token.encode() not in page.data and b'/preferences/' not in page.data and b'data-client-desk' in page.data
    assert stranger.post(share).status_code==403
    assert stranger.get('/admin/api/desk/threads').status_code==401
    thread,=services.desk.threads('tenant-b')
    key=login(client,'tenant-a')
    thread_path='/admin/api/desk/threads/'+str(thread['id'])
    assert client.get(thread_path).status_code==403
    assert post(client,key,thread_path+'/reply',{'body':'hello','key':'test-reply'}).status_code==403
    assert client.get('/admin/api/desk/threads').get_json()=={'items':[]}
    guest=share.rsplit('/',1)[-1]
    web.links.set_expiry_at(guest,now()-timedelta(seconds=1))
    assert stranger.get(share).status_code==410
    assert web.links.cleanup_guest_users()==1
    for url in (share,'/pwa/'+guest+'/gate'):
        page=stranger.get(url)
        assert page.status_code==410 and 'Пропуск удален' in page.get_data(as_text=True)
        assert b'data-client-desk' not in page.data and b'<button' not in page.data and b'/admin/login' not in page.data


def test_new_pass_token_skips_existing_and_retired_guest_urls(product, monkeypatch):
    import hashlib
    from link_manager import LinkManager
    values = iter('A'*10 + 'B'*10 + 'C'*10)
    monkeypatch.setattr('link_manager.secrets.choice', lambda alphabet: next(values))
    with product.services.store.connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS removed_guest_links(digest TEXT PRIMARY KEY)')
        db.execute('INSERT INTO removed_guest_links VALUES(?)', (hashlib.sha256(b'BBBBBBBBBB').hexdigest(),))
        assert LinkManager.generate_token({'A'*10}, db) == 'C'*10


def test_standard_guest_pass_without_phone_is_scoped_repeatable_and_deleted(product):
    p=product
    p.branches.update_settings(10,{'pass_duration_hours':5,'buttons':[
        {'type':'gate','label':'First','phone':'+15550100001'},
        {'type':'gate','label':'Second','phone':'+15550100002'}]})
    p.services.attach(p.access)
    desk=p.services.desk
    inputs={'no_phone':True,'key':'guest-first'}
    result=desk.share(p.token,**inputs)
    assert desk.share(p.token,**inputs)==result and result['delivery']=='not_requested'
    other=desk.share(p.token,no_phone=True,key='guest-second')
    token=result['path'].split('/')[-1]
    cards=p.links.load_links()
    card=cards[token]
    assert card['is_temporary'] and card['system_phone'] and card['user_name']=='Гость от Client'
    assert card['phone']!=cards[other['path'].split('/')[-1]]['phone']
    assert not p.links.get_users_by_phone(card['phone'])
    assert abs((parse_datetime(card['access_expires'])-now()).total_seconds()-5*3600)<5
    assert [b['label'] for b in desk.client_view(token)['gates']]==['First','Second']
    p.links.revoke_link(p.token)
    assert p.links.verify_token(token)
    with pytest.raises(PermissionError):
        desk.share(p.token,no_phone=True,key='guest-third')
    p.links.set_expiry_at(token,now()-timedelta(minutes=1))
    p.links.cleanup_guest_users()
    with p.services.store.connect() as db:
        assert not db.execute('SELECT 1 FROM persons WHERE id=?',(card['person_id'],)).fetchone()
        assert not db.execute('SELECT 1 FROM branch_clients WHERE legacy_token=?',(token,)).fetchone()


def test_shared_link_delivery_prefers_chatpush_then_enabled_sms(product,monkeypatch):
    p=product
    monkeypatch.setattr('storage.config.CLIENT_OUTBOUND_ENABLED',True)
    desk=p.services.desk
    assert not desk.client_view(p.token)['share_delivery']
    with pytest.raises(ValueError,match='недоступна'):
        desk.share(p.token,phone='+15550100105',send=True,key='guest-before')
    p.branches.update_settings(10,{'sms_notifications_enabled':True})
    calls=[]
    monkeypatch.setattr(desk.notices,'send',lambda *args:calls.append(args) or {'status':'accepted'})
    args={'phone':'+15550100105','send':True,'key':'guest-message'}
    first=desk.share(p.token,**args)
    assert desk.share(p.token,**args)==first and len(calls)==1
    assert calls[-1][0]=='sms' and first['path'] in calls[-1][2]
    monkeypatch.setattr(desk.identity,'integration_credentials',lambda *args:{'token':'example'})
    desk.share(p.token,phone=args['phone'],send=True,key='guest-next')
    assert calls[-1][0]=='chatpush' and calls[-1][1]==args['phone']


def test_auto_approval_generates_one_branch_event(product):
    p=product
    p.branches.update_settings(10,{'auto_approve_pass_requests':True,'pass_duration_hours':3})
    p.links.revoke_link(p.token)
    desk=p.services.desk
    desk.client_send(p.token,'request','','request-once')
    desk.client_send(p.token,'request','','request-once')
    card=p.links.verify_token(p.token)
    assert abs((parse_datetime(card['access_expires'])-now()).total_seconds()-3*3600)<5
    msg,=desk.client_view(p.token)['messages']
    assert msg['approved']
    with desk.store.connect() as db:
        events=db.execute("SELECT DISTINCT event,branch_id FROM notification_inbox WHERE event LIKE 'pass_request%'").fetchall()
        assert {(r['event'],r['branch_id']) for r in events}=={('pass_request_auto_granted','10')}


@pytest.mark.parametrize('platform',['telegram','max'])
def test_client_and_admin_bot_dialogs(product,platform):
    p=product
    p.services.attach(p.access)
    identity=p.services.identity
    if platform=='max':
        p.links.bind_identity('max','77',token=p.token)
        identity.bind_ticket('max','91',identity.issue_bind_ticket('branchtest','max'))
    flows=BotWorkflows(p.access,BotStateStore(p.root/'desk-bot.sqlite3'))
    key=client_key(p.token)
    paths={a.get('callback') for a in flows.actions(platform,'77')}
    assert {'x:contact:'+key,'x:share:'+key}.issubset(paths)
    share=flows.action(platform,'77','x:share:'+key)
    guest=flows.action(platform,'77',next(b['callback'] for b in share['actions'] if b['label']=='Нет номера'))
    assert p.links.verify_token(guest['text'].rsplit('/opn/',1)[1])['is_temporary']
    flows.action(platform,'77','x:share:'+key)
    guest=flows.message(platform,'77','+15550100101')
    assert p.links.verify_token(guest['text'].rsplit('/opn/',1)[1])['phone']=='+15550100101'
    with pytest.raises(PermissionError):
        flows.action(platform,'78','x:contact:'+key)
    flows.action(platform,'77','x:write:'+key)
    flows.message(platform,'77','Можно въехать?')
    p.links.revoke_link(p.token)
    flows.action(platform,'77','x:request:'+key)
    ticket,=p.services.desk.threads('branchtest')
    callback=f'x:approve:{ticket["id"]}:{ticket["pending"]}'
    with pytest.raises(PermissionError):
        flows.action(platform,'77',callback)
    flows.action(platform,'91',callback)
    assert access_is_active(p.links.load_links()[p.token])
    flows.action(platform,'91','x:respond:'+str(ticket['id']))
    flows.message(platform,'91','Въезжайте.')
    assert 'Въезжайте.' in flows.action(platform,'77','x:contact:'+key)['text']


def test_client_push_reply_obeys_opt_in_and_hides_contents(product,monkeypatch):
    p=product
    from storage import config
    monkeypatch.setattr(config,'BASE_URL','https://example.invalid')
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    p.services.pwa.subscribe(p.token,subscription('branchdesk'),consent=True)
    desk=p.services.desk
    p.links.revoke_link(p.token)
    desk.client_send(p.token,'message','Когда можно въехать?','client-message-one')
    thread,=desk.threads('branchtest')
    desk.reply('branchtest',thread['id'],'Приём открыт.','reply-one')
    notices=p.services.notifications
    sent=[]
    p.services.pwa.transport=lambda sub,payload,*args:sent.append(payload) or {'status':'accepted'}
    with notices.store.connect() as db:
        row=db.execute("SELECT * FROM message_outbox WHERE audience='desk_reply' AND channel='push'").fetchone()
        data=notices._decrypt(row['payload'])
        assert notices._allowed(row,data)
    assert p.services.pwa.deliver(data['target'],{'event':'desk_reply','client_token':p.token,'branch_id':'10'})['status']=='accepted'
    assert sent[0]['tag']=='gatebot-message' and 'Приём открыт.' not in sent[0]['body']
    p.services.pwa.unsubscribe(p.token,subscription('branchdesk')['endpoint'])
    assert not notices._allowed(row,data)


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in headless browser')
def test_guest_chat_and_approval_in_browser(web,browser_ui):
    page,origin,expect,_=browser_ui
    migrate_legacy(web.links.store.path.parent,activate=True)
    web.module.product_services().attach(web.module.access_service)
    page.goto(origin+'/opn/'+web.token_a)
    page.get_by_role('button',name='Поделиться ссылкой').click()
    page.get_by_label('Нет номера').check()
    page.get_by_role('button',name='Создать ссылку').click()
    field=page.get_by_label('Гостевая ссылка',exact=True)
    expect(field).to_be_visible()
    url=field.input_value()
    assert url.startswith(origin+'/opn/') and web.token_a not in url
    page.goto(url)
    expect(page.locator('.button-gate').first).to_be_visible()
    web.links.revoke_link(web.token_a)
    page.goto(origin+'/pwa/'+web.token_a+'/gate')
    page.get_by_role('button',name='Обратиться к администратору').click()
    page.get_by_role('button',name='Запросить пропуск').click()
    page.get_by_role('button',name='Написать в чат').click()
    expect(page.locator('[data-desk-history]')).to_contain_text('Прошу предоставить пропуск')
    page.get_by_role('button',name='Закрыть',exact=True).click()
    admin=page.context.browser.new_page()
    try:
        sign_in(admin,origin,'tenant-a')
        admin.goto(origin+'/admin/client-messages')
        admin.locator('[data-desk-threads] button').first.click()
        admin.get_by_role('button',name='Выдать разовый пропуск').click()
        expect(admin.locator('[data-desk-history]')).to_contain_text('Пропуск разрешён')
        expect(page.locator('.button-gate').first).to_be_visible(timeout=6000)
        admin.get_by_label('Ответ клиенту').fill('Пожалуйста, проезжайте')
        admin.get_by_role('button',name='Отправить',exact=True).click()
        expect(admin.locator('[data-desk-history]')).to_contain_text('Пожалуйста, проезжайте')
    finally:
        admin.close()
    page.goto(origin+'/pwa/'+web.token_a+'/gate#messages')
    expect(page.locator('[data-desk-history]')).to_contain_text('Пожалуйста, проезжайте')


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in headless browser')
def test_mobile_browser_chat_size_and_instant_approval(web,browser_ui):
    page,origin,expect,_=browser_ui
    migrate_legacy(web.links.store.path.parent,activate=True)
    web.module.product_services().attach(web.module.access_service)
    web.branches.update_settings(12,{'auto_approve_pass_requests':True})
    web.links.revoke_link(web.token_a)
    page.set_viewport_size({'width':360,'height':740})
    page.goto(origin+'/opn/'+web.token_a)
    page.get_by_role('button',name='Обратиться к администратору').click()
    page.get_by_role('button',name='Написать в чат').click()
    field=page.get_by_label('Сообщение администратору').bounding_box()
    send=page.get_by_role('button',name='Отправить',exact=True).bounding_box()
    assert field['width']>150 and 40<=send['width']<=48 and field['x']+field['width']<=send['x']
    page.get_by_role('button',name='Закрыть',exact=True).click()
    page.get_by_role('button',name='Обратиться к администратору').click()
    page.get_by_role('button',name='Запросить пропуск').click()
    expect(page.locator('.button-gate').first).to_be_visible()
    # The tab may have been suspended while an administrator deletes the client.
    page.evaluate("Object.defineProperty(document,'hidden',{configurable:true,value:true});document.dispatchEvent(new Event('visibilitychange'))")
    web.links.delete_link(web.token_a)
    page.evaluate("delete document.hidden;document.dispatchEvent(new Event('visibilitychange'))")
    expect(page.get_by_role('heading',name='Доступ невозможен')).to_be_visible(timeout=6500)
    expect(page.locator('a[href*="/admin"]')).to_have_count(0)
    assert web.calls==[]


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in headless browser')
def test_share_form_routes_phone_and_keeps_copyable_guest_link(web, browser_ui, monkeypatch):
    from unittest.mock import Mock
    from storage import config
    page, origin, expect, _ = browser_ui
    migrate_legacy(web.links.store.path.parent, activate=True)
    monkeypatch.setattr(config,'CLIENT_OUTBOUND_ENABLED',True)
    services = web.module.product_services()
    services.attach(web.module.access_service)
    messenger = Mock(return_value={'status':'accepted'})
    monkeypatch.setattr(services.notifications,'send',messenger)
    web.branches.update_settings(12,{'sms_notifications_enabled':True})
    for channel, phone in (('sms','+15550400101'),('chatpush','+15550400102')):
        if channel == 'chatpush':
            services.identity.save_integration('super',12,'chatpush',{'token':'fixture-only'})
        page.goto(origin+'/opn/'+web.token_a)
        page.get_by_role('button', name='Поделиться ссылкой').click()
        page.get_by_label('Телефон гостя').fill(phone)
        page.get_by_label('Отправить ссылку гостю').check()
        page.get_by_role('button',name='Создать ссылку').click()
        url = page.get_by_label('Гостевая ссылка',exact=True)
        expect(url).to_be_visible()
        issued = url.input_value().rsplit('/',1)[1]
        sent_channel, recipient, text, context = messenger.call_args.args
        assert sent_channel == channel and recipient == phone and ('/opn/'+issued) in text
        assert context['branch_id'] == '12' and web.links.load_links()[issued]['user_name'] == 'Гость от Client A'
