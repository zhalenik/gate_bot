"""Portable JSON for installation settings. Credentials travel in a password-encrypted block."""
import base64
import json
import os

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from modem_settings import check_inventory_assignments, inventory, save_modems
from smtp_settings import SmtpSettings, validate as validate_smtp
from storage import config
from system_settings_catalog import BY_KEY, default_values, validate_effective


def _box(password, salt):
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        raise ValueError('Введите пароль файла длиной от 8 до 256 символов')
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def export_settings(settings, username, password):
    settings._super(username)
    salt = os.urandom(16)
    cipher = _box(password, salt)
    with settings.identity.product.connect():
        effective = default_values({**config.SYSTEM_BASE_ENVIRONMENT, **settings.store.read()['values']})
        values = validate_effective(effective)
        smtp = SmtpSettings(settings.identity).effective()
        fleet = inventory(settings.identity.product)
    secrets = {key: values.pop(key) for key, field in BY_KEY.items() if field['secret']}
    secrets['smtp_password'] = smtp.pop('password')
    return {'format': 'gatebot-settings-v1', 'values': values, 'smtp': smtp,
            'modems': fleet['modems'] if fleet['revision'] else None,
            'salt': salt.hex(), 'credentials': cipher.encrypt(json.dumps(secrets).encode()).decode()}


def import_settings(settings, username, password, document, revision):
    settings._super(username)
    if not isinstance(document, dict) or document.get('format') != 'gatebot-settings-v1':
        raise ValueError('Выберите файл настроек GateBot')
    try:
        salt = bytes.fromhex(document['salt'])
        if len(salt) != 16:
            raise ValueError
        secrets = json.loads(_box(password, salt).decrypt(document['credentials'].encode()))
    except (InvalidToken, TypeError, AttributeError, KeyError, ValueError):
        raise ValueError('Неверный пароль или повреждённый файл настроек') from None
    required_secrets = {key for key, field in BY_KEY.items() if field['secret']}
    if not isinstance(secrets, dict) or set(secrets) != required_secrets | {'smtp_password'}:
        raise ValueError('Некорректные поля секретов')
    public = document.get('values')
    if not isinstance(public, dict) or set(public) != set(BY_KEY) - required_secrets:
        raise ValueError('Проверьте состав системных параметров')
    smtp = document.get('smtp')
    if not isinstance(smtp, dict) or set(smtp) != set(SmtpSettings.defaults()) - {'password'}:
        raise ValueError('Проверьте поля SMTP')
    smtp = validate_smtp({**smtp, 'password': secrets.pop('smtp_password')})
    values = validate_effective({**public, **secrets})
    if 'modems' not in document:
        raise ValueError('В файле отсутствует список модемов')
    store = settings.identity.product
    with store.connect() as db:
        settings._super(username)
        current = inventory(store)
        if document['modems'] is None:
            check_inventory_assignments(db, {'legacy'})
            db.execute('DELETE FROM sim800_inventory WHERE id=1')
        else:
            save_modems(store, current['revision'], document['modems'])
        SmtpSettings(settings.identity).save(username, {**smtp, 'clear_password': not smtp['password']})
        settings.store.save(values, revision)
    return settings.public(username)
