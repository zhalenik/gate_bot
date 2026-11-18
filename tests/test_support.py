"""Private support authorization and durable bot notification acceptance, offline."""
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask
import pytest

from bot_state import BotStateStore
from bot_workflows import BotWorkflows
from help_catalog import articles_for
from help_support_web import create_blueprint
from storage import config
from storage.sql_documents import read_document,write_document
from support_service import SupportService
from test_identity_product import identities


@pytest.fixture
def support(identities,monkeypatch):
    s=identities
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    with s.product.connect() as db:
        owner=s.service._admin(db,'owner')
        for platform,user_id in [('telegram','90001'),('max','90002')]:
            db.execute('INSERT INTO bot_bindings(person_id,platform,user_id,assurance) VALUES(?,?,?,"admin_verified")',(owner['person_id'],platform,user_id))
        admins=read_document(db,'admins.json');admins['neighbor']={**admins['north'],'person_id':None,'name':'Neighbor'};write_document(db,'admins.json',admins)
    s.service.attach_console_account('neighbor','+15550100777')
    sender=Mock(return_value=True)
    service=SupportService(s.service,clock=lambda:s.clock[0],sender=sender)
    return SimpleNamespace(service=service,identity=s,sender=sender)


def send(s,text='Synthetic support question',**kwargs):
    return s.service.send('north',text,'request-test-'+str(int(s.identity.clock[0])),branch_id='11',**kwargs)


def test_atomic_message_dedup_outbox_and_response(support):
    s=support;first=send(s);again=send(s)
    assert first['thread_id']==again['thread_id'] and again['duplicate']
    assert s.service.process_once()==2 and s.sender.call_count==2
    assert {call.args[0] for call in s.sender.call_args_list}=={'telegram','max'}
    assert s.service.process_once()==0
    result=s.service.send('owner','Synthetic answer','support-answer-001',thread_id=first['thread_id'])
    view=s.service.messages('north',first['thread_id'])
    assert [item['text'] for item in view['messages']]==['Synthetic support question','Synthetic answer']
    assert view['messages'][1]['sender_role']=='superadmin' and not view['messages'][1]['mine']
    assert s.service.list_threads('north')['items'][0]['unread']==1
    s.service.mark_read('north',first['thread_id'],result['message_id'])
    assert s.service.list_threads('north')['items'][0]['unread']==0


def test_same_branch_admin_cannot_read_reply_close_or_reuse_others_key(support):
    s=support;first=send(s)
    assert not s.service.list_threads('neighbor')['items']
    for operation in [lambda:s.service.messages('neighbor',first['thread_id']),lambda:s.service.send('neighbor','answer','another-key-001',thread_id=first['thread_id']),lambda:s.service.set_state('neighbor',first['thread_id'],'closed'),lambda:s.service.mark_read('neighbor',first['thread_id'],first['message_id'])]:
        with pytest.raises(PermissionError):operation()
    with pytest.raises(PermissionError):s.service.send('north','message','other-branch-key',branch_id='12')
    with pytest.raises(ValueError):send(s,text='Changed content')


def test_revoked_owner_and_super_roles_are_checked_before_send(support):
    s=support;first=send(s)
    with s.identity.product.connect() as db:
        person=s.identity.service._admin(db,'north')['person_id']
        db.execute('UPDATE branch_memberships SET status="revoked" WHERE person_id=?',(person,))
    with pytest.raises(PermissionError):s.service.messages('north',first['thread_id'])
    with pytest.raises(PermissionError):s.service.send('owner','reply','answer-after-revoke',thread_id=first['thread_id'])
    assert s.service.process_once()==0;s.sender.assert_not_called()


def test_recipient_role_and_binding_are_rechecked(support):
    s=support;send(s)
    with s.identity.product.connect() as db:
        admins=read_document(db,'admins.json');admins['owner']['disabled']=True;write_document(db,'admins.json',admins)
    assert s.service.process_once()==0;s.sender.assert_not_called()


def test_timeout_never_retries_ambiguous_delivery_after_restart(support):
    s=support;send(s);s.sender.side_effect=TimeoutError('private provider data')
    assert s.service.process_once()==2
    restarted=SupportService(s.identity.service,clock=lambda:s.identity.clock[0],sender=s.sender)
    assert restarted.process_once()==0 and s.sender.call_count==2
    with s.identity.product.connect() as db:
        rows=db.execute('SELECT state,code FROM support_outbox').fetchall()
        assert all(row['state']=='unknown' and row['code']=='delivery_result_unknown' for row in rows)


def test_disabled_or_unready_delivery_stays_queued(support,monkeypatch):
    s=support;send(s);monkeypatch.setattr(config,'OUTBOUND_ENABLED',False)
    assert s.service.process_once()==0
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True);s.service.ready=lambda channel:False
    assert s.service.process_once()==0;s.sender.assert_not_called()
    s.service.ready=lambda channel:True
    assert s.service.process_once()==2


@pytest.mark.parametrize('platform,user_id',[('telegram','90001'),('max','90002')])
def test_super_bot_selects_thread_and_reply_returns_to_correct_browser_chat(support,tmp_path,platform,user_id):
    s=support;first=send(s);s.identity.clock[0]+=1;second=send(s,text='Second issue')
    access=SimpleNamespace(identity=s.identity.service,support=s.service,branches=s.identity.branches)
    access._identity_records=lambda *args:[('client-'+str(i),{'branch_id':'11'}) for i in range(25)]
    workflows=BotWorkflows(access,BotStateStore(tmp_path/'support-bot.sqlite3'))
    assert any(item.get('callback')=='x:support:0' for item in workflows.actions(platform,user_id)[:20])
    listing=workflows.action(platform,user_id,'x:support:0')
    assert any(action['callback']=='x:ticket:'+str(first['thread_id']) for action in listing['actions'])
    workflows.action(platform,user_id,'x:reply:'+str(first['thread_id']))
    reply=workflows.message(platform,user_id,'Reply for first only')
    assert 'сохранён' in reply['text']
    assert s.service.messages('north',first['thread_id'])['messages'][-1]['text']=='Reply for first only'
    assert len(s.service.messages('north',second['thread_id'])['messages'])==1
    assert workflows.message(platform,user_id,'Duplicate incoming update') is None
    workflows.action(platform,user_id,'x:reply:'+str(first['thread_id']))
    workflows.message(platform,user_id,'/menu')
    assert len(s.service.messages('north',first['thread_id'])['messages'])==2
    with pytest.raises(PermissionError):workflows.action(platform,'99999','x:support:0')


def test_bot_reply_rechecks_super_role_after_prompt(support,tmp_path):
    s=support;first=send(s);access=SimpleNamespace(identity=s.identity.service,support=s.service,branches=s.identity.branches)
    workflows=BotWorkflows(access,BotStateStore(tmp_path/'support-bot.sqlite3'))
    workflows.action('telegram','90001','x:reply:'+str(first['thread_id']))
    with s.identity.product.connect() as db:
        admins=read_document(db,'admins.json');admins['owner']['is_super']=False;write_document(db,'admins.json',admins)
    with pytest.raises(PermissionError):workflows.message('telegram','90001','Must not deliver')
    assert len(s.service.messages('north',first['thread_id'])['messages'])==1


def test_help_has_role_and_branch_type_specific_instructions():
    branch={item['id'] for item in articles_for(False,False)}
    assert {'standalone','passes','subscription','support','qr'}<=branch
    assert not {'modem','smtp','logs','plans','yclients-connect','chatpush'}&branch
    owner={item['id'] for item in articles_for(True,True)}
    assert {'modem','smtp','logs','plans','yclients-connect','support-super'}<=owner and 'subscription' not in owner


def test_web_requires_current_role_and_active_branch(support):
    s=support;first=send(s)
    current={'is_super':False,'branch_id':'12'}
    auth=SimpleNamespace(get_current_admin=lambda:current)
    app=Flask(__name__);app.secret_key='offline-session'
    app.register_blueprint(create_blueprint(lambda:{'service':s.identity.service,'auth':auth,'branches':s.identity.branches,'support':s.service}))
    client=app.test_client()
    with client.session_transaction() as cookie:cookie['admin_username']='north'
    assert client.get('/admin/api/support/threads/'+str(first['thread_id'])).status_code==403
    current['branch_id']='11'
    assert client.get('/admin/api/support/threads/'+str(first['thread_id'])).status_code==200
    assert client.post('/admin/api/support/messages',json={'branch_id':'12','text':'no','request_key':'request-key-123'}).status_code==403
    current.clear();assert client.get('/admin/api/support').status_code==401
