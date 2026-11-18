"""One authorization path for web, Telegram and MAX. No transport may dial directly."""
from pathlib import Path
import threading
import time
import uuid
from branch_manager import BranchManager
from link_manager import LinkManager, access_is_active, normalize_phone
from runtime_state import RuntimeState
from storage import config
import logger
from branch_hours import access_hours_state, CLOSED_MESSAGE


class AccessService:
    def __init__(self, link_manager=None, branch_manager=None, opener=None, cooldown_seconds=None,
                 billing=None, identity=None, event_sink=None):
        self.links = link_manager or LinkManager()
        self.branches = branch_manager or BranchManager()
        self.opener = opener
        self._stopping = threading.Event()
        self._admission_lock = threading.RLock()
        self.cooldown_seconds = config.timeout if cooldown_seconds is None else cooldown_seconds
        self.state = RuntimeState(Path(self.links.links_file).parent / 'runtime.sqlite3')
        self.billing, self.identity, self.event_sink = billing, identity, event_sink
        if billing is not None:
            with billing.store.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS access_command_routes(command_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, button_id TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS access_request_auth(request_id TEXT PRIMARY KEY, token TEXT NOT NULL, platform TEXT, user_id TEXT)')

    def stop_accepting(self):
        self._stopping.set()
        # Wait until any already admitted queue submission has completed.
        with self._admission_lock:
            pass

    @staticmethod
    def _error(message='Нет действующего пропуска. Обратитесь к администратору.', code=403):
        return {'status': 'error', 'message': message, 'http_status': code, 'confirmed': False}

    def bind_token(self, platform, user_id, token):
        if not isinstance(token, str) or len(token) > 128:
            return self._error('Ссылка недействительна.')
        if self.identity is not None and token.startswith('adm_'):
            try:
                return self.identity.bind_ticket(platform, user_id, token)
            except (ValueError, PermissionError) as error:
                return self._error(str(error))
        try:
            count = self.links.bind_identity(platform, user_id, token=token)
        except ValueError:
            return self._error('Не удалось привязать аккаунт. Обратитесь к администратору.')
        if not count:
            return self._error('Ссылка недействительна.')
        logger.event('IDENTITY_BOUND', actor=platform + ':' + str(user_id), details={'platform': platform, 'count': count})
        return {'status': 'success', 'message': 'Аккаунт привязан.', 'count': count}

    def bind_verified_phone(self, platform, user_id, phone):
        """Only adapters that verified a platform-signed own contact may call this."""
        try:
            count = self.links.bind_identity(platform, user_id, phone=phone)
        except ValueError:
            return self._error('Не удалось привязать аккаунт. Обратитесь к администратору.')
        if not count:
            return self._error('Клиент с этим номером пока не найден. Используйте персональную ссылку или обратитесь к администратору.')
        logger.event('IDENTITY_BOUND', actor=platform + ':' + str(user_id), details={'platform': platform, 'count': count})
        return {'status': 'success', 'message': 'Номер подтверждён, аккаунт привязан.', 'count': count}

    def _identity_records(self, platform, user_id):
        field = {'telegram': 'tg_id', 'max': 'max_id'}.get(platform)
        if not field or not str(user_id).isdigit() or int(user_id) <= 0:
            return []
        return [(token, data) for token, data in self.links.load_links().items() if data.get(field) is not None and str(data[field]) == str(user_id)]

    def menu_for_identity(self, platform, user_id):
        grouped = {}
        for _, data in self._identity_records(platform, user_id):
            branch_id = str(data.get('branch_id'))
            branch = self.branches.get_branch(branch_id)
            if not branch or not branch.enabled:
                continue
            active = access_is_active(data)
            if self.billing is not None:
                active = active and self.billing.subscription_view(branch_id)['available']
            if branch_id in grouped and grouped[branch_id]['active']:
                continue
            buttons = [{'id': button.id, 'type': button.type, 'label': button.label, **({'phone': button.phone} if button.type == 'admin' else {})} for button in branch.buttons if active or button.type == 'admin']
            remaining = self.cooldown_remaining(data)
            grouped[branch_id] = {'branch_id': branch_id, 'branch_name': branch.name, 'active': active,
                                  'retry_after': remaining, 'access_expires': None if data.get('admin_role_active') else data.get('access_expires'), 'buttons': buttons,
                                  'access_hours':access_hours_state(self.branches,branch_id)}
        return list(grouped.values())

    def open_for_identity(self, platform, user_id, branch_id, button_id):
        records = self._identity_records(platform, user_id)
        if any(str(data.get('branch_id'))==str(branch_id) for _,data in records) and not access_hours_state(self.branches,branch_id)['open']:
            return dict(self._error(CLOSED_MESSAGE),code='outside_working_hours')
        for token, data in records:
            if str(data.get('branch_id')) == str(branch_id) and access_is_active(data):
                return self._open(token, str(branch_id), button_id, platform + ':' + str(user_id), expected_identity=(platform, str(user_id)))
        known = next(((token, data) for token, data in records if str(data.get('branch_id')) == str(branch_id)), None)
        if known:
            self._record_denied(known[1], known[0], button_id, platform, 'pass_inactive')
        logger.event('GATE_DENIED', branch_id=branch_id, actor=platform + ':' + str(user_id), details={'reason': 'no_branch_access'})
        return self._error()

    def open_for_token(self, token, button_id, request_id=None, *, expected_phone=None, expected_gate_id=None):
        if not isinstance(token, str) or len(token) > 128:
            return self._error()
        known=self.links.load_links().get(token)
        if known and not access_hours_state(self.branches,known.get('branch_id'))['open']:
            return dict(self._error(CLOSED_MESSAGE),code='outside_working_hours')
        data = self.links.verify_token(token)
        if not data:
            known = self.links.load_links().get(token)
            if known:
                self._record_denied(known, token, button_id, 'web', 'pass_inactive')
            logger.event('GATE_DENIED', actor='web', details={'reason': 'invalid_access'})
            return self._error()
        return self._open(token, str(data['branch_id']), button_id, 'web:' + logger.reference(token), request_id=request_id,
                          expected_phone=expected_phone, expected_gate_id=expected_gate_id)

    @staticmethod
    def person_key(data):
        return data.get('person_id') or logger.reference('person:' + (normalize_phone(data.get('phone')) or str(data.get('id_ycl') or data.get('created'))))

    @staticmethod
    def pass_type(data):
        if data.get('admin_role_active') or data.get('access_source') == 'admin_role':
            return 'admin_role'
        if data.get('is_temporary'):
            return 'guest'
        if data.get('access_source') == 'appointment':
            return 'appointment'
        return 'manual_timed' if data.get('access_expires') else 'permanent'

    def _record_denied(self, data, token, button_id, source, reason):
        if self.billing is None or not data:
            return
        branch = self.branches.get_branch(data.get('branch_id'))
        button = next((b for b in branch.buttons if b.id == button_id and b.type == 'gate'), None) if branch else None
        if not button:
            return
        self.billing.record_denied(uuid.uuid4().hex, str(data['branch_id']), self.person_key(data),
            getattr(button, 'stable_id', None) or button.id, reason, source.split(':', 1)[0],
            branch_client_id=token, pass_type=self.pass_type(data))
        self._emit('gate_denied', data['branch_id'], client_name=data.get('user_name'), gate_label=button.label,
                   message='Нет действующего разрешения')

    def cooldown_remaining(self, data):
        if self.billing is not None:
            return self.billing.cooldown_remaining(str(data['branch_id']), self.person_key(data))
        return self.state.cooldown_remaining(logger.reference(str(data['branch_id']) + ':' + self.person_key(data)))

    def _emit(self, event, branch_id, **payload):
        if self.event_sink:
            try:
                self.event_sink(event, branch_id, payload)
            except Exception:
                logger.event('NOTIFICATION_ENQUEUE_ERROR', branch_id=branch_id, level='ERROR')

    def authorize_command(self, command_id, *, start=False):
        if self.billing is None or self._stopping.is_set():
            return False
        with self.branches.store.lock, self.links.store.transaction() as links:
            with self.billing.store.connect() as db:
                route = db.execute('SELECT * FROM access_command_routes WHERE command_id=?', (command_id,)).fetchone()
                admission = self.billing.authorize_command(command_id, db=db)
                if not route or not admission['authorized']:
                    return False
                branch = self.branches.get_branch(route['branch_id'])
                button = next((b for b in branch.buttons if b.id == route['button_id'] and b.type == 'gate'), None) if branch else None
                if not branch or not branch.enabled or not button:
                    self.billing.finalize(command_id, 'cancelled', 'route_changed', db=db)
                    return False
                if not access_hours_state(self.branches,route['branch_id'])['open']:
                    self.billing.finalize(command_id,'cancelled','outside_working_hours',db=db)
                    return False
                for record in admission['requests']:
                    auth = db.execute('SELECT * FROM access_request_auth WHERE request_id=?', (record['id'],)).fetchone()
                    data = links.get(auth['token']) if auth else None
                    allowed = bool(data and str(data.get('branch_id')) == str(branch.branch_id) and access_is_active(data))
                    if allowed and auth['platform']:
                        field = {'telegram': 'tg_id', 'max': 'max_id'}.get(auth['platform'])
                        allowed = bool(field and str(data.get(field)) == auth['user_id'])
                    if not allowed:
                        self.billing.cancel_request(record['id'], 'access_revoked', db=db)
                allowed = self.billing.authorize_command(command_id, db=db)['authorized']
                if allowed and start:
                    self.billing.finalize(command_id, 'started', db=db)
                return allowed

    def command_finished(self, command_id, status, code=None):
        if self.billing is not None and status not in {'queued', 'processing'}:
            self.billing.finalize(command_id, status, code)

    def _open(self, token, branch_id, button_id, actor, expected_identity=None, request_id=None,
              expected_phone=None, expected_gate_id=None):
        with self._admission_lock:
            # Global lock order: branch configuration, then access records, then runtime state.
            with self.branches.store.lock:
                branch = self.branches.get_branch(branch_id)
                if branch is None or not branch.enabled:
                    self._record_denied(self.links.load_links().get(token), token, button_id, actor, 'branch_disabled')
                    return self._error()
                button = next((b for b in branch.buttons if b.id == button_id and b.type == 'gate'), None)
                if button is None:
                    logger.event('GATE_DENIED', branch_id=branch_id, actor=actor, details={'reason': 'invalid_button'})
                    return self._error()
                if expected_gate_id is not None and button.stable_id != expected_gate_id:
                    return self._error('Настройки ворот изменились. Обновите страницу.')
                if not access_hours_state(self.branches,branch_id)['open']:
                    self._record_denied(self.links.load_links().get(token),token,button_id,actor,'outside_working_hours')
                    return dict(self._error(CLOSED_MESSAGE),code='outside_working_hours')
                # Serialize the final authorization check with grants/revocations. This
                # closes the check/queue race across web and both messenger handlers.
                with self.links.store.transaction() as links:
                    if self._stopping.is_set():
                        return self._error('Сервис завершает работу. Повторите позже.', 503)
                    data = links.get(token)
                    if expected_phone is not None and (not data or normalize_phone(data.get('phone')) != expected_phone):
                        return self._error('Номер клиента изменился. Подтвердите телефон заново.')
                    if expected_identity and data:
                        platform, user_id = expected_identity
                        field = {'telegram': 'tg_id', 'max': 'max_id'}.get(platform)
                        if not field or str(data.get(field)) != user_id:
                            return self._error()
                    if not data or str(data.get('branch_id')) != branch_id or not access_is_active(data):
                        self._record_denied(data, token, button_id, actor, 'pass_inactive')
                        return self._error()
                    key = logger.reference(branch_id + ':' + self.person_key(data))
                    admission = None
                    if self.billing is not None:
                        request_id = request_id or uuid.uuid4().hex
                        # The SQL document transaction and billing share one connection.
                        with self.billing.store.connect() as db:
                            stable_gate_id = getattr(button, 'stable_id', None) or button.id
                            # A preserved gate identity may now route to another
                            # number. Do not merge a fresh request into a stale
                            # command that the hardware guard will rightly reject.
                            stale = db.execute('SELECT h.id FROM hardware_commands h JOIN access_command_routes r ON r.command_id=h.id WHERE h.branch_id=? AND h.gate_id=? AND h.status=? AND r.button_id<>?',
                                (branch_id, stable_gate_id, 'queued', button.id)).fetchall()
                            for command in stale:
                                self.billing.finalize(command['id'], 'cancelled', 'route_changed', db=db)
                            admission = self.billing.admit(request_id, branch_id, self.person_key(data),
                                stable_gate_id, actor.split(':', 1)[0],
                                branch_client_id=data.get('branch_client_id') or token, pass_type=self.pass_type(data),
                                cooldown_seconds=self.cooldown_seconds, ttl_seconds=config.call_queue_timeout,
                                simulated=config.GATE_DRY_RUN, db=db)
                            if admission['admitted'] and not admission.get('duplicate'):
                                db.execute('INSERT OR IGNORE INTO access_command_routes VALUES(?,?,?)', (admission['command_id'], branch_id, button.id))
                                platform, user_id = expected_identity or (None, None)
                                db.execute('INSERT INTO access_request_auth VALUES(?,?,?,?)', (request_id, token, platform, user_id))
                        if not admission['admitted']:
                            if admission['reason'] == 'cooldown':
                                return dict(self._error('Команда уже принята. Подождите перед повтором.', 429), retry_after=admission['retry_after'])
                            return self._error('Открытие временно недоступно. Обратитесь к администратору.', 403)
                        if admission.get('duplicate') or admission.get('coalesced'):
                            return {'status': 'success', 'delivery': admission['delivery'], 'command_id': admission['command_id'],
                                    'confirmed': False, 'http_status': 200, 'retry_after': self.cooldown_seconds,
                                    'message': 'Запрос уже принят'}
                        claim = None
                    else:
                        claim, remaining = self.state.acquire_cooldown(key, self.cooldown_seconds)
                        if claim is None:
                            return dict(self._error('Команда уже принята. Подождите перед повтором.', 429), retry_after=remaining)
                # Commit domain admission before taking the hardware queue lock.
                try:
                    if self._stopping.is_set():
                        self.state.release_cooldown(key, claim)
                        if admission:
                            self.billing.finalize(admission['command_id'], 'cancelled', 'stopping')
                        return self._error('Сервис завершает работу. Повторите позже.', 503)
                    opener = self.opener
                    if opener is None:
                        from sim800c import open_gate
                        opener = open_gate
                    kwargs = {'branch_id': branch.branch_id}
                    if admission and self.opener is None:
                        kwargs.update(command_id=admission['command_id'], gate_id=getattr(button, 'stable_id', None) or button.id)
                    result = opener(button.phone, **kwargs)
                except Exception as error:
                    self.state.release_cooldown(key, claim)
                    if admission:
                        self.billing.finalize(admission['command_id'], 'unknown', 'submission_interrupted')
                    logger.event('GATE_ERROR', branch_id=branch_id, actor=actor, level='ERROR', details={'error_type': type(error).__name__})
                    return self._error('Ошибка связи с воротами. Повторите попытку позже.', 503)
                if result.get('status') != 'success':
                    self.state.release_cooldown(key, claim)
                    if admission:
                        self.billing.finalize(admission['command_id'], 'failed', result.get('code', 'queue_rejected'))
                elif admission and self.opener is not None and result.get('delivery', 'command_sent') != 'queued':
                    self.billing.finalize(admission['command_id'], result.get('delivery', 'command_sent'))
                result = {k: v for k, v in result.items() if k in {'status', 'message', 'delivery', 'command_id', 'confirmed', 'retry_after'}}
                result['confirmed'] = False
                result['http_status'] = 200 if result.get('status') == 'success' else 503
                if result.get('status') == 'success':
                    result['retry_after'] = self.cooldown_seconds
                self._emit('gate_requested' if result.get('status') == 'success' else 'gate_error', branch_id,
                           client_name=data.get('user_name'), gate_label=button.label,
                           delivery=result.get('delivery'), event_id=result.get('command_id'))
                logger.event('GATE_ACCEPTED' if result.get('status') == 'success' else 'GATE_ERROR', branch_id=branch_id, actor=actor, details={'button_id': button_id, 'delivery': result.get('delivery'), 'command_id': result.get('command_id')})
                return result
