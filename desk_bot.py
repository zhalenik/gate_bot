"""Conversations and guest links shared by Telegram and MAX menu handlers."""
import hashlib
import re
import time
import uuid

from link_manager import access_is_active
from identity_service import valid_phone
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
        if kind in {'share','gate'}:
            if not state['active']:
                raise PermissionError('Для передачи ссылки нужен действующий пропуск')
            nonce=uuid.uuid4().hex[:20]
            workflow._set(platform,user,{'phase':'guest_phone','key':key,'nonce':nonce,'expires':time.time()+600})
            return {'text':'Введите номер телефона гостя или выберите «Нет номера».',
                    'actions':[{'label':'Нет номера','callback':'x:guest:none:'+nonce},{'label':'Отмена','callback':'x:cancel'}]}
        if kind=='request':
            desk.client_send(token,'request','',uuid.uuid4().hex)
            text='Разовый пропуск выдан.' if desk.client_view(token)['active'] else 'Запрос передан администратору филиала.'
            return {'text':text,'actions':[back]}
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
    match=re.fullmatch(r'x:guest:(none|send|copy):([a-f0-9]{20})',value)
    if match:
        action,nonce=match.groups()
        dialog=workflow._get(platform,user)
        if dialog.get('nonce')!=nonce or dialog.get('phase')!=('guest_phone' if action=='none' else 'guest_send'):
            raise ValueError('Откройте форму гостевого пропуска заново')
        return _issue_guest(workflow,platform,user,dialog,action=='send')
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
            actions.append({'label':'Выдать разовый пропуск','callback':f'x:approve:{identifier}:{m["id"]}'})
            break
    return {'text':'\n'.join(lines),'actions':actions}


def _issue_guest(workflow, platform, user, dialog, send):
    token=_client(workflow,platform,user,dialog['key'])
    result=workflow.access.desk.share(token,phone=dialog.get('phone'),no_phone=not dialog.get('phone'),send=send,key=dialog['nonce'])
    workflow._set(platform,user,{})
    text='Гостевой пропуск готов.'
    if send:
        text+=(' Ссылка отправлена.' if result['delivery'] in {'accepted','sent','success'}
               else ' Сервис не подтвердил отправку. Передайте ссылку гостю.')
    return {'text':text+'\n'+config.BASE_URL.rstrip('/')+result['path'],'actions':[]}


def input_message(workflow, platform, user, dialog, text):
    phase=dialog.get('phase')
    if phase=='guest_phone':
        token=_client(workflow,platform,user,dialog['key'])
        dialog={**dialog,'phone':valid_phone(text),'phase':'guest_send'}
        if not workflow.access.desk.client_view(token)['share_delivery']:
            return _issue_guest(workflow,platform,user,dialog,False)
        workflow._set(platform,user,dialog)
        return {'text':'Отправить ссылку гостю?', 'actions':[
            {'label':'Да, отправить','callback':'x:guest:send:'+dialog['nonce']},
            {'label':'Только получить ссылку','callback':'x:guest:copy:'+dialog['nonce']}]}
    if phase=='guest_send':
        return {'text':'Отправить ссылку гостю?', 'actions':[
            {'label':'Да, отправить','callback':'x:guest:send:'+dialog['nonce']},
            {'label':'Только получить ссылку','callback':'x:guest:copy:'+dialog['nonce']}]}
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
