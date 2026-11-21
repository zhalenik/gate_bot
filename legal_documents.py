"""Immutable registration documents and proof of each separate affirmative choice."""
import hashlib
import json
from pathlib import Path
import re
import time


TITLES = {
    'admin-privacy': 'Согласие администратора на обработку персональных данных',
    'terms': 'Правила использования GateBot',
    'client-privacy': 'Согласие клиента на обработку персональных данных',
}
ADMIN_DOCS = ('admin-privacy', 'terms')
CLIENT_DOCS = ('client-privacy',)
MAX_SIZE = 5 * 1024 * 1024


def setup(db):
    db.execute('''CREATE TABLE IF NOT EXISTS registration_documents(
        id INTEGER PRIMARY KEY, kind TEXT NOT NULL, filename TEXT NOT NULL,
        media TEXT NOT NULL, digest TEXT NOT NULL, content BLOB NOT NULL,
        published REAL NOT NULL, author TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS registration_documents_kind ON registration_documents(kind,id)')
    db.execute('''CREATE TABLE IF NOT EXISTS registration_consent(
        challenge TEXT NOT NULL, document_id INTEGER NOT NULL REFERENCES registration_documents(id),
        flow TEXT NOT NULL, branch_id TEXT NOT NULL, presented REAL NOT NULL,
        accepted REAL, phone TEXT, participant TEXT, verification_channel TEXT,
        PRIMARY KEY(challenge,document_id))''')
    db.execute('CREATE INDEX IF NOT EXISTS pending_registration_consent ON registration_consent(presented) WHERE accepted IS NULL')
    existing = {row[0] for row in db.execute('SELECT DISTINCT kind FROM registration_documents')}
    for kind in TITLES:
        if kind in existing:
            continue
        file = Path(__file__).parent / 'resources' / 'legal' / (kind + '.txt')
        publish(db, kind, file.name, file.read_bytes(), 'GateBot')


def publish(db, kind, filename, content, actor):
    if kind not in TITLES:
        raise ValueError('Неизвестный вид документа')
    if not content or len(content) > MAX_SIZE:
        raise ValueError('Выберите документ размером до 5 МБ')
    name = Path(str(filename).replace('\\', '/')).name[:120]
    if name.lower().endswith('.txt'):
        try:
            value = content.decode('utf-8-sig').strip()
        except UnicodeDecodeError:
            raise ValueError('Сохраните текст в кодировке UTF-8') from None
        if not value or re.search(r'[\x00-\x08\x0b-\x0c\x0e-\x1f]', value):
            raise ValueError('В файле должен быть текст документа')
        media = 'text/plain'
    elif name.lower().endswith('.pdf') and re.match(rb'%PDF-[12]\.[0-9]', content) and b'%%EOF' in content[-2048:]:
        media = 'application/pdf'
    else:
        raise ValueError('Выберите PDF или текстовый файл UTF-8')
    digest = hashlib.sha256(content).hexdigest()
    last = db.execute('SELECT id,digest FROM registration_documents WHERE kind=? ORDER BY id DESC LIMIT 1', (kind,)).fetchone()
    if last and last['digest'] == digest:
        return last['id']
    return db.execute('INSERT INTO registration_documents(kind,filename,media,digest,content,published,author) VALUES(?,?,?,?,?,?,?)',
                      (kind, name, media, digest, content, time.time(), actor)).lastrowid


def current(db, kinds):
    result = []
    for kind in kinds:
        row = db.execute('SELECT id,media FROM registration_documents WHERE kind=? ORDER BY id DESC LIMIT 1', (kind,)).fetchone()
        result.append({'kind': kind, 'title': TITLES[kind], 'version': row['id'], 'media': row['media'],
                       'url': f"/documents/{kind}/{row['id']}"})
    return result


def choices(db, flow, value):
    kinds = CLIENT_DOCS if flow == 'branch_qr' else ADMIN_DOCS
    expected = {item['kind']: item['version'] for item in current(db, kinds)}
    if not isinstance(value, dict) or set(value) != set(expected) or any(type(v) is not int for v in value.values()):
        raise ValueError('Ознакомьтесь с документами и отметьте согласие перед получением кода')
    if expected != value:
        raise ValueError('Редакция документа изменилась. Обновите страницу, прочитайте документ и запросите новый код.')
    return expected


def reserve(db, flow, value, challenge, branch, stamp):
    versions = choices(db, flow, value)
    db.execute('DELETE FROM registration_consent WHERE accepted IS NULL AND presented < ?', (stamp - 86400,))
    db.executemany('INSERT INTO registration_consent(challenge,document_id,flow,branch_id,presented) VALUES(?,?,?,?,?)',
                   [(challenge, version, flow, str(branch), stamp) for version in versions.values()])


def confirm(db, flow, value, otp, participant, stamp):
    versions = choices(db, flow, value)
    records = db.execute('SELECT document_id FROM registration_consent WHERE challenge=? AND flow=? AND accepted IS NULL',
                         (otp['id'], flow))
    recorded = {row['document_id'] for row in records}
    if recorded != set(versions.values()):
        raise ValueError('Запросите новый код для подтверждения выбранных документов')
    delivery = json.loads(otp['delivery'] or '{}')
    channel = 'test_redirect' if delivery.get('_development_redirect') else delivery.get('channel', 'sms')
    db.execute('UPDATE registration_consent SET accepted=?,phone=?,participant=?,verification_channel=? WHERE challenge=? AND flow=?',
               (stamp, otp['phone'], participant, channel, otp['id'], flow))
