"""Installation-wide color overrides; CSS sources remain the default palette."""
import json
import re
import time

import logger
from palette_catalog import current_colors, load_catalog


class PaletteConflict(ValueError):
    def __init__(self, revision, catalog_version):
        super().__init__('Палитра изменилась в другом окне или обновились стили. Загрузите актуальные настройки перед сохранением.')
        self.revision = revision
        self.catalog_version = catalog_version


def color_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?', value):
        raise ValueError('Цвет должен быть в формате #RRGGBB или #RRGGBBAA')
    value = value.upper()
    return value[:7] if len(value) == 9 and value.endswith('FF') else value


class PaletteSettings:
    def __init__(self, identity):
        self.identity, self.store = identity, identity.product
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS ui_palette_settings(id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, overrides TEXT NOT NULL, updated REAL NOT NULL)')

    def _super(self, db, username):
        if self.identity._admin(db, username).get('is_super') is not True:
            raise PermissionError('Цвета интерфейса настраивает только суперадминистратор')

    @staticmethod
    def _read(db):
        row = db.execute('SELECT revision,overrides,updated FROM ui_palette_settings WHERE id=1').fetchone()
        if not row:
            return 0, {}, None
        overrides = json.loads(row['overrides'])
        if not isinstance(overrides, dict):
            raise ValueError('Не удалось прочитать сохранённую палитру')
        # Even data read from storage never becomes executable CSS.
        return row['revision'], {key: color_value(value) for key, value in overrides.items()}, row['updated']

    @staticmethod
    def _view(catalog, revision, overrides, updated):
        entries = []
        current = current_colors(catalog, overrides)
        for item in catalog['entries']:
            entries.append({**item, 'current': current[item['id']],
                            'overridden': item['id'] in overrides})
        known = {item['id'] for item in entries}
        return {'catalog_version': catalog['catalog_version'], 'revision': revision,
                'updated_at': updated, 'entries': entries,
                'overrides': {key: value for key, value in overrides.items() if key in known},
                'unused_overrides': len(set(overrides) - known)}

    def public(self, username):
        with self.store.connect() as db:
            self._super(db, username)
            return self._view(load_catalog(), *self._read(db))

    def _write(self, username, payload, *, reset):
        with self.store.connect() as db:
            self._super(db, username)
        if not isinstance(payload, dict) or set(payload) != ({'revision', 'catalog_version'} if reset else {'revision', 'catalog_version', 'overrides'}):
            raise ValueError('Некорректный формат настроек палитры')
        if type(payload['revision']) is not int or payload['revision'] < 0 or not isinstance(payload['catalog_version'], str):
            raise ValueError('Некорректная версия палитры')
        catalog = load_catalog()
        editable = {item['id']: item for item in catalog['entries'] if item.get('editable', True)}
        changes = {} if reset else payload['overrides']
        if not isinstance(changes, dict) or len(changes) > len(editable):
            raise ValueError('Некорректный список цветов')
        if any(key not in editable for key in changes):
            raise ValueError('Неизвестный цвет. Обновите каталог интерфейса')
        changes = {key: color_value(value) for key, value in changes.items()}
        with self.store.connect() as db:
            self._super(db, username)  # Check current role inside the mutation transaction.
            revision, previous, updated = self._read(db)
            if payload['revision'] != revision or payload['catalog_version'] != catalog['catalog_version']:
                raise PaletteConflict(revision, catalog['catalog_version'])
            # Removed CSS entries stay recoverable across upgrades. A deliberate
            # full reset is the only operation that also clears these old keys.
            values = {} if reset else {key: value for key, value in previous.items() if key not in editable}
            values.update(changes)
            if values != previous:
                revision += 1
                updated = time.time()
                db.execute('INSERT INTO ui_palette_settings VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,overrides=excluded.overrides,updated=excluded.updated',
                           (revision, json.dumps(values, sort_keys=True), updated))
                logger.event('UI_PALETTE_RESET' if reset else 'UI_PALETTE_SAVED',
                             details={'actor_ref': logger.reference(username), 'revision': revision, 'colors': len(changes)})
            return self._view(catalog, revision, values, updated)

    def save(self, username, payload):
        return self._write(username, payload, reset=False)

    def reset(self, username, payload):
        return self._write(username, payload, reset=True)

    def stylesheet(self):
        """Public CSS exposes color values only, without private account metadata."""
        catalog = load_catalog()
        with self.store.connect() as db:
            _, overrides, _ = self._read(db)
        lines = []
        for entry in catalog['entries']:
            if entry['id'] not in overrides:
                continue
            variable = entry['css_var']
            if not re.fullmatch(r'--gb-color-[a-z0-9_-]{1,100}', variable):
                raise ValueError('Некорректный идентификатор цвета в каталоге')
            lines.append('  ' + variable + ': ' + overrides[entry['id']] + ';')
        return '/* GateBot saved interface palette */\n:root {\n' + '\n'.join(lines) + '\n}\n'
