"""Structured, rotated audit events with redaction and exact tenant filtering."""
from collections import deque
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import threading
import uuid
from storage import config

LOG_DIR = config.LOG_DIR
ADMIN_LOG_FILE = USER_LOG_FILE = DEBUG_LOG_FILE = str(Path(LOG_DIR) / 'events.jsonl')
_correlation = ContextVar('correlation_id', default=None)
_guard = threading.Lock()
_configured = None
_PRIVATE = re.compile(r'token|secret|password|authorization|cookie|phone|email|user_name|payload|raw|headers|body|message_text', re.I)


def reference(value):
    return hmac.new(config.SECRET_KEY.encode(), str(value).encode(), hashlib.sha256).hexdigest()[:16]


def correlation_id():
    return _correlation.get() or uuid.uuid4().hex


def set_correlation_id(value=None):
    value = value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,64}', value) else uuid.uuid4().hex
    _correlation.set(value)
    return value


def _text(value):
    value = str(value)
    value = re.sub(r'/(?:shared|api/pass|pwa|preferences)/[A-Za-z0-9_-]+', '/[REDACTED]', value)
    value = re.sub(r'(?:https?://\S+)?/(?:opn|unsubscribe)/[^\s?"\']+', '/opn/[REDACTED]', value)
    value = re.sub(r'\b\d{6,12}:[A-Za-z0-9_-]{20,}', '[BOT_TOKEN]', value)
    value = re.sub(r'\bgbt_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])', '[API_TOKEN]', value)
    value = re.sub(r'\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+', '[CREDENTIAL]', value)
    value = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[EMAIL]', value)
    value = re.sub(r'(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)', '[PHONE]', value)
    for key in ('BOT_TOKEN', 'MAX_BOT_TOKEN', 'YC_PARTNER_TOKEN', 'YC_USER_TOKEN', 'MAILING_SMTP_PASSWORD', 'SECRET_KEY'):
        secret = getattr(config, key, '')
        if secret and len(str(secret)) >= 6:
            value = value.replace(str(secret), '[REDACTED]')
    return value[:2000]


def redact(value):
    if isinstance(value, dict):
        return {str(key): '[REDACTED]' if _PRIVATE.search(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value[:50]]
    if isinstance(value, str):
        return _text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return type(value).__name__


def setup_loggers():
    global _configured
    path = str(Path(config.LOG_DIR) / 'events.jsonl')
    with _guard:
        audit = logging.getLogger('gatebot.audit')
        if _configured == path and audit.handlers:
            return audit
        for handler in list(audit.handlers):
            audit.removeHandler(handler)
            handler.close()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(message)s'))
        audit.addHandler(handler)
        audit.setLevel(logging.INFO)
        audit.propagate = False
        _configured = path
        return audit


def event(name, branch_id=None, actor=None, level='INFO', details=None, correlation=None):
    item = {'ts': datetime.now(timezone.utc).isoformat(), 'level': level, 'event': _text(name), 'branch_id': str(branch_id) if branch_id is not None else None, 'actor': reference(actor) if actor is not None else None, 'correlation_id': correlation or correlation_id(), 'details': redact(details or {})}
    setup_loggers().log(getattr(logging, level, logging.INFO), json.dumps(item, ensure_ascii=False))
    return item['correlation_id']


def _scope(details):
    details = details or {}
    branch = details.get('branch_id')
    if branch is None and details.get('token'):
        from link_manager import LinkManager
        data = LinkManager().load_links().get(details['token'], {})
        branch = data.get('branch_id')
    return branch


def log_admin_action(action, username, details=None):
    return event(action, branch_id=_scope(details), actor=username, details={**(details or {}), 'actor_type': 'admin'})


def log_user_action(action, user_identifier, details=None):
    return event(action, branch_id=_scope(details), actor=user_identifier, details={**(details or {}), 'actor_type': 'user'})


def log_error(error_type, message, details=None):
    return event(error_type, branch_id=_scope(details), level='ERROR', details={**(details or {}), 'reason': _text(message)})


def log_sim800_error(phone, error):
    return event('SIM800_ERROR', level='ERROR', details={'target_ref': reference(phone), 'reason': _text(error)})


def log_debug(message, details=None):
    if config.CONSOLE_DEBUG_MESSAGES:
        return event('DEBUG', branch_id=_scope(details), details={**(details or {}), 'reason': _text(message)})


def read_events(branch_id=None, limit=200):
    limit = max(1, min(int(limit), 5000))
    found = deque(maxlen=limit)
    base = Path(config.LOG_DIR) / 'events.jsonl'
    for path in [Path(str(base) + '.' + str(n)) for n in range(5, 0, -1)] + [base]:
        if not path.exists():
            continue
        with path.open(encoding='utf-8') as f:
            for line in f:
                try:
                    item = json.loads(line)
                    if branch_id is not None and str(item.get('branch_id')) != str(branch_id):
                        continue
                    found.append(item)
                except (ValueError, TypeError):
                    continue
    return list(reversed(found))
