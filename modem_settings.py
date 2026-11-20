"""Installation-owned SIM800C inventory, persisted with the private application DB."""
import json
import math
import os
import re

from storage import config
from storage.product_store import ProductStore


def default_modem():
    return {'id': 'legacy', 'name': 'SIM800C', 'port': config.SIM800_PORT, 'host_device': '',
            'baudrate': config.SIM800_BAUDRATE, 'command_timeout': config.SIM800_COMMAND_TIMEOUT,
            'call_enabled': True, 'sms_enabled': True, 'call_priority': 1, 'sms_priority': 1}


def _inventory_table(db):
    db.execute('CREATE TABLE IF NOT EXISTS sim800_inventory (id INTEGER PRIMARY KEY CHECK (id=1), revision INTEGER NOT NULL, payload TEXT NOT NULL)')


def inventory(store=None):
    with (store or ProductStore()).connect() as db:
        _inventory_table(db)
        row = db.execute('SELECT revision,payload FROM sim800_inventory WHERE id=1').fetchone()
        return {'revision': row['revision'], 'modems': json.loads(row['payload'])} if row else {'revision': 0, 'modems': [default_modem()]}


def read_modems(store=None):
    return inventory(store)['modems']


def _device(value, allow_windows):
    if not isinstance(value, str) or len(value) > 256 or value.rsplit('/', 1)[-1] in {'.', '..'}:
        return False
    return bool((allow_windows and re.fullmatch(r'COM[1-9][0-9]*', value, re.I))
                or re.fullmatch(r'/dev/(?:tty(?:USB|ACM|AMA|S)[0-9]+|serial/by-(?:id|path)/[\w.:+-]+|gatebot-[\w-]+)', value))


def save_modems(store, revision, rows):
    if type(revision) is not int or not isinstance(rows, list):
        raise ValueError('Обновите страницу с настройками модемов')
    ids, ports, hosts = set(), set(), set()
    for item in rows:
        if not isinstance(item, dict) or set(item) != set(default_modem()):
            raise ValueError('Заполните настройки каждого модема')
        if not isinstance(item['id'], str) or not re.fullmatch(r'(?:legacy|[a-f0-9]{32})', item['id']) or item['id'] in ids:
            raise ValueError('Некорректный идентификатор модема')
        if not isinstance(item['name'], str) or not 1 <= len(item['name'].strip()) <= 64 or not item['name'].isprintable():
            raise ValueError('Введите название модема (до 64 символов)')
        item['name'] = item['name'].strip()
        if not _device(item['port'], True) or (item['host_device'] != '' and not _device(item['host_device'], False)):
            raise ValueError('Укажите COM-порт или путь к последовательному устройству в /dev')
        port = os.path.normcase(os.path.realpath(item['port'])) if os.name != 'nt' else item['port'].casefold()
        host = item['host_device'] or item['port']
        if port in ports or host in hosts:
            raise ValueError('Каждому модему нужен отдельный порт')
        ports.add(port); hosts.add(host); ids.add(item['id'])
        if type(item['baudrate']) is not int or not 1 <= item['baudrate'] <= 921600:
            raise ValueError('Проверьте скорость порта')
        if type(item['command_timeout']) not in (int, float) or not math.isfinite(item['command_timeout']) or not 0.1 <= item['command_timeout'] <= 300:
            raise ValueError('Таймаут команды должен быть от 0,1 до 300 секунд')
        for kind in ('call', 'sms'):
            if type(item[kind+'_enabled']) is not bool or type(item[kind+'_priority']) is not int or not 0 <= item[kind+'_priority'] <= 1000:
                raise ValueError('Приоритет звонков и SMS должен быть от 0 до 1000')
    with store.connect() as db:
        _inventory_table(db)
        current = db.execute('SELECT revision FROM sim800_inventory WHERE id=1').fetchone()
        if (current['revision'] if current else 0) != revision:
            raise ValueError('Список модемов изменён в другом окне. Обновите страницу')
        db.execute('INSERT INTO sim800_inventory VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload',
                   (revision + 1, json.dumps(rows, ensure_ascii=False)))
    return {'revision': revision + 1, 'modems': rows}
