"""Durable application notifications; the runtime owns the only delivery worker."""
from datetime import datetime, timezone
from html import escape
import hashlib
import json
import threading
import time
import uuid

from link_manager import access_is_active
from storage import config
from storage.sql_documents import read_document


TITLES = {
    'gate_requested': 'Запрос открытия ворот', 'gate_denied': 'Доступ к воротам отклонён',
    'gate_error': 'Ошибка команды ворот', 'admin_contact_requested': 'Запрошен контакт администратора',
    'invitation_accepted': 'Приглашение администратора принято', 'access_changed': 'Пропуск изменён',
    'crm_error': 'Связь с YCLIENTS недоступна', 'crm_recovered': 'Связь с YCLIENTS восстановлена',
    'modem_error': 'Модем недоступен', 'modem_recovered': 'Связь с модемом восстановлена',
    'delivery_failed': 'Уведомление не отправлено', 'subscription_expiring': 'Подписка скоро закончится',
    'subscription_expired': 'Подписка закончилась', 'quota_warning': 'Квота подходит к концу',
    'quota_exhausted': 'Квота исчерпана', 'sms_received': 'Входящая SMS', 'call_received': 'Входящий звонок',
}


class ProductNotifications:
    def __init__(self, identity, links, branches, billing, *, sender=None, telephony=None):
        self.identity, self.links, self.branches, self.billing = identity, links, branches, billing
        self.store = identity.product
        self.sender = sender or self.send
        self.telephony = telephony
        self.hardware = None
        self.support = None
        self.stop_event = threading.Event()
        self.thread = None
        self.menu_thread = None
        self._menu_refreshers = []
        self._last_modem = None
        self._last_error = None
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS message_outbox(id TEXT PRIMARY KEY,audience TEXT NOT NULL,event TEXT NOT NULL,branch_id TEXT,channel TEXT NOT NULL,recipient TEXT NOT NULL,payload TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL,result TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS notification_inbox(id TEXT PRIMARY KEY,person_id TEXT NOT NULL,branch_id TEXT,event TEXT NOT NULL,payload TEXT NOT NULL,created REAL NOT NULL,read_at REAL)')
            db.execute('CREATE TABLE IF NOT EXISTS message_effects(id TEXT PRIMARY KEY,state TEXT NOT NULL,result TEXT,created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS notification_bridge(id TEXT PRIMARY KEY,created REAL NOT NULL)')

    def _encrypt(self, payload):
        return self.identity._fernet().encrypt(json.dumps(payload, ensure_ascii=False).encode()).decode()

    def _decrypt(self, value):
        return json.loads(self.identity._fernet().decrypt(value.encode()))

    def _enqueue(self, key, audience, event, branch, channel, target, payload, db):
        key = hashlib.sha256(key.encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO message_outbox VALUES(?,?,?,?,?,?,?,"pending",?,?,NULL)',
                   (key, audience, event, str(branch) if branch is not None else None, channel,
                    hashlib.sha256(str(target).encode()).hexdigest(),
                    self._encrypt({**payload, 'target': str(target)}), time.time(), time.time()))
        return key

    def welcome(self, person_id, branch_id, username):
        """A non-secret welcome letter, committed together with the new account."""
        with self.store.connect() as db:
            person = db.execute('SELECT name,email FROM persons WHERE id=?', (person_id,)).fetchone()
            contact = self.identity.admin_preferences(username)
            if not person or contact.get('person_id') != person_id or not contact.get('email'):
                return
            branch = self.branches.get_branch(branch_id)
            if not branch:
                return
            details = {'name': person['name'], 'branch_name': branch.name,
                       'yclients_enabled': getattr(branch, 'yclients_enabled', True)}
            self._enqueue('admin-welcome:' + str(person_id) + ':' + str(branch_id),
                          'welcome', 'admin_welcome', branch_id, 'email', contact['email'],
                          {'person_id': person_id, 'username': username, 'welcome': details,
                           'text': 'Добро пожаловать в GateBot. Вход: ' + config.BASE_URL + '/admin/login'}, db)

    def access_transition(self, payload):
        self.event('access_changed', payload.get('branch_id'), {
            'event_id': hashlib.sha256(json.dumps([payload.get('token'), payload.get('event'),
                         payload.get('access_revision', 0), payload.get('access_expires')]).encode()).hexdigest(),
            'client_name': payload.get('user_name'),
            'message': 'Пропуск активирован' if payload.get('event') == 'granted' else 'Срок или права пропуска изменены'})
        self.enqueue(payload)

    def enqueue(self, payload, *, only_channel=None):
        if not payload.get('mailing_enabled'):
            return
        token = payload.get('token')
        try:
            prefs = self.identity.prefs_for_token(token)
        except (ValueError, PermissionError):
            return
        if not prefs['enabled']:
            return
        event = 'access_granted' if payload.get('event') == 'granted' else 'access_revoked'
        text = ('Ваш пропуск на парковку активен.\n' if event == 'access_granted' else 'Срок действия пропуска изменился.\n')
        text += config.BASE_URL + '/opn/' + token
        targets = {'telegram': payload.get('tg_id'), 'max': payload.get('max_id'),
                   'email': prefs.get('email') if prefs.get('email_verified') else None}
        with self.store.connect() as db:
            for channel, enabled in prefs['channels'].items():
                if only_channel is not None and channel != only_channel:
                    continue
                target = targets.get(channel)
                physical_channel = channel
                if channel == 'messengers':
                    from messenger_service import MessengerService
                    plan = MessengerService(self.identity).plan(db, payload.get('branch_id'), token)
                    if not plan['connected']:
                        continue
                    physical_channel, target = plan['provider'], plan['target']
                if not enabled or not target:
                    continue
                key = json.dumps([token, event, payload.get('access_revision', 0), payload.get('access_expires'), physical_channel, target])
                self._enqueue(key, 'client', event, payload.get('branch_id'), physical_channel, target,
                              {'text': text, 'token': token, 'revision': payload.get('access_revision', 0),
                               'person_id': prefs['person_id'], 'preference_channel': channel}, db)

    def event(self, event, branch_id, payload=None):
        if event not in TITLES:
            return
        payload = payload or {}
        branch = self.branches.get_branch(branch_id) if branch_id is not None else None
        title = TITLES[event]
        lines = [title + (' · ' + branch.name if branch else '')]
        for field in ('client_name', 'gate_label', 'phone', 'time', 'text', 'message', 'delivery'):
            value = payload.get(field)
            if value not in (None, ''):
                lines.append(str(value))
        if payload.get('days_remaining') is not None:
            lines.append('До окончания: ' + str(max(0, payload['days_remaining'])) + ' дн.')
        if payload.get('quota') is not None:
            lines.append('Использовано: ' + str(payload.get('spent', 0)) + ' из ' + str(payload['quota']))
        text = '\n'.join(lines)
        # Repeated outages are grouped into ten-minute intervals; call/SMS and
        # billing events provide stable event IDs and never use time deduplication.
        event_id = payload.get('event_id') or hashlib.sha256(json.dumps(
            [event, branch_id, payload, int(time.time() // 600)], sort_keys=True).encode()).hexdigest()
        contacts = self.identity.contacts_for_admin_events(branch_id, event)
        with self.store.connect() as db:
            # A scheduler/transport callback may outlive its branch. Check in
            # the write transaction so deletion cannot leave a new orphan event.
            if branch_id is not None and str(branch_id) not in read_document(db, 'branches.json'):
                return
            for contact in contacts:
                key = json.dumps([event, event_id, contact['person_id'], contact['channel']])
                self._enqueue(key, 'admin', event, branch_id, contact['channel'], contact['target'],
                    {'text': text, 'person_id': contact['person_id'], 'username': contact['username']}, db)
            # The web inbox is available even if external channels are disabled.
            for row in db.execute('SELECT username,person_id FROM web_accounts').fetchall():
                try:
                    admin = self.identity.web_admin(row['username'])
                except PermissionError:
                    continue
                if not admin.get('is_super') and (event in {'sms_received','call_received'} or str(branch_id) not in admin['branch_ids']):
                    continue
                key = hashlib.sha256(json.dumps([event, event_id, row['person_id']]).encode()).hexdigest()
                db.execute('INSERT OR IGNORE INTO notification_inbox VALUES(?,?,?,?,?,?,NULL)',
                           (key, row['person_id'], str(branch_id) if branch_id is not None else None,
                            event, self._encrypt({'text': text}), time.time()))

    def _allowed(self, row, payload):
        if row['audience'] == 'welcome':
            with self.store.connect() as db:
                try:
                    admin = self.identity._admin(db, payload['username'])
                except PermissionError:
                    return False
                contact = self.identity._admin_contact(db, admin)
                return bool(contact['email'] and contact['email'] == payload['target'] and
                            admin.get('person_id') == payload['person_id'] and
                            row['branch_id'] in admin['branch_ids'])
        if row['audience'] == 'admin':
            return any(c['person_id'] == payload['person_id'] and c['channel'] == row['channel'] and
                       str(c['target']) == payload['target'] for c in
                       self.identity.contacts_for_admin_events(row['branch_id'], row['event']))
        current = self.links.load_links().get(payload.get('token'))
        if not current or not current.get('mailing_enabled') or current.get('access_revision', 0) != payload.get('revision'):
            return False
        try:
            prefs = self.identity.prefs_for_token(payload['token'])
        except (ValueError, PermissionError):
            return False
        logical_channel = 'messengers' if row['channel'] in {'chatpush', 'yclients'} else row['channel']
        if not prefs['enabled'] or not prefs['channels'].get(logical_channel):
            return False
        target = {'telegram': current.get('tg_id'), 'max': current.get('max_id'),
                  'email': prefs.get('email') if prefs.get('email_verified') else None}.get(row['channel'])
        if logical_channel == 'messengers':
            from messenger_service import MessengerService
            with self.store.connect() as db:
                plan = MessengerService(self.identity).plan(db, row['branch_id'], payload['token'])
            if not plan['connected'] or plan['provider'] != row['channel']:
                return False
            target = plan['target']
        if str(target) != payload['target']:
            return False
        return access_is_active(current) == (row['event'] == 'access_granted')

    def send(self, channel, target, text, context):
        """Explicit service delivery, never retries an ambiguous external effect."""
        if self.stop_event.is_set():
            return {'status': 'failed', 'code': 'runtime_stopping'}
        if not config.OUTBOUND_ENABLED:
            return {'status': 'failed', 'code': 'outbound_disabled'}
        if channel == 'yclients' and not config.YCLIENTS_ENABLED:
            return {'status': 'pending', 'code': 'yclients_disabled'}
        if channel in {'telegram', 'max'}:
            module = __import__('telegram_bot' if channel == 'telegram' else 'max_bot')
            if not module.status().get('ready'):
                return {'status': 'pending', 'code': 'bot_not_ready'}
        effect_id = hashlib.sha256(json.dumps([channel, context.get('idempotency_key') or uuid.uuid4().hex]).encode()).hexdigest()
        with self.store.connect() as db:
            branch_id=context.get('branch_id')
            if branch_id is not None:
                if str(branch_id) not in read_document(db,'branches.json'):
                    # Registration can send its proof before creating a branch.
                    # Deletion removes existing branch challenges atomically.
                    # Claiming a send here makes deletion wait for its outcome.
                    pending_otp=(context.get('purpose')=='otp' and db.execute(
                        'SELECT 1 FROM identity_otp WHERE id=? AND state="sending"',
                        (context.get('idempotency_key'),)).fetchone())
                    if not pending_otp:
                        return {'status':'skipped','code':'branch_deleted','confirmed':False}
            previous = db.execute('SELECT * FROM message_effects WHERE id=?', (effect_id,)).fetchone()
            if previous:
                if previous['state'] == 'unknown':
                    return {'status': 'unknown', 'delivery': 'unknown', 'code': 'effect_result_unknown'}
                return json.loads(previous['result']) if previous['result'] else {'status': 'unknown', 'code': 'effect_in_progress'}
            db.execute('INSERT INTO message_effects VALUES(?,"sending",NULL,?)', (effect_id, time.time()))
        result = {'status': 'failed', 'code': 'channel_unavailable'}
        sms_record = None
        sms_journal = None
        try:
            if channel in {'telegram', 'max'}:
                module = __import__('telegram_bot' if channel == 'telegram' else 'max_bot')
                if module.status().get('ready'):
                    sent = module.notify_text(str(target), text)
                    result = {'status': 'accepted' if sent else 'unknown', 'confirmed': False}
            elif channel == 'email':
                from smtp_mailing import send_email
                if context.get('event') == 'admin_welcome':
                    from welcome_email import render_welcome
                    subject = 'Добро пожаловать в GateBot'
                    html = render_welcome(context.get('welcome') or {}, config.BASE_URL)
                else:
                    subject, html = 'GateBot — уведомление', '<p>' + escape(text).replace('\n', '<br>') + '</p>'
                sent = send_email(target, subject, html)
                result = {'status': 'accepted' if sent else 'failed', 'confirmed': False}
            elif channel in {'chatpush', 'yclients'} and context.get('purpose') == 'integration_test':
                from integration_test_delivery import IntegrationTestDelivery
                result = IntegrationTestDelivery(self.identity).deliver(channel, target, text, context)
            elif channel == 'yclients' or (channel == 'chatpush' and context.get('preference_channel') == 'messengers'):
                from messenger_service import MessengerService
                result = MessengerService(self.identity).send(context.get('branch_id'), context.get('client_token'), channel, target, text)
            elif channel == 'chatpush':
                from chatpush import send_message
                result = send_message(target, text, context.get('branch_id'),
                    credentials=self.identity.integration_credentials(context.get('branch_id'), 'chatpush'), external_id=effect_id)
            elif channel == 'sms':
                import sim800c
                from sms_submit import encode_submit
                parts = encode_submit(target, text)  # Validate before entering the hardware queue.
                queue = sim800c._get_queue()
                # Every SMS, including verification codes, belongs in the
                # private superadmin conversation archive. The ordinary audit
                # and public queue status still contain no phone/text/code.
                sms_journal = queue.journal
                sms_record = sms_journal.add('sms', 'outgoing', id=effect_id,
                    phone=target, text=text, status='simulated' if queue.dry_run else 'queued',
                    total_parts=len(parts), segments=[], read=True, actor='GateBot',
                    purpose='verification' if context.get('purpose') == 'otp' else 'notification')
                if queue.dry_run:
                    result = {'status': 'failed', 'delivery': 'simulated', 'code': 'hardware_required'}
                else:
                    sensitive = context.get('purpose') == 'otp'
                    result = queue.add_manual('sms', target, text=text, record_id=sms_record,
                                             sensitive=sensitive, priority=sensitive, command_id=effect_id)
        except Exception as exc:
            result = {'status': 'unknown', 'code': 'transport_result_unknown'}
            if channel == 'max':
                from max_api import MaxApiError
                if isinstance(exc, MaxApiError):
                    # A multi-part send may have partially succeeded. Preserve
                    # uncertainty and expose only the adapter's safe diagnostic.
                    result.update(exc.diagnostic())
        if sms_record:
            result['record_id'] = sms_record
            # The hardware worker owns accepted commands and can finish before
            # add_manual returns. Never overwrite its terminal status as queued.
            if result.get('status') != 'success':
                sms_journal.update(sms_record,
                    status=result.get('delivery') or ('failed' if result.get('status') == 'error' else result.get('status', 'unknown')),
                    code=result.get('code'))
        with self.store.connect() as db:
            db.execute('UPDATE message_effects SET state=?,result=? WHERE id=?',
                       (result.get('delivery') or result.get('status', 'unknown'), json.dumps(result), effect_id))
        return result

    def process_once(self):
        if config.OUTBOUND_ENABLED:
            for _ in range(10):
                if self.stop_event.is_set():
                    break
                with self.store.connect() as db:
                    waiting = [channel for channel in ('telegram', 'max', 'email', 'yclients') if not self._channel_ready(channel)]
                    condition = ' AND channel NOT IN (' + ','.join('?' for _ in waiting) + ')' if waiting else ''
                    row = db.execute('SELECT * FROM message_outbox WHERE state="pending"' + condition + ' ORDER BY created LIMIT 1', waiting).fetchone()
                    if not row:
                        break
                    row = dict(row)
                    payload = self._decrypt(row['payload'])
                    allowed = self._allowed(row, payload)
                    db.execute('UPDATE message_outbox SET state=?,updated=? WHERE id=?',
                               ('sending' if allowed else 'skipped', time.time(), row['id']))
                if not allowed:
                    continue
                if self.stop_event.is_set():
                    with self.store.connect() as db:
                        db.execute('UPDATE message_outbox SET state="pending",updated=? WHERE id=?', (time.time(), row['id']))
                    break
                result = self.sender(row['channel'], payload['target'], payload['text'], {
                    'event': row['event'], 'branch_id': row['branch_id'], 'idempotency_key': row['id'],
                    'welcome': payload.get('welcome'), 'client_token': payload.get('token'),
                    'preference_channel': ('messengers' if row['audience'] == 'client' and row['channel'] in {'chatpush','yclients'} else payload.get('preference_channel'))})
                state = result.get('delivery') or result.get('status', 'unknown')
                with self.store.connect() as db:
                    db.execute('UPDATE message_outbox SET state=?,updated=?,result=? WHERE id=?',
                               (state, time.time(), json.dumps(result), row['id']))
                if state in {'failed', 'unknown', 'error'} and row['event'] != 'delivery_failed':
                    self.event('delivery_failed', row['branch_id'], {
                        'event_id': row['id'], 'message': 'Канал: ' + row['channel'] + '. Проверьте подключение и адрес получателя.'})
        if self.stop_event.is_set():
            return
        for notice in self.billing.due_notices():
            self.event(notice['event_type'], notice['branch_id'], {**notice.get('payload', {}), 'event_id': notice['id']})
            self.billing.acknowledge_notice(notice['id'])
        self.bridge_telephony()
        self.reconcile_hardware()
        if self.support and not self.stop_event.is_set():
            self.support.process_once(limit=5)

    def effect_status(self, channel, idempotency_key):
        effect_id=hashlib.sha256(json.dumps([channel,idempotency_key]).encode()).hexdigest()
        with self.store.connect() as db:
            row=db.execute('SELECT state,result FROM message_effects WHERE id=?',(effect_id,)).fetchone()
        if not row:return None
        result=json.loads(row['result'] or '{}')
        return {'status':row['state'],'delivery':row['state'],'code':result.get('code'),'confirmed':False}

    def _channel_ready(self, channel):
        # Startup/network recovery is not a failed delivery attempt. Leave the
        # durable row pending until its bot is ready, without blocking others.
        if self.sender != self.send:
            return True  # Injected transport owns its own readiness contract.
        if channel in {'telegram', 'max'}:
            module = __import__('telegram_bot' if channel == 'telegram' else 'max_bot')
            return bool(module.status().get('ready'))
        if channel == 'email':
            from smtp_settings import SmtpSettings
            return bool(SmtpSettings().effective().get('enabled'))
        if channel == 'yclients':
            return bool(config.YCLIENTS_ENABLED)
        return True

    def reconcile_hardware(self):
        if self.hardware is None:
            return
        status = self.hardware.status()
        modem = status.get('modem', {}).get('state')
        if modem in {'ok', 'error'} and modem != self._last_modem:
            if modem == 'error' or self._last_modem == 'error':
                self.event('modem_error' if modem == 'error' else 'modem_recovered', None, {})
            self._last_modem = modem
        completed = {r['command_id']: r for r in status.get('recent', [])}
        if not completed:
            return
        with self.store.connect() as db:
            for row in db.execute('SELECT * FROM message_effects WHERE state="queued"').fetchall():
                result = json.loads(row['result'] or '{}')
                command = completed.get(result.get('command_id'))
                if command:
                    result.update(status=command['status'], delivery=command['status'], code=command.get('code'))
                    db.execute('UPDATE message_effects SET state=?,result=? WHERE id=?', (command['status'], json.dumps(result), row['id']))
            for row in db.execute('SELECT id,result FROM message_outbox WHERE state="queued"').fetchall():
                result = json.loads(row['result'] or '{}')
                command = completed.get(result.get('command_id'))
                if command:
                    result.update(status=command['status'], delivery=command['status'], code=command.get('code'))
                    db.execute('UPDATE message_outbox SET state=?,result=?,updated=? WHERE id=?',
                               (command['status'], json.dumps(result), time.time(), row['id']))

    def bridge_telephony(self):
        if self.telephony is None:
            return
        records = self.telephony.store.read().get('records', {})
        with self.store.connect() as db:
            initialized = db.execute('SELECT 1 FROM notification_bridge WHERE id="initialized"').fetchone()
            if not initialized:
                for record in records.values():
                    if record.get('direction') == 'incoming' and (record.get('kind') != 'sms' or record.get('status') == 'received'):
                        db.execute('INSERT OR IGNORE INTO notification_bridge VALUES(?,?)', (record['id'], time.time()))
                db.execute('INSERT INTO notification_bridge VALUES("initialized",?)', (time.time(),))
                return
            for record in records.values():
                if record.get('direction') != 'incoming' or db.execute('SELECT 1 FROM notification_bridge WHERE id=?', (record['id'],)).fetchone():
                    continue
                if record['kind'] == 'sms' and record.get('status') != 'received':
                    continue
                if record['kind'] == 'call' and not record.get('phone'):
                    received = record.get('received_at') or record.get('timestamp')
                    try:
                        if time.time() - datetime.fromisoformat(received).timestamp() < 5:
                            continue
                    except (ValueError, TypeError):
                        pass
                self.event('sms_received' if record['kind'] == 'sms' else 'call_received', None, {
                    'event_id': record['id'], 'phone': record.get('phone') or 'Номер скрыт',
                    'time': record.get('received_at'), 'text': record.get('text')})
                db.execute('INSERT INTO notification_bridge VALUES(?,?)', (record['id'], time.time()))

    def migrate_legacy_outbox(self):
        """Move only unsent legacy notices, with crash-safe per-channel deduplication."""
        import sqlite3
        from pathlib import Path
        path = Path(self.store.path).parent / 'runtime.sqlite3'
        if not path.exists():
            return
        with sqlite3.connect(path, timeout=10) as db:
            db.row_factory = sqlite3.Row
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='outbox'").fetchone():
                return
            db.execute('UPDATE outbox SET state="unknown",error="interrupted_send" WHERE state="sending"')
            db.commit()
            rows = db.execute('SELECT * FROM outbox WHERE state="pending" ORDER BY created').fetchall()
            for row in rows:
                # The new insert commits first. A crash before this marker repeats
                # that insert harmlessly; sent/unknown records are never replayed.
                self.enqueue(json.loads(row['payload']), only_channel=row['channel'])
                db.execute('UPDATE outbox SET state="migrated",updated=? WHERE id=? AND state="pending"',
                           (time.time(), row['id']))
                db.commit()

    def recover(self):
        self.migrate_legacy_outbox()
        with self.store.connect() as db:
            result = json.dumps({'status': 'unknown', 'delivery': 'unknown', 'code': 'runtime_interrupted'})
            db.execute('UPDATE message_outbox SET state="unknown",result=? WHERE state IN ("sending","queued")', (result,))
            db.execute('UPDATE message_effects SET state="unknown",result=? WHERE state IN ("sending","queued")', (result,))

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.recover()
        # Establish the historical baseline synchronously, before run.py starts
        # the sole modem reader. Partial messages stay eligible for completion.
        self.bridge_telephony()
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name='gatebot-product-notifications', daemon=True)
        self.thread.start()
        self.menu_thread = threading.Thread(target=self._run_menus, name='gatebot-menu-refresh', daemon=True)
        self.menu_thread.start()

    def _run_menus(self):
        # A slow email or long forwarded SMS must not hold a 20-second menu
        # cooldown, or leave an expired gate button visible for that delivery.
        while not self.stop_event.is_set():
            for refresh in self._menu_refreshers:
                if self.stop_event.is_set():
                    break
                try:
                    refresh()
                except Exception as error:
                    import logger
                    logger.event('BOT_MENU_REFRESH_ERROR', level='ERROR', details={'error_type': type(error).__name__})
            self.stop_event.wait(1)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.process_once()
                self._last_error = None
            except Exception as error:
                import logger
                self._last_error = type(error).__name__
                logger.event('NOTIFICATION_WORKER_ERROR', level='ERROR', details={'error_type': self._last_error})
            self.stop_event.wait(2)

    def request_stop(self):
        self.stop_event.set()

    def stop(self):
        self.request_stop()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=20)
        if self.menu_thread and self.menu_thread is not threading.current_thread():
            self.menu_thread.join(timeout=20)
        return not any(thread and thread.is_alive() for thread in (self.thread, self.menu_thread))

    def status(self):
        return {'worker_alive': bool(self.thread and self.thread.is_alive()),
                'menu_worker_alive': bool(self.menu_thread and self.menu_thread.is_alive()),
                'outbound_enabled': config.OUTBOUND_ENABLED, 'last_error_type': self._last_error}
