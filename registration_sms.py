"""Explicit local-only registration SMS routing. No default test destinations."""
import re
from storage import config


def sms_recipient(phone):
    # Unknown/staging environments are deliberately excluded as well. This is
    # not a transport-wide override: only registration requests call it.
    if config.ENVIRONMENT != 'development' or not config.EXPLICIT_DEVELOPMENT or not config.DEV_REGISTRATION_SMS_REDIRECT_ENABLED:
        return phone
    prefix, target = config.DEV_REGISTRATION_SMS_PREFIX, config.DEV_REGISTRATION_SMS_PHONE
    if not re.fullmatch(r'\+[1-9][0-9]{3,13}', prefix) or not re.fullmatch(r'\+[1-9][0-9]{6,14}', target):
        raise ValueError('Некорректная приватная настройка тестовой доставки кода регистрации')
    return target if phone.startswith(prefix) else phone
