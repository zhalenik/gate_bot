"""Versioned commercial catalog. Files are uploaded by authenticated web handlers."""
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import re
import time
import uuid


DEFAULT_PLANS = [
    {'id': 'trial', 'name': 'Пробный', 'kind': 'trial', 'price_kopecks': 0,
     'duration_value': 2, 'duration_unit': 'months', 'quota': None, 'sms_quota': None},
    {'id': 'start', 'name': 'Старт', 'kind': 'paid', 'price_kopecks': 50000,
     'duration_value': 1, 'duration_unit': 'months', 'quota': 100, 'sms_quota': 0},
    {'id': 'business', 'name': 'Бизнес', 'kind': 'paid', 'price_kopecks': 100000,
     'duration_value': 1, 'duration_unit': 'months', 'quota': 500, 'sms_quota': 0},
    {'id': 'pro', 'name': 'Про', 'kind': 'paid', 'price_kopecks': 350000,
     'duration_value': 1, 'duration_unit': 'months', 'quota': 1000, 'sms_quota': 0},
]


def timestamp(value=None):
    if value is None: return time.time()
    if isinstance(value, datetime): return value.timestamp()
    if isinstance(value, str): return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    return float(value)


def integer(value, label, *, maximum=10000000, minimum=0, nullable=False):
    if nullable and value is None: return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(label + ': укажите целое число в допустимом диапазоне')
    return value


def media_url(value, extension='png'):
    if not value: return ''
    if not isinstance(value, str) or not re.fullmatch(r'/admin/billing-assets/[a-f0-9]{32}\.' + extension, value):
        raise ValueError('Загрузите файл через форму')
    return value


class BillingCatalog:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS billing_catalog(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(kind,id))')
            db.execute('CREATE TABLE IF NOT EXISTS billing_catalog_revisions(kind TEXT NOT NULL,item_id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,actor TEXT,created REAL NOT NULL,PRIMARY KEY(kind,item_id,revision))')
            db.execute('CREATE TABLE IF NOT EXISTS billing_offers(id TEXT PRIMARY KEY,filename TEXT NOT NULL,url TEXT NOT NULL,sha256 TEXT NOT NULL,published_at REAL NOT NULL,actor TEXT)')
            for plan in DEFAULT_PLANS:
                item = {**plan, 'logo': '', 'active': True, 'visible': plan['kind'] != 'trial', 'revision': 1}
                db.execute('INSERT OR IGNORE INTO billing_catalog VALUES(?,?,?,1)', ('plan', item['id'], json.dumps(item, ensure_ascii=False)))

    def _tx(self, db):
        return nullcontext(db) if db is not None else self.store.connect()

    @staticmethod
    def _item(payload):
        item = json.loads(payload)
        # Existing catalog revisions keep their commercial terms unchanged.
        # Visibility is independent of availability for existing subscribers.
        if 'duration_unit' in item:
            item.setdefault('visible', item.get('kind') != 'trial')
        return item

    def get(self, kind, item_id, *, include_archived=False, include_hidden=True, db=None):
        if not isinstance(kind,str) or kind not in {'plan','package'} or not isinstance(item_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',item_id):
            raise ValueError('Тариф или пакет не найден')
        with self._tx(db) as tx:
            row = tx.execute('SELECT payload FROM billing_catalog WHERE kind=? AND id=?', (kind, item_id)).fetchone()
            if row is None: raise ValueError('Тариф или пакет не найден')
            item = self._item(row['payload'])
            if not include_archived and not item['active']: raise ValueError('Тариф или пакет находится в архиве')
            if not include_hidden and not item.get('visible', True): raise ValueError('Тариф недоступен для самостоятельного подключения')
            return item

    def list(self, include_archived=False, *, include_hidden=False):
        with self.store.connect() as db:
            values = [self._item(row['payload']) for row in db.execute('SELECT payload FROM billing_catalog')]
            values = [item for item in values if (include_archived or item['active']) and (include_hidden or item.get('visible', True))]
            values.sort(key=lambda item: (item['price_kopecks'], item['name'].casefold(), item['id']))
            return {'plans': [item for item in values if 'duration_unit' in item],
                    'packages': [item for item in values if 'resource' in item], 'offer': self.offer(db=db)}

    def save(self, kind, payload, *, actor):
        if kind not in {'plan', 'package'} or not isinstance(payload, dict): raise ValueError('Некорректный каталог')
        allowed = {'id','name','price_kopecks','logo','active','visible','revision','kind','duration_value','duration_unit','quota','sms_quota'} if kind == 'plan' else {'id','name','price_kopecks','logo','active','revision','resource','quantity'}
        if set(payload) - allowed: raise ValueError('Неизвестные поля каталога')
        with self.store.connect() as db:
            item_id = payload.get('id') or uuid.uuid4().hex
            if not isinstance(item_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', item_id): raise ValueError('Некорректный ID')
            old = db.execute('SELECT payload,revision FROM billing_catalog WHERE kind=? AND id=?', (kind,item_id)).fetchone()
            if old and payload.get('revision') != old['revision']: raise ValueError('Каталог уже изменился. Обновите страницу')
            item = {**(self._item(old['payload']) if old else {}), **payload, 'id': item_id}
            name = item.get('name')
            if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c)<32 for c in name): raise ValueError('Укажите название до 100 символов')
            item['name'] = name.strip()
            item['price_kopecks'] = integer(item.get('price_kopecks'), 'Цена', maximum=1000000000)
            item['logo'] = media_url(item.get('logo', ''))
            if type(item.get('active', True)) is not bool: raise ValueError('Некорректный статус каталога')
            item['active'] = item.get('active', True)
            if kind == 'plan':
                item['kind'] = 'trial' if item_id == 'trial' else 'paid'
                if type(item.get('visible', item_id != 'trial')) is not bool: raise ValueError('Некорректная видимость тарифа')
                item['visible'] = item.get('visible', item_id != 'trial')
                if item.get('duration_unit') not in ('days', 'months'): raise ValueError('Срок задаётся в днях или календарных месяцах')
                item['duration_value'] = integer(item.get('duration_value'), 'Срок', minimum=1, maximum=1200 if item['duration_unit']=='months' else 36500)
                item['quota'] = integer(item.get('quota'), 'Квота звонков', nullable=True)
                item['sms_quota'] = integer(item.get('sms_quota'), 'Квота SMS', nullable=True)
                if item_id == 'trial' and (item['price_kopecks'] != 0 or not item['active']): raise ValueError('Пробный тариф бесплатный и не архивируется')
            else:
                if item.get('resource') not in ('calls','sms'): raise ValueError('Выберите звонки или SMS')
                item['quantity'] = integer(item.get('quantity'), 'Количество', minimum=1)
            item['revision'] = old['revision'] + 1 if old else 1
            encoded = json.dumps(item, ensure_ascii=False)
            db.execute('INSERT INTO billing_catalog VALUES(?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision', (kind,item_id,encoded,item['revision']))
            db.execute('INSERT INTO billing_catalog_revisions VALUES(?,?,?,?,?,?)', (kind,item_id,item['revision'],encoded,str(actor),time.time()))
            return item

    def publish_offer(self, metadata, *, actor, at=None):
        if not isinstance(metadata, dict) or set(metadata) != {'filename','url','sha256'}: raise ValueError('Некорректный файл оферты')
        url = media_url(metadata['url'], 'pdf')
        filename = metadata['filename']
        if not url or not isinstance(filename, str) or not filename or len(filename)>200 or any(c in filename for c in '/\\') or any(ord(c)<32 for c in filename): raise ValueError('Некорректное имя оферты')
        if not isinstance(metadata['sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',metadata['sha256']): raise ValueError('Не удалось проверить файл оферты')
        version = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute('INSERT INTO billing_offers VALUES(?,?,?,?,?,?)',(version,filename,url,metadata['sha256'],timestamp(at),str(actor)))
            return self.offer(version, db=db)

    def offer(self, version=None, *, db=None):
        with self._tx(db) as tx:
            row = tx.execute('SELECT * FROM billing_offers WHERE id=?',(version,)).fetchone() if version is not None else tx.execute('SELECT * FROM billing_offers ORDER BY published_at DESC,rowid DESC LIMIT 1').fetchone()
            return dict(row) if row else None
