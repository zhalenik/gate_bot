"""Explicit superadmin telecom operations, separate from gate authorization."""
import hashlib
import re

from link_manager import normalize_phone
from sms_submit import encode_submit
from telephony_store import TelephonyStore, stamp, upgrade, chat_key


class TelephonyService:
    def __init__(self, store=None, queue=None):
        self.store = store or TelephonyStore()
        self.queue = queue

    def submit(self, admin, actor, data):
        if not admin or admin.get('is_super') is not True or admin.get('disabled'):
            raise PermissionError('Требуются права суперадминистратора')
        kind, phone = data.get('kind'), data.get('phone')
        source = None
        if kind == 'sms_delete':
            if not isinstance(data.get('source_id'), str):
                raise ValueError('Укажите сохранённую SMS')
            source = self.store.store.read().get('records', {}).get(data.get('source_id'))
            if not source or source['kind'] != 'sms' or source['direction'] != 'incoming':
                raise ValueError('Сохранённая входящая SMS не найдена')
            phone = source.get('phone', '')
        elif kind not in {'call', 'sms'} or not isinstance(phone, str) or not re.fullmatch(r'[+0-9\s().-]{7,64}', phone):
            raise ValueError('Введите корректный номер телефона с кодом страны')
        if kind != 'sms_delete':
            phone = normalize_phone(phone)
        if kind != 'sms_delete' and not re.fullmatch(r'\+[1-9][0-9]{6,14}', phone):
            raise ValueError('Введите телефон с кодом страны')
        text = data.get('text', '') if kind == 'sms' else ''
        count = len(encode_submit(phone, text)) if kind == 'sms' else 0
        duration = data.get('duration', 20)
        if kind == 'call' and (type(duration) is not int or not 1 <= duration <= 30):
            raise ValueError('Длительность вызова — от 1 до 30 секунд')
        request_id = data.get('request_id')
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{32}', request_id):
            raise ValueError('Обновите форму перед отправкой')
        record_id = hashlib.sha256((actor + ':' + request_id).encode()).hexdigest()
        with self.store.store.transaction() as state:
            upgrade(state)
            records = state['maintenance'] if source else state['records']
            if record_id in state['requests']:
                return {'status': 'success', 'record_id': record_id, 'duplicate': True,
                        'delivery': state['requests'][record_id]['status'], 'chat_id': chat_key(phone) if kind == 'sms' else None}
            state['requests'][record_id] = {'status': 'queued'}
            records[record_id] = {'id': record_id, 'kind': 'sms' if source else kind, 'direction': 'outgoing',
                'timestamp': stamp(), 'phone': phone, 'text': text, 'actor': actor,
                'duration': duration if kind == 'call' else None, 'total_parts': count,
                'status': 'queued', 'read': True, 'segments': [], 'action': kind}
        if self.queue is None:
            from sim800c import _get_queue
            queue = _get_queue()
        else:
            queue = self.queue
        result = queue.add_manual(kind, phone, text=text, duration=duration, record_id=record_id,
                                  locations=source.get('sim_locations', []) if source else None,
                                  source_id=source['id'] if source else None)
        if result.get('status') != 'success':
            self.store.update(record_id, status='failed', code=result.get('code'))
        return {**result, 'record_id': record_id, 'chat_id': chat_key(phone) if kind == 'sms' else None}
