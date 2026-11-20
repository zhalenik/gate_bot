"""Conversations and guest links shared by Telegram and MAX menu handlers."""
import hashlib
import re
import time
import uuid

from link_manager import access_is_active
from storage import config


def client_key(token):
    return hashlib.sha256(token.encode()).hexdigest()[:16]


def client_actions(workflow, platform, user_id):
    desk = getattr(workflow.access,'desk',None)
    if not desk:
        return []
    actions=[]
    for token, data in workflow.access._identity_records(platform,user_id):
        branch=workflow.access.branches.get_branch(data['branch_id'])
        if not branch:
            continue
        key=client_key(token)
        actions.append({'label':'Обратиться к администратору · '+branch.name,'callback':'x:contact:'+key})
        if branch.enabled and access_is_active(data):
            actions.append({'label':'Поделиться ссылкой · '+branch.name,'callback':'x:share:'+key})
    return actions


def _client(workflow, platform, user, key):
    for token, _ in workflow.access._identity_records(platform,user):
        if client_key(token)==key:
            return token
    raise PermissionError('Обновите меню для своего пропуска')


def handle(workflow, platform, user, value):
    desk=getattr(workflow.access,'desk',None)
    if not desk:
        return None
    match=re.fullmatch(r'x:(contact|dial|write|request|share|gate):([a-f0-9]{16})(?::([A-Za-z0-9_-]{1,32}))?',value)
    if match:
        kind,key,gate=match.groups()
        token=_client(workflow,platform,user,key)
        state=desk.client_view(token)
        back={'label':'К диалогу','callback':'x:contact:'+key}
        if kind=='dial':
            workflow.access._emit('admin_contact_requested',state['branch_id'])
            numbers=[c['label']+' · '+c['phone'] for c in state['contacts']]
            return {'text':'\n'.join(numbers) or 'Номер пока не указан','actions':[back]}
        if kind=='share':
            return {'text':'Выберите ворота для гостя. Ссылка действует один час и допускает одно открытие.',
                    'actions':[{'label':b['label'],'callback':f'x:gate:{key}:{b["id"]}'} for b in state['gates']]}
        if kind=='gate':
            return {'text':'Гостевая ссылка для передачи:\n'+config.BASE_URL+desk.share(token,gate),'actions':[]}
        if kind=='request':
            desk.client_send(token,'request','',uuid.uuid4().hex)
            return {'text':'Запрос передан администратору филиала.','actions':[back]}
        if kind=='write':
            workflow._set(platform,user,{'phase':'desk_client','key':key,'nonce':uuid.uuid4().hex,'expires':time.time()+600})
            return {'text':'Введите сообщение администратору филиала.','actions':[{'label':'Отмена','callback':'x:cancel'}]}
        lines=[state['branch']]+[('Администратор' if m['sender']=='admin' else 'Вы')+': '+m['body'][:350] for m in state['messages'][-5:]]
        actions=[{'label':'Отправить сообщение','callback':'x:write:'+key}]
        if state['contacts']:
            actions.append({'label':'Позвонить','callback':'x:dial:'+key})
        if not state['active']:
            actions.append({'label':'Запросить пропуск','callback':'x:request:'+key})
        return {'text':'\n'.join(lines),'actions':actions}
    match=re.fullmatch(r'x:(inbox|thread|respond|approve):(\d{1,16})(?::(\d{1,16}))?',value)
    if not match:
        return None
    admin=workflow._admin(platform,user)
    kind,identifier,message_id=match.groups()
    if kind=='inbox':
        offset=int(identifier)
        rows=desk.threads(admin['username'])
        actions=[{'label':('Запрос пропуска · ' if r['pending'] else '')+r['name'],'callback':'x:thread:'+str(r['id'])} for r in rows[offset:offset+10]]
        if offset+10<len(rows):
            actions.append({'label':'Ещё','callback':f'x:inbox:{offset+10}'})
        return {'text':'Переписка с клиентами' if rows else 'Новых обращений пока нет','actions':actions}
    thread=desk.admin_view(admin['username'],identifier)
    if kind=='respond':
        workflow._set(platform,user,{'phase':'desk_reply','thread':identifier,'nonce':uuid.uuid4().hex,'expires':time.time()+600})
        return {'text':'Введите ответ клиенту.','actions':[{'label':'Отмена','callback':'x:cancel'}]}
    if kind=='approve':
        desk.approve(admin['username'],identifier,message_id)
        return {'text':'Пропуск разрешён.','actions':[{'label':'К переписке','callback':'x:thread:'+identifier}]}
    lines=[thread['name']]+[('Клиент' if m['sender']=='client' else 'Администратор')+': '+m['body'][:350] for m in thread['messages'][-6:]]
    actions=[{'label':'Ответить','callback':'x:respond:'+identifier}]
    for m in reversed(thread['messages']):
        if m['kind']=='request' and not m['approved']:
            actions.append({'label':'Разрешить до конца дня','callback':f'x:approve:{identifier}:{m["id"]}'})
            break
    return {'text':'\n'.join(lines),'actions':actions}


def input_message(workflow, platform, user, dialog, text):
    phase=dialog.get('phase')
    if phase not in {'desk_client','desk_reply'}:
        return None
    if phase=='desk_client':
        token=_client(workflow,platform,user,dialog['key'])
        workflow.access.desk.client_send(token,'message',text,dialog['nonce'])
        back='x:contact:'+dialog['key']
    else:
        actor=workflow._admin(platform,user)
        workflow.access.desk.reply(actor['username'],dialog['thread'],text,dialog['nonce'])
        back='x:thread:'+dialog['thread']
    workflow._set(platform,user,{})
    return {'text':'Сообщение сохранено.','actions':[{'label':'К диалогу','callback':back}]}
