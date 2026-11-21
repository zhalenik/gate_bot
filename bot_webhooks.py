"""Private webhook inbox, provider ownership and the two authenticated HTTP routes."""
import base64
import hashlib
import hmac
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from flask import Blueprint, current_app, request

from bot_state import BotStateStore


HEADERS = {'telegram':'X-Telegram-Bot-Api-Secret-Token', 'max':'X-Max-Bot-Api-Secret'}
EXTENSION = 'gatebot_bot_webhooks'
bp = Blueprint('bot_webhooks', __name__)


def webhook_url(platform, base, override=''):
    url = override or base.rstrip('/') + '/webhook/' + platform
    parts = urlsplit(url)
    ports = {None,443} if platform=='max' else {None,443,80,88,8443}
    if (parts.scheme!='https' or not parts.hostname or parts.port not in ports or parts.username is not None
            or parts.password is not None or parts.fragment or parts.query or any(c.isspace() for c in url)):
        raise ValueError('Для webhook нужен публичный HTTPS-адрес GateBot с поддерживаемым портом')
    return url


def validate_secret(platform, secret):
    minimum = 5 if platform=='max' else 1
    if secret and not re.fullmatch(r'[A-Za-z0-9_-]{%d,256}' % minimum, secret):
        raise ValueError(f'Секрет webhook {platform}: от {minimum} до 256 символов A–Z, a–z, 0–9, дефис или подчёркивание')


class BotInbox:
    def __init__(self, platform, token, state, *, url='', secret=''):
        self.platform, self.state, self.url = platform,state,url
        self.namespace = platform + ':' + hashlib.sha256(token.encode()).hexdigest()[:16]
        self.wake = threading.Event()
        self.wake.set()  # Resume updates accepted before the last restart.
        self.secret = ''
        if url:
            validate_secret(platform,secret)
            self.secret = secret or hmac.new(token.encode(),('GateBot webhook '+platform+' '+url).encode(),'sha256').hexdigest()
        key = hmac.new(token.encode(),('GateBot inbox '+platform).encode(),'sha256').digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(key))

    @classmethod
    def configured(cls, platform):
        from storage import config
        telegram = platform=='telegram'
        token = config.BOT_TOKEN if telegram else config.MAX_BOT_TOKEN
        mode = config.BOT_MODE if telegram else config.MAX_BOT_MODE
        if not token or mode not in {'polling','webhook'}:
            raise ValueError('Проверьте режим и токен бота')
        url = webhook_url(platform, config.BASE_URL, config.WEBHOOK_URL if telegram else '') if mode=='webhook' else ''
        return cls(platform,token,BotStateStore(Path(config.DATA_DIR)/'bot_state.sqlite3'),url=url,
                   secret=config.TELEGRAM_WEBHOOK_SECRET if telegram else config.MAX_WEBHOOK_SECRET)

    def enqueue(self, data):
        if self.platform=='telegram':
            event_id = data.get('update_id')
            if type(event_id) is not int or event_id<0:
                raise ValueError('Invalid Telegram update')
            key = str(event_id)
        else:
            from max_bot import normalize_update
            if not isinstance(data.get('update_type'), str):
                raise ValueError('Invalid MAX update')
            update = normalize_update(data)
            if update is None:
                return  # Unsupported event; contains no consumable command.
            key = update.event_key
        content=json.dumps(data, ensure_ascii=False, allow_nan=False).encode()
        self.state.enqueue_webhook(self.namespace,key,self.cipher.encrypt(content))
        self.wake.set()

    def take(self):
        self.wake.clear()
        row = self.state.claim_webhook(self.namespace)
        if row is None:
            return None
        self.wake.set()
        stamp,content = row
        data = json.loads(self.cipher.decrypt(content))
        query = data.get('callback_query')
        if (self.platform=='telegram' and isinstance(query,dict) and str(query.get('data','')).startswith('g:')
                and time.time()-stamp>30):
            return None
        return data

    def check_ownership(self, urls):
        previous = self.state.get_checkpoint(self.namespace,'owned_webhook',{})
        # A configured destination alone is not evidence this installation
        # created the provider subscription. A recorded intent also covers a
        # restart after registration was accepted but before confirmation.
        if any(not url or url not in (previous.get('url'),previous.get('previous_url')) for url in urls):
            from bot_diagnostics import BotCheckError
            raise BotCheckError('active_webhook',stage='subscriptions' if self.platform=='max' else 'webhook_info')
        return previous

    def registration(self, confirmed):
        return {'url':self.url,'key_hash':hashlib.sha256(self.secret.encode()).hexdigest(),'confirmed':confirmed}

    def remember(self, record):
        self.state.set_checkpoint(self.namespace,'owned_webhook',record)


def receive(secret, header, enqueue):
    supplied = request.headers.get(header,'')
    if not secret or not hmac.compare_digest(supplied.encode(),secret.encode()):
        return {'error':'unauthorized'},401
    request.max_content_length = 1024*1024
    data = request.get_json(silent=True)
    if not isinstance(data,dict):
        return {'error':'invalid_json'},400
    try:
        enqueue(data)
    except (ValueError, UnicodeError):
        return {'error':'invalid_update'},400
    return {'status':'accepted'},200


@bp.post('/webhook/telegram', defaults={'platform':'telegram'})
@bp.post('/webhook/max', defaults={'platform':'max'})
def ingress(platform):
    inbox = current_app.extensions.get(EXTENSION,{}).get(platform)
    if inbox is None:
        return {'error':'unavailable'},503
    return receive(inbox.secret,HEADERS[platform],inbox.enqueue)
