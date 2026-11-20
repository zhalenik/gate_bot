"""Offline acceptance of branch correspondence and delegated guest gate access."""
from concurrent.futures import ThreadPoolExecutor
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


def test_approval_is_scoped_idempotent_and_replies_follow_current_identity(product):
    p=product
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
def test_delegation_rechecked_before_hardware(product,change,monkeypatch):
    p=product
    p.services.attach(p.access)
    desk=p.services.desk
    gate=p.branches.get_branch(10).buttons[0]
    url=desk.share(p.token,gate.id)
    assert p.token not in url
    raw=url.split('/')[-1]
    assert desk.shared(raw)=={'branch':'North','gate':'Entry'}
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
    gate=web.branches.get_branch(123).buttons[0]
    share=post(client,key,path+'/share',{'gate_id':gate.id}).get_json()['path']
    stranger=web.app.test_client()
    page=stranger.get(share)
    assert page.status_code==200 and token.encode() not in page.data and b'/preferences/' not in page.data and b'data-client-desk' not in page.data
    assert stranger.post(share).status_code==403
    assert stranger.get('/admin/api/desk/threads').status_code==401
    thread,=services.desk.threads('tenant-b')
    key=login(client,'tenant-a')
    thread_path='/admin/api/desk/threads/'+str(thread['id'])
    assert client.get(thread_path).status_code==403
    assert post(client,key,thread_path+'/reply',{'body':'hello','key':'test-reply'}).status_code==403
    assert client.get('/admin/api/desk/threads').get_json()=={'items':[]}


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
    page.get_by_role('button',name='Создать ссылку').click()
    field=page.get_by_label('Гостевая ссылка',exact=True)
    expect(field).to_be_visible()
    url=field.input_value()
    assert url.startswith(origin+'/shared/') and web.token_a not in url
    page.goto(url)
    expect(page.get_by_role('button',name='Открыть ворота')).to_be_visible()
    web.links.revoke_link(web.token_a)
    page.goto(origin+'/pwa/'+web.token_a+'/gate')
    page.get_by_role('button',name='Обратиться к администратору').click()
    page.get_by_role('button',name='Запросить пропуск').click()
    expect(page.locator('[data-desk-history]')).to_contain_text('Прошу предоставить пропуск')
    sign_in(page,origin,'tenant-a')
    page.goto(origin+'/admin/client-messages')
    page.locator('[data-desk-threads] button').first.click()
    page.get_by_role('button',name='Разрешить до конца дня').click()
    expect(page.locator('[data-desk-history]')).to_contain_text('Пропуск разрешён')
    page.get_by_label('Ответ клиенту').fill('Пожалуйста, проезжайте')
    page.get_by_role('button',name='Отправить',exact=True).click()
    page.goto(origin+'/pwa/'+web.token_a+'/gate#messages')
    expect(page.locator('[data-desk-history]')).to_contain_text('Пожалуйста, проезжайте')
