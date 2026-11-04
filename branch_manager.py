"""Validated branch configuration. Branch IDs are YCLIENTS company IDs."""
from copy import deepcopy
import hashlib
import hmac
from pathlib import Path
import re
from types import SimpleNamespace
from storage.json_store import JsonStore
from timeutil import timezone


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
        seen = set()
        for button in data.get('buttons', []):
            if button.get('type') not in {'gate', 'admin'}:
                raise ValueError('Invalid button type')
            if not re.fullmatch(r'\+?[0-9]{3,15}', str(button.get('phone', ''))):
                raise ValueError('Invalid button phone')
            button_id = self._button_id(button)
            if button_id in seen:
                raise ValueError('Duplicate branch button')
            seen.add(button_id)
        return data

    def _dict_to_branch_object(self, branch_dict):
        data = deepcopy(self._validate(branch_dict))
        buttons = [SimpleNamespace(id=self._button_id(button), type=button['type'], label=str(button.get('label', '')), phone=button['phone']) for button in data.get('buttons', [])]
        return SimpleNamespace(branch_id=int(data['branch_id']), name=str(data.get('name', '')), logo=str(data.get('logo', '')), timezone=data.get('timezone', 'Europe/Moscow'), enabled=data.get('enabled', True) is True, buttons=buttons)

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
        allowed = {'name', 'timezone', 'enabled', 'buttons', 'logo'}
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
            value.update(deepcopy(settings))
            self._validate(value)
            branches[key] = value
        return True
