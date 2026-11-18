"""Shared branch-scoped administrator workflows for both messenger adapters."""
from datetime import datetime
import re
import time
import uuid

from link_manager import access_is_active, normalize_phone
from storage import config
from timeutil import end_of_day, parse_datetime


class BotWorkflows:
    def __init__(self, access, state):
        self.access, self.state = access, state

    def _admin(self, platform, user_id, branch_id=None):
        identity = self.access.identity
        admin = identity.bot_admin(platform, user_id) if identity else None
        if not admin:
            raise PermissionError('Подключите бот из своего кабинета администратора')
        if branch_id is not None:
            if not admin['is_super'] and str(branch_id) not in admin['branch_ids']:
                raise PermissionError('Нет доступа к этому филиалу')
            if not self.access.branches.get_branch(branch_id):
                raise ValueError('Филиал не найден')
        return admin

    def _get(self, platform, user_id):
        value = self.state.get_checkpoint('dialogs:' + platform, str(user_id), {})
        return value if value.get('expires', 0) > time.time() else {}

    def _set(self, platform, user_id, value):
        self.state.set_checkpoint('dialogs:' + platform, str(user_id), value)

    def actions(self, platform, user_id):
        actions = []
        admin=self.access.identity.bot_admin(platform,user_id) if self.access.identity else None
        if admin:
            actions.append({'label': 'Администрирование', 'callback': 'x:admin:0'})
            actions.append({'label': 'Настройки администратора', 'url': config.BASE_URL + '/admin/notification-settings'})
            actions.append({'label': 'Помощь', 'url': config.BASE_URL + '/admin/help'})
            if getattr(self.access,'support',None) and admin.get('is_super'):
                actions.append({'label':'Обращения в поддержку','callback':'x:support:0'})
        if self._get(platform, user_id):
            actions.append({'label': 'Отменить ввод', 'callback': 'x:cancel'})
        for token, data in self.access._identity_records(platform, user_id):
            if data.get('is_temporary'):
                continue
            branch = self.access.branches.get_branch(data['branch_id'])
            if branch:
                actions.append({'label': 'Уведомления · ' + branch.name,
                                'url': config.BASE_URL + '/preferences/' + token})
        return actions

    def action(self, platform, user_id, value):
        if value == 'x:cancel':
            self._set(platform, user_id, {})
            return {'text': 'Действие отменено', 'actions': []}
        admin = self._admin(platform, user_id)
        match=re.fullmatch(r'x:(support|ticket|reply|close):([0-9]{1,18})',value)
        if match:
            support=getattr(self.access,'support',None)
            if support is None:raise ValueError('Поддержка пока недоступна')
            actor=support.bot_actor(platform,user_id);kind,identifier=match.groups()
            if kind=='support':
                result=support.list_threads(actor['username'],before=None if identifier=='0' else identifier,limit=8)
                actions=[{'label':('● ' if item['unread'] else '')+'№'+str(item['id'])+' · '+item['title'][:45],'callback':'x:ticket:'+str(item['id'])} for item in result['items']]
                if result['next_before']:actions.append({'label':'Следующие обращения','callback':'x:support:'+str(result['next_before'])})
                if identifier!='0':actions.append({'label':'К началу списка','callback':'x:support:0'})
                return {'text':'Обращения в поддержку' if actions else 'Обращений пока нет','actions':actions}
            result=support.messages(actor['username'],identifier,limit=5);thread=result['thread']
            if kind=='reply':
                if not thread['can_reply']:raise PermissionError('Автор больше не имеет доступа к филиалу')
                self._set(platform,user_id,{'phase':'support_reply','thread_id':int(identifier),'nonce':uuid.uuid4().hex,'expires':time.time()+300})
                return {'text':'Ответ в обращение №'+identifier+' · '+thread['title']+'\nВведите текст одним сообщением (до 4000 символов).','actions':[{'label':'Отмена','callback':'x:cancel'}]}
            if kind=='close':support.set_state(actor['username'],identifier,'closed')
            lines=['Обращение №'+identifier+' · '+thread['branch_name']+'\n'+thread['owner_name']+' · '+thread['title']]
            for message in result['messages']:lines.append(('Поддержка' if message['sender_role']=='superadmin' else 'Администратор')+': '+message['text'][:500])
            actions=[{'label':'Ответить','callback':'x:reply:'+identifier}] if thread['can_reply'] else []
            actions.extend([{'label':'Закрыть обращение','callback':'x:close:'+identifier},{'label':'Все обращения','callback':'x:support:0'}])
            if result['messages']:support.mark_read(actor['username'],identifier,result['messages'][-1]['id'])
            return {'text':'\n\n'.join(lines),'actions':actions}
        if re.fullmatch(r'x:admin:[0-9]{1,6}', value):
            page = int(value.rsplit(':', 1)[1])
            branches = [b for b in self.access.branches.get_all_branches()
                        if admin['is_super'] or str(b.branch_id) in admin['branch_ids']]
            actions = [{'label': b.name, 'callback': 'x:branch:' + str(b.branch_id)} for b in branches[page*15:(page+1)*15]]
            if page:
                actions.append({'label': '← Назад', 'callback': f'x:admin:{page-1}'})
            if (page+1)*15 < len(branches):
                actions.append({'label': 'Далее →', 'callback': f'x:admin:{page+1}'})
            return {'text': 'Выберите филиал', 'actions': actions}
        match = re.fullmatch(r'x:(branch|issue):([0-9]{1,24})', value)
        if match:
            kind, branch_id = match.groups()
            self._admin(platform, user_id, branch_id)
            if kind == 'issue':
                self._set(platform, user_id, {'phase': 'phone', 'branch_id': branch_id, 'expires': time.time()+300})
                return {'text': 'Введите номер клиента с кодом страны. Пропуск будет действовать до конца дня филиала.',
                        'actions': [{'label': 'Отмена', 'callback': 'x:cancel'}]}
            return {'text': self.access.branches.get_branch(branch_id).name, 'actions': [
                {'label': 'Выдать разовый пропуск', 'callback': 'x:issue:' + branch_id},
                {'label': 'Временные пропуска', 'callback': 'x:passes:temp:' + branch_id + ':0'},
                {'label': 'Постоянные пропуска', 'callback': 'x:passes:permanent:' + branch_id + ':0'},
                {'label': 'Выбрать филиал', 'callback': 'x:admin:0'}]}
        match = re.fullmatch(r'x:passes:(temp|permanent):([0-9]{1,24}):([0-9]{1,6})', value)
        if match:
            category, branch_id, page = match.groups(); page = int(page)
            self._admin(platform, user_id, branch_id)
            rows = [d for d in self.access.links.load_links().values() if str(d.get('branch_id')) == branch_id
                    and access_is_active(d) and bool(d.get('access_expires') and not d.get('admin_role_active')) == (category == 'temp')]
            rows.sort(key=lambda d: (d.get('user_name') or '').casefold())
            lines = ['Временные пропуска' if category == 'temp' else 'Постоянные пропуска']
            for data in rows[page*15:(page+1)*15]:
                line = (data.get('user_name') or 'Клиент')[:80] + ' · ' + (data.get('phone') or '')
                if data.get('access_expires') and not data.get('admin_role_active'):
                    line += ' · до ' + parse_datetime(data['access_expires']).strftime('%d.%m %H:%M')
                lines.append(line)
            if not rows:
                lines.append('Действующих пропусков нет')
            actions = []
            if page:
                actions.append({'label': '← Назад', 'callback': f'x:passes:{category}:{branch_id}:{page-1}'})
            if (page+1)*15 < len(rows):
                actions.append({'label': 'Далее →', 'callback': f'x:passes:{category}:{branch_id}:{page+1}'})
            actions.append({'label': 'К филиалу', 'callback': 'x:branch:' + branch_id})
            return {'text': '\n'.join(lines), 'actions': actions}
        match = re.fullmatch(r'x:confirm:([a-f0-9]{24})', value)
        if match:
            dialog = self._get(platform, user_id)
            if dialog.get('phase') != 'confirm' or dialog.get('nonce') != match[1]:
                raise ValueError('Подтверждение истекло. Начните заново')
            branch_id, phone = dialog['branch_id'], dialog['phone']
            self._admin(platform, user_id, branch_id)
            # Consume the dialog before the side effect; a duplicate callback
            # cannot create another pass even if its platform callback ID changes.
            self._set(platform, user_id, {})
            matches = [(t,d) for t,d in self.access.links.load_links().items()
                       if str(d.get('branch_id')) == branch_id and normalize_phone(d.get('phone')) == phone]
            if len(matches) > 1:
                raise ValueError('У номера несколько клиентских записей. Выберите клиента в веб-кабинете')
            branch = self.access.branches.get_branch(branch_id)
            expiry = end_of_day(tz=branch.timezone)
            if matches:
                token, data = matches[0]
                if data.get('admin_blocked'):
                    raise ValueError('Клиент заблокирован. Снимите блокировку в кабинете')
                if not access_is_active(data) or (data.get('access_expires') and parse_datetime(data['access_expires']) < expiry):
                    self.access.links.set_expiry_at(token, expiry)
                    self.access.links.toggle_user_access(token, True)
            else:
                token = self.access.links.create_link({'branch_id': int(branch_id), 'phone': phone,
                    'user_name': 'Гость', 'gate_access': True, 'is_temporary': True, 'mailing_enabled': False}, expires_at=expiry)
            self.access._emit('access_changed', branch_id, client_name='Пропуск выдан администратором', event_id=match[1])
            return {'text': 'Пропуск готов. Уведомление отправляется по настройкам клиента.\n' + config.BASE_URL + '/opn/' + token,
                    'actions': [{'label': 'К филиалу', 'callback': 'x:branch:' + branch_id}]}
        raise ValueError('Действие устарело. Откройте меню')

    def message(self, platform, user_id, text):
        dialog = self._get(platform, user_id)
        if not dialog:
            return None
        self._admin(platform, user_id, dialog.get('branch_id'))
        if dialog.get('phase')=='support_reply':
            support=getattr(self.access,'support',None)
            if support is None:raise ValueError('Поддержка пока недоступна')
            actor=support.bot_actor(platform,user_id)
            if isinstance(text,str) and text.strip().lower() in {'/menu','/help','меню','помощь'}:
                self._set(platform,user_id,{})
                return {'text':'Ввод ответа отменён','actions':[{'label':'Все обращения','callback':'x:support:0'}]}
            support.send(actor['username'],text,'bot:'+platform+':'+dialog['nonce'],thread_id=dialog['thread_id'])
            if self._get(platform,user_id).get('nonce')==dialog['nonce']:self._set(platform,user_id,{})
            return {'text':'Ответ сохранён в обращении №'+str(dialog['thread_id'])+'. Администратор увидит его в кабинете.',
                    'actions':[{'label':'К обращению','callback':'x:ticket:'+str(dialog['thread_id'])},{'label':'Все обращения','callback':'x:support:0'}]}
        if dialog.get('phase') != 'phone':
            return {'text': 'Подтвердите выпуск пропуска кнопкой или отмените действие', 'actions': [
                {'label': 'Выдать пропуск', 'callback': 'x:confirm:' + dialog['nonce']},
                {'label': 'Отмена', 'callback': 'x:cancel'}]}
        from identity_service import valid_phone
        try:phone=valid_phone(text)
        except ValueError:
            return {'text':'Номер не распознан. Проверьте телефон и код страны','actions':[{'label':'Отмена','callback':'x:cancel'}]}
        dialog.update(phase='confirm', phone=phone, nonce=uuid.uuid4().hex[:24])
        self._set(platform, user_id, dialog)
        return {'text': 'Выдать пропуск для ' + phone + '?', 'actions': [
            {'label': 'Выдать пропуск', 'callback': 'x:confirm:' + dialog['nonce']},
            {'label': 'Отмена', 'callback': 'x:cancel'}]}
