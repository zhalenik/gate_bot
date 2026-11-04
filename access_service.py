"""One authorization path for web, Telegram and MAX. No transport may dial directly."""
from pathlib import Path
import threading
from branch_manager import BranchManager
from link_manager import LinkManager, access_is_active
from runtime_state import RuntimeState
from storage import config
import logger


class AccessService:
    def __init__(self, link_manager=None, branch_manager=None, opener=None, cooldown_seconds=None):
        self.links = link_manager or LinkManager()
        self.branches = branch_manager or BranchManager()
        self.opener = opener
        self._stopping = threading.Event()
        self._admission_lock = threading.RLock()
        self.cooldown_seconds = config.timeout if cooldown_seconds is None else cooldown_seconds
        self.state = RuntimeState(Path(self.links.links_file).parent / 'runtime.sqlite3')

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
            if branch_id in grouped and grouped[branch_id]['active']:
                continue
            buttons = [{'id': button.id, 'type': button.type, 'label': button.label, **({'phone': button.phone} if button.type == 'admin' else {})} for button in branch.buttons if active or button.type == 'admin']
            grouped[branch_id] = {'branch_id': branch_id, 'branch_name': branch.name, 'active': active, 'access_expires': data.get('access_expires'), 'buttons': buttons}
        return list(grouped.values())

    def open_for_identity(self, platform, user_id, branch_id, button_id):
        for token, data in self._identity_records(platform, user_id):
            if str(data.get('branch_id')) == str(branch_id) and access_is_active(data):
                return self._open(token, str(branch_id), button_id, platform + ':' + str(user_id), expected_identity=(platform, str(user_id)))
        logger.event('GATE_DENIED', branch_id=branch_id, actor=platform + ':' + str(user_id), details={'reason': 'no_branch_access'})
        return self._error()

    def open_for_token(self, token, button_id):
        if not isinstance(token, str) or len(token) > 128:
            return self._error()
        data = self.links.verify_token(token)
        if not data:
            logger.event('GATE_DENIED', actor='web', details={'reason': 'invalid_access'})
            return self._error()
        return self._open(token, str(data['branch_id']), button_id, 'web:' + logger.reference(token))

    def _open(self, token, branch_id, button_id, actor, expected_identity=None):
        with self._admission_lock:
            # Global lock order: branch configuration, then access records, then runtime state.
            with self.branches.store.lock:
                branch = self.branches.get_branch(branch_id)
                if branch is None or not branch.enabled:
                    return self._error()
                button = next((b for b in branch.buttons if b.id == button_id and b.type == 'gate'), None)
                if button is None:
                    logger.event('GATE_DENIED', branch_id=branch_id, actor=actor, details={'reason': 'invalid_button'})
                    return self._error()
                # Serialize the final authorization check with grants/revocations. This
                # closes the check/queue race across web and both messenger handlers.
                with self.links.store.transaction() as links:
                    if self._stopping.is_set():
                        return self._error('Сервис завершает работу. Повторите позже.', 503)
                    data = links.get(token)
                    if expected_identity and data:
                        platform, user_id = expected_identity
                        field = {'telegram': 'tg_id', 'max': 'max_id'}.get(platform)
                        if not field or str(data.get(field)) != user_id:
                            return self._error()
                    if not data or str(data.get('branch_id')) != branch_id or not access_is_active(data):
                        return self._error()
                    key = logger.reference(branch_id + ':' + token)
                    claim, remaining = self.state.acquire_cooldown(key, self.cooldown_seconds)
                    if claim is None:
                        return dict(self._error('Команда уже принята. Подождите перед повтором.', 429), retry_after=remaining)
                    try:
                        if self._stopping.is_set():
                            self.state.release_cooldown(key, claim)
                            return self._error('Сервис завершает работу. Повторите позже.', 503)
                        opener = self.opener
                        if opener is None:
                            from sim800c import open_gate
                            opener = open_gate
                        result = opener(button.phone, branch_id=branch.branch_id)
                    except Exception as error:
                        self.state.release_cooldown(key, claim)
                        logger.event('GATE_ERROR', branch_id=branch_id, actor=actor, level='ERROR', details={'error_type': type(error).__name__})
                        return self._error('Ошибка связи с воротами. Повторите попытку позже.', 503)
                    if result.get('status') != 'success':
                        self.state.release_cooldown(key, claim)
                result = {k: v for k, v in result.items() if k in {'status', 'message', 'delivery', 'command_id', 'confirmed', 'retry_after'}}
                result['confirmed'] = False
                result['http_status'] = 200 if result.get('status') == 'success' else 503
                logger.event('GATE_ACCEPTED' if result.get('status') == 'success' else 'GATE_ERROR', branch_id=branch_id, actor=actor, details={'button_id': button_id, 'delivery': result.get('delivery'), 'command_id': result.get('command_id')})
                return result
