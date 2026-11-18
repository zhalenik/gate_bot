"""Validated branches: legacy CRM company IDs or allocated standalone IDs."""
from copy import deepcopy
import hashlib
import hmac
from pathlib import Path
import re
import secrets
from types import SimpleNamespace
from storage.sql_documents import DocumentStore as JsonStore
from timeutil import timezone


INTERNAL_BRANCH_ID_START = 1_000_000_000_000
MAX_SAFE_BRANCH_ID = 9_007_199_254_740_991


def allocate_internal_branch_id(db, branches):
    """Reserve an immutable numeric ID inside the caller's SQL transaction."""
    db.execute('CREATE TABLE IF NOT EXISTS branch_id_allocator(singleton INTEGER PRIMARY KEY CHECK(singleton=1),next_id INTEGER NOT NULL)')
    db.execute('INSERT OR IGNORE INTO branch_id_allocator VALUES(1,?)',(INTERNAL_BRANCH_ID_START,))
    candidate=db.execute('SELECT next_id FROM branch_id_allocator WHERE singleton=1').fetchone()[0]
    occupied={str(key) for key in branches}
    if db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="branch_registrations"').fetchone():
        occupied.update(str(row['branch_id']) for row in db.execute('SELECT branch_id FROM branch_registrations'))
    while str(candidate) in occupied:
        candidate+=1
    if candidate>MAX_SAFE_BRANCH_ID:
        raise ValueError('Диапазон внутренних идентификаторов исчерпан')
    db.execute('UPDATE branch_id_allocator SET next_id=? WHERE singleton=1',(candidate+1,))
    return candidate


class BranchManager:
    def __init__(self, branches_file=None):
        if branches_file is None:
            from storage import config
            branches_file = Path(config.DATA_DIR) / 'branches.json'
        self.branches_file = str(branches_file)
        self.store = JsonStore(branches_file)

    def load_branches(self):
        branches = self.store.read()
        for key, data in branches.items():
            if str(data.get('branch_id')) != str(key):
                raise ValueError('Branch key and branch_id must match')
        return branches

    def save_branches(self, branches):
        for key, data in branches.items():
            self._validate(data)
            if str(data['branch_id']) != str(key):
                raise ValueError('Branch key and branch_id must match')
        self.store.replace(branches)

    @staticmethod
    def _button_id(button):
        # The identity changes if its target changes; previously sent buttons cannot
        # silently open a different gate after a configuration edit.
        material = str(button.get('type')) + ':' + str(button.get('phone'))
        from storage import config
        return hmac.new(config.SECRET_KEY.encode(), material.encode(), hashlib.sha256).hexdigest()[:12]

    def _validate(self, data):
        if not str(data.get('branch_id', '')).isdigit() or int(data['branch_id']) <= 0:
            raise ValueError('Invalid branch_id')
        timezone(data.get('timezone'))
        if type(data.get('yclients_enabled',True)) is not bool:
            raise ValueError('yclients_enabled must be boolean')
        if data.get('id_origin') == 'internal' and data.get('yclients_enabled',True):
            raise ValueError('A standalone internal ID cannot be used as a YCLIENTS company ID')
        seen = set()
        stable_seen = set()
        for button in data.get('buttons', []):
            if button.get('type') not in {'gate', 'admin'}:
                raise ValueError('Invalid button type')
            if not re.fullmatch(r'\+?[0-9]{3,15}', str(button.get('phone', ''))):
                raise ValueError('Invalid button phone')
            button_id = self._button_id(button)
            if button_id in seen:
                raise ValueError('Duplicate branch button')
            seen.add(button_id)
            stable_id = button.get('stable_id')
            if stable_id is not None:
                if not isinstance(stable_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{12,64}', stable_id) or stable_id in stable_seen:
                    raise ValueError('Invalid or duplicate stable gate identity')
                stable_seen.add(stable_id)
        return data

    def _dict_to_branch_object(self, branch_dict):
        data = deepcopy(self._validate(branch_dict))
        buttons = [SimpleNamespace(id=self._button_id(button), stable_id=button.get('stable_id') or hashlib.sha256((str(data['branch_id'])+':gate:'+str(index)).encode()).hexdigest()[:24], type=button['type'], label=str(button.get('label', '')), phone=button['phone']) for index,button in enumerate(data.get('buttons', []))]
        return SimpleNamespace(branch_id=int(data['branch_id']), name=str(data.get('name', '')), logo=str(data.get('logo', '')), timezone=data.get('timezone', 'Europe/Moscow'), enabled=data.get('enabled', True) is True, yclients_enabled=data.get('yclients_enabled',True), id_origin=data.get('id_origin','yclients'), buttons=buttons)

    def allocate_internal_id(self, db=None):
        if db is not None:
            return allocate_internal_branch_id(db,self.load_branches())
        with self.store.product().connect() as connection:
            return allocate_internal_branch_id(connection,self.load_branches())

    def get_branch(self, branch_id):
        data = self.load_branches().get(str(branch_id))
        return self._dict_to_branch_object(data) if data else None

    def get_all_branches(self):
        return [self._dict_to_branch_object(data) for data in self.load_branches().values()]

    def get_branch_name(self, branch_id):
        branch = self.get_branch(branch_id)
        return branch.name if branch else None

    def get_default_branch(self):
        """Display-only compatibility helper. Never use as an access fallback."""
        branches = self.get_all_branches()
        return branches[0] if branches else None

    def add_branch(self, branch_data):
        self._validate(branch_data)
        key = str(branch_data['branch_id'])
        with self.store.transaction() as branches:
            if key in branches:
                return False
            branches[key] = deepcopy(branch_data)
        return True

    def update_branch(self, branch_id, branch_data):
        self._validate(branch_data)
        if str(branch_data['branch_id']) != str(branch_id):
            raise ValueError('Changing branch identity is forbidden')
        with self.store.transaction() as branches:
            if str(branch_id) not in branches:
                return False
            branches[str(branch_id)] = deepcopy(branch_data)
        return True

    def delete_branch(self, branch_id):
        with self.store.transaction() as branches:
            return branches.pop(str(branch_id), None) is not None

    def update_settings(self, branch_id, settings, create=False):
        allowed = {'name', 'timezone', 'enabled', 'buttons', 'logo', 'yclients_enabled'}
        if set(settings) - allowed:
            raise ValueError('Unsupported branch settings')
        key = str(branch_id)
        if not key.isdigit() or int(key) <= 0:
            raise ValueError('Invalid branch_id')
        with self.store.transaction() as branches:
            if key not in branches and not create:
                return False
            if key in branches and create:
                raise ValueError('Branch already exists')
            value = deepcopy(branches.get(key, {'branch_id': int(key)}))
            previous = value.get('buttons', [])
            value.update(deepcopy(settings))
            if create and value.get('yclients_enabled') is False:
                value['id_origin']='internal'
            # A QR belongs to the physical gate, never to its position in this
            # editable list. Legacy callers can preserve an unchanged route;
            # changing a route requires its explicit previously issued stable ID.
            previous_ids = {button.get('stable_id') or hashlib.sha256((key+':gate:'+str(index)).encode()).hexdigest()[:24]: button
                            for index, button in enumerate(previous)}
            previous_routes = {self._button_id(button): stable for stable, button in previous_ids.items()}
            for button in value.get('buttons', []):
                stable_id = button.get('stable_id')
                if stable_id:
                    if stable_id not in previous_ids or previous_ids[stable_id]['type'] != button.get('type'):
                        raise ValueError('Unknown stable gate identity; reload branch settings')
                else:
                    button['stable_id'] = previous_routes.get(self._button_id(button)) or secrets.token_hex(12)
            self._validate(value)
            branches[key] = value
        return True
