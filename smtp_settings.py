"""Encrypted installation-wide SMTP settings. No transport runs on construction."""
from contextlib import contextmanager
import ipaddress
import json
import re
import smtplib
import ssl
import time

from identity_service import IdentityService
from storage import config


def email_address(value):
    return (isinstance(value, str) and len(value) <= 254 and
            re.fullmatch(r'[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+', value) is not None)


def validate(values, *, connection=False):
    if type(values.get('enabled')) is not bool:
        raise ValueError('Укажите состояние отправки email')
    for field, limit in (('host', 253), ('username', 254), ('password', 1024), ('from_name', 100), ('from_email', 254)):
        value = values.get(field)
        if not isinstance(value, str) or len(value) > limit or any(c in value for c in '\r\n\x00'):
            raise ValueError('Некорректное поле SMTP: ' + field)
    host = values['host']
    if host:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host) or '..' in host:
                raise ValueError('Укажите имя SMTP-сервера без протокола и пути')
    if type(values.get('port')) is not int or not 1 <= values['port'] <= 65535:
        raise ValueError('Порт SMTP должен быть от 1 до 65535')
    if values.get('security') not in {'ssl', 'starttls'}:
        raise ValueError('Выберите SSL/TLS или STARTTLS')
    if values['from_email'] and not email_address(values['from_email']):
        raise ValueError('Укажите корректный email отправителя')
    if values['enabled'] or connection:
        if not host or not email_address(values['from_email']):
            raise ValueError('Заполните SMTP-сервер и email отправителя')
        if bool(values['username']) != bool(values['password']):
            raise ValueError('Для авторизации укажите логин и пароль SMTP')
    return values


@contextmanager
def smtp_connection(values):
    """Authenticated TLS connection; explicit callers decide whether to send."""
    validate(values, connection=True)
    context = ssl.create_default_context()
    factory = smtplib.SMTP_SSL if values['security'] == 'ssl' else smtplib.SMTP
    kwargs = {'host': values['host'], 'port': values['port'], 'timeout': 15}
    if values['security'] == 'ssl':
        kwargs['context'] = context
    with factory(**kwargs) as server:
        server.ehlo()
        if values['security'] == 'starttls':
            server.starttls(context=context)
            server.ehlo()
        if values['username']:
            server.login(values['username'], values['password'])
        yield server


class SmtpSettings:
    def __init__(self, identity=None):
        self.identity = identity or IdentityService()
        self.store = self.identity.product
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS application_settings(name TEXT PRIMARY KEY,ciphertext TEXT NOT NULL,updated REAL NOT NULL)')

    def _super(self, username):
        if self.identity.web_admin(username).get('is_super') is not True:
            raise PermissionError('SMTP настраивает только суперадминистратор')

    @staticmethod
    def defaults():
        return {'enabled': config.SMTP_ENABLED, 'host': config.MAILING_SMTP_SERVER,
                'port': config.MAILING_SMTP_PORT,
                'security': getattr(config, 'MAILING_SMTP_SECURITY', 'ssl' if config.MAILING_SMTP_PORT == 465 else 'starttls'),
                'username': config.MAILING_SMTP_USER, 'password': config.MAILING_SMTP_PASSWORD,
                'from_name': config.MAILING_SMTP_FROM,
                'from_email': getattr(config, 'MAILING_SMTP_FROM_EMAIL', '') or config.MAILING_SMTP_USER}

    def effective(self):
        """Server-only credentials; never serialize this return value to HTTP/logs."""
        with self.store.connect() as db:
            row = db.execute('SELECT ciphertext FROM application_settings WHERE name="smtp"').fetchone()
        if not row:
            return self.defaults()
        return json.loads(self.identity._fernet().decrypt(row['ciphertext'].encode()))

    def public(self, username):
        self._super(username)
        values = self.effective()
        with self.store.connect() as db:
            saved = db.execute('SELECT updated FROM application_settings WHERE name="smtp"').fetchone()
        return {k: v for k, v in values.items() if k != 'password'} | {
            'has_password': bool(values['password']), 'source': 'saved' if saved else 'environment',
            'outbound_enabled': config.OUTBOUND_ENABLED}

    def save(self, username, payload):
        self._super(username)
        permitted = {'enabled', 'host', 'port', 'security', 'username', 'password', 'from_name', 'from_email', 'clear_password'}
        if not isinstance(payload, dict) or set(payload) - permitted:
            raise ValueError('Некорректные настройки SMTP')
        if 'clear_password' in payload and type(payload['clear_password']) is not bool:
            raise ValueError('Некорректный признак удаления пароля')
        if payload.get('clear_password') and payload.get('password'):
            raise ValueError('Нельзя одновременно заменить и удалить пароль')
        with self.store.connect() as db:
            self._super(username)
            values = self.effective()
            for key in permitted - {'password', 'clear_password'}:
                if key in payload:
                    values[key] = payload[key].strip() if isinstance(payload[key], str) else payload[key]
            if 'password' in payload and not isinstance(payload['password'], str):
                raise ValueError('Некорректный пароль SMTP')
            if payload.get('clear_password'):
                values['password'] = ''
            elif payload.get('password'):
                values['password'] = payload['password']
            validate(values)
            ciphertext = self.identity._fernet().encrypt(json.dumps(values, ensure_ascii=False).encode()).decode()
            db.execute('INSERT INTO application_settings VALUES("smtp",?,?) ON CONFLICT(name) DO UPDATE SET ciphertext=excluded.ciphertext,updated=excluded.updated',
                       (ciphertext, time.time()))
        import logger
        logger.event('SMTP_SETTINGS_CHANGED', actor=username, details={'enabled': values['enabled'], 'security': values['security']})
        return self.public(username)

    def check(self, username):
        self._super(username)
        values = self.effective()
        validate(values, connection=True)
        try:
            with smtp_connection(values) as server:
                code, _ = server.noop()
                if code != 250:
                    return {'status': 'error', 'message': 'SMTP-сервер не подтвердил проверку соединения'}
        except smtplib.SMTPAuthenticationError:
            return {'status': 'error', 'message': 'SMTP-сервер отклонил логин или пароль'}
        except ssl.SSLError:
            return {'status': 'error', 'message': 'Не удалось проверить защищённое соединение SMTP'}
        except (OSError, smtplib.SMTPException):
            return {'status': 'error', 'message': 'Не удалось подключиться к SMTP. Проверьте адрес, порт и режим TLS'}
        return {'status': 'success', 'message': 'Соединение и авторизация проверены. Письмо не отправлялось'}
