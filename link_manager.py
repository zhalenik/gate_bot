"""Branch-scoped access records and verified messenger bindings.

Public methods perform complete read/modify/write transactions. JSON is kept for
legacy compatibility; a runtime process lock prevents multiple hardware owners.
"""
from copy import deepcopy
from datetime import timedelta
import secrets
from pathlib import Path
from storage.json_store import JsonStore
from timeutil import now, end_of_day, parse_datetime

_notify_hook = None


def normalize_phone(raw):
    if not raw:
        return ''
    digits = ''.join(ch for ch in str(raw) if ch in '0123456789')
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    elif len(digits) == 10 and digits.startswith('9'):
        digits = '7' + digits
    return '+' + digits if 7 <= len(digits) <= 15 else ''


def phones_equal(a, b):
    return bool(normalize_phone(a)) and normalize_phone(a) == normalize_phone(b)


def set_notify_hook(func):
    global _notify_hook
    _notify_hook = func


def _emit_event(payload):
    if _notify_hook:
        _notify_hook(payload)


def access_is_active(data, at=None):
    if data.get('admin_blocked') or data.get('gate_access') is not True:
        return False
    if not data.get('access_expires'):
        return True
    try:
        return (at or now()) < parse_datetime(data['access_expires'])
    except (TypeError, ValueError):
        return False


class LinkManager:
    def __init__(self, links_file=None):
        if links_file is None:
            from storage import config
            links_file = Path(config.DATA_DIR) / 'links.json'
        self.links_file = str(links_file)
        self.store = JsonStore(links_file)

    def load_links(self):
        return self.store.read()

    def save_links(self, links):
        """Whole-state import only; normal mutations must use transactional methods."""
        self.store.replace(links)

    def generate_token(self):
        return secrets.token_urlsafe(16)

    def create_link(self, user_data, expires_days=None, expires_at=None):
        branch = user_data.get('branch_id')
        if branch is None or not str(branch).isdigit() or int(branch) <= 0:
            raise ValueError('A valid branch_id is required')
        if expires_days is not None and (isinstance(expires_days, bool) or not 0 <= int(expires_days) <= 36500):
            raise ValueError('Invalid access duration')
        expiry = parse_datetime(expires_at) if expires_at else (now() + timedelta(days=int(expires_days)) if expires_days else None)
        with self.store.transaction() as links:
            token = self.generate_token()
            while token in links:
                token = self.generate_token()
            links[token] = {
                'id_ycl': user_data.get('id_ycl'),
                'user_name': str(user_data.get('user_name') or ''),
                'phone': normalize_phone(user_data.get('phone')),
                'email': str(user_data.get('email') or ''),
                'tg_id': None, 'max_id': None,
                'mailing_enabled': user_data.get('mailing_enabled', False) is True,
                'branch_id': int(branch),
                'gate_access': user_data.get('gate_access', True) is True,
                'admin_blocked': user_data.get('admin_blocked', False) is True,
                'access_source': user_data.get('access_source', 'manual'),
                'is_temporary': user_data.get('is_temporary', False) is True,
                'access_expires': expiry.isoformat() if expiry else None,
                'created': now().isoformat(),
            }
        return token

    def verify_token(self, token):
        data = self.load_links().get(token)
        return data if data and access_is_active(data) else None

    def get_all_links(self):
        return [dict(data, token=token, token_preview=token[:5] + '…') for token, data in self.load_links().items()]

    def get_active_links(self):
        return [data for data in self.get_all_links() if access_is_active(data)]

    def get_users_by_phone(self, phone):
        normalized = normalize_phone(phone)
        if not normalized:
            return []
        return [(token, data) for token, data in self.load_links().items() if normalize_phone(data.get('phone')) == normalized]

    def bind_identity(self, platform, user_id, token=None, phone=None):
        field = {'telegram': 'tg_id', 'max': 'max_id'}.get(platform)
        if not field or not str(user_id).isdigit() or int(user_id) <= 0:
            raise ValueError('Invalid messenger identity')
        normalized = normalize_phone(phone) if phone else None
        with self.store.transaction() as links:
            candidates = [(key, data) for key, data in links.items() if (key == token if token else normalized and normalize_phone(data.get('phone')) == normalized)]
            if not candidates:
                return 0
            if any(data.get(field) is not None and str(data[field]) != str(user_id) for _, data in candidates):
                raise ValueError('Account is already linked; contact the administrator')
            for _, data in candidates:
                data[field] = str(user_id)
            return len(candidates)

    def bind_tg_id_for_phone(self, phone, tg_id):
        """Internal compatibility API; caller must verify contact ownership first."""
        return self.bind_identity('telegram', tg_id, phone=phone)

    def _change(self, token, change, notify=False):
        with self.store.transaction() as links:
            data = links.get(token)
            if data is None:
                return False
            before = deepcopy(data)
            change(data)
            changed = before != data
            if notify and changed:
                data['access_revision'] = int(data.get('access_revision', 0)) + 1
            payload = dict(data, token=token, event='granted' if access_is_active(data) else 'revoked')
        if notify and changed:
            _emit_event(payload)
        return True

    def revoke_link(self, token):
        return self.toggle_user_access(token, False)

    def toggle_user_access(self, token, enable=None):
        if enable is not None and not isinstance(enable, bool):
            raise ValueError('enable must be boolean')
        def change(data):
            enabled = not access_is_active(data) if enable is None else enable
            data['gate_access'] = enabled
            data['admin_blocked'] = not enabled
            data['access_source'] = 'manual'
        return self._change(token, change, notify=True)

    def set_expiry_at(self, token, expiry):
        value = parse_datetime(expiry).isoformat() if expiry else None
        def change(data):
            data.update(access_expires=value, gate_access=True, admin_blocked=False, access_source='manual')
        return self._change(token, change, notify=True)

    def set_access_expires(self, token, days):
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or days < 0 or days > 36500):
            raise ValueError('days must be a non-negative integer')
        return self.set_expiry_at(token, now() + timedelta(days=days) if days else None)

    def set_access_expires_to_end_of_day(self, token):
        return self.set_expiry_at(token, end_of_day())

    def has_access_until_end_of_day(self, token):
        data = self.verify_token(token)
        if not data:
            return False
        return not data.get('access_expires') or parse_datetime(data['access_expires']) >= end_of_day()

    def set_mailing_enabled(self, token, enabled):
        if not isinstance(enabled, bool):
            raise ValueError('mailing_enabled must be boolean')
        return self._change(token, lambda data: data.update(mailing_enabled=enabled))

    def set_user_phone(self, token, phone):
        return self.update_user(token, {'phone': phone})

    def update_user(self, token, updates):
        allowed = {'id_ycl', 'user_name', 'phone', 'email'}
        if set(updates) - allowed:
            raise ValueError('Unsupported user fields')
        def change(data):
            for key, value in updates.items():
                if key == 'phone':
                    value = normalize_phone(value)
                    if data.get('phone') != value:
                        data['tg_id'] = data['max_id'] = None
                data[key] = value
        return self._change(token, change)

    def delete_link(self, token):
        with self.store.transaction() as links:
            return links.pop(token, None) is not None

    def get_all_users_by_id_ycl(self, id_ycl, branch_id):
        if id_ycl is None or branch_id is None:
            return []
        return [(token, data) for token, data in self.load_links().items() if str(data.get('branch_id')) == str(branch_id) and str(data.get('id_ycl')) == str(id_ycl)]

    def get_user_by_id_ycl(self, id_ycl, branch_id):
        matches = self.get_all_users_by_id_ycl(id_ycl, branch_id)
        if len(matches) > 1:
            raise ValueError('Duplicate CRM identity in this branch; manual reconciliation required')
        return matches[0] if matches else None

    def cleanup_expired_accesses(self):
        events = []
        with self.store.transaction() as links:
            for token, data in links.items():
                if data.get('gate_access') and not access_is_active(data):
                    data['gate_access'] = False
                    data['access_revision'] = int(data.get('access_revision', 0)) + 1
                    events.append(dict(data, token=token, event='revoked'))
        for event in events:
            _emit_event(event)
        return len(events)

    def cleanup_guest_users(self):
        # Legacy records named 'Гость' may be staff or permanent: never delete by name.
        with self.store.transaction() as links:
            expired = []
            for token, data in links.items():
                if not data.get('is_temporary') or not data.get('access_expires') or access_is_active(data):
                    continue
                try:
                    if parse_datetime(data['access_expires']) <= now():
                        expired.append(token)
                except (TypeError, ValueError):
                    # Keep malformed records for manual repair; never delete or
                    # stop synchronization of unrelated branches because of them.
                    continue
            for token in expired:
                del links[token]
            return len(expired)

    def reconcile_appointments(self, branch_id, eligible_clients, at=None, branch_timezone=None):
        """Apply only a complete, successful CRM snapshot. Manual grants are preserved."""
        at = at or now(branch_timezone)
        expiry = end_of_day(at, branch_timezone).isoformat()
        eligible = {str(key): value for key, value in eligible_clients.items()}
        events = []
        with self.store.transaction() as links:
            by_id = {}
            for token, data in links.items():
                if str(data.get('branch_id')) == str(branch_id) and data.get('id_ycl') is not None:
                    key = str(data['id_ycl'])
                    if key in by_id:
                        raise ValueError('Duplicate branch/client identity; snapshot not applied')
                    by_id[key] = (token, data)
            for key, client in eligible.items():
                if key not in by_id:
                    token = self.generate_token()
                    while token in links:
                        token = self.generate_token()
                    data = {'id_ycl': key, 'branch_id': int(branch_id), 'user_name': client.get('name', ''), 'phone': normalize_phone(client.get('phone')), 'email': client.get('email', ''), 'tg_id': None, 'max_id': None, 'mailing_enabled': True, 'gate_access': False, 'admin_blocked': False, 'access_source': 'appointment', 'access_expires': None, 'created': at.isoformat(), 'is_temporary': False}
                    links[token] = data
                    by_id[key] = (token, data)
                token, data = by_id[key]
                # CRM owns its contact fields. Omitted fields are not blank values.
                if 'phone' in client:
                    phone = normalize_phone(client['phone'])
                    if normalize_phone(data.get('phone')) != phone:
                        data['tg_id'] = data['max_id'] = None
                    data['phone'] = phone
                for source, target in [('name', 'user_name'), ('email', 'email')]:
                    if source in client:
                        data[target] = client[source] or ''
                if data.get('admin_blocked'):
                    continue
                if data.get('access_source', 'manual') == 'manual' and access_is_active(data, at):
                    continue
                changed = not access_is_active(data, at) or data.get('access_expires') != expiry
                data.update(gate_access=True, access_source='appointment', access_expires=expiry)
                if changed:
                    data['access_revision'] = int(data.get('access_revision', 0)) + 1
                    events.append(dict(data, token=token, event='granted'))
            for key, (token, data) in by_id.items():
                if key not in eligible and data.get('access_source') == 'appointment' and data.get('gate_access'):
                    data['gate_access'] = False
                    data['access_revision'] = int(data.get('access_revision', 0)) + 1
                    events.append(dict(data, token=token, event='revoked'))
        for event in events:
            _emit_event(event)
        return {'eligible': len(eligible), 'changed': len(events)}
