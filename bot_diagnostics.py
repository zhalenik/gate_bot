"""Explicit read-only bot checks and safe startup errors; no polling or sends."""
from __future__ import annotations

import asyncio
import logging
import socket
import ssl

from storage import config


_ACTIONS = {
    'token_missing': 'Заполните токен нужного бота в .env и перезапустите команду.',
    'token_invalid_format': 'Проверьте MAX_BOT_TOKEN в .env: только токен, без Authorization, Bearer, перевода строки или лишних пробелов.',
    'unauthorized': 'Сервер отклонил токен. Проверьте токен именно этого бота в .env; не публикуйте его.',
    'forbidden': 'Сервер отказал в доступе. Проверьте статус бота и разрешения в кабинете платформы.',
    'tls_trust_failed': 'Проверка TLS не прошла. Для MAX см. docs/MAX.md: официальный корневой сертификат и MAX_CA_BUNDLE. Не отключайте проверку TLS.',
    'ca_bundle_invalid': 'Проверьте файл MAX_CA_BUNDLE: он должен существовать и содержать доверенные сертификаты PEM.',
    'connect_timeout': 'Не удалось подключиться за отведённое время. Проверьте сеть, VPN, прокси и доступность API.',
    'read_timeout': 'Подключение не дало ответа вовремя. Проверьте сеть/VPN и повторите проверку.',
    'timeout': 'API не ответил вовремя. Проверьте сеть/VPN и повторите проверку.',
    'dns_error': 'Не удалось разрешить имя API. Проверьте DNS и сеть/VPN.',
    'proxy_error': 'Ошибка прокси. Проверьте настройки прокси этого процесса и Windows.',
    'connection_error': 'Соединение с API прервано или недоступно. Проверьте сеть, VPN и защитное ПО.',
    'network_error': 'Сетевая ошибка. Проверьте соединение, VPN и прокси.',
    'active_webhook': 'У бота есть webhook. Polling не запущен. Используйте отдельного тестового бота или отдельно согласуйте отключение webhook.',
    'polling_already_running': 'Остановите другой локальный polling этого токена и повторите запуск.',
    'polling_conflict': 'У токена другой polling или webhook. Проверьте другие экземпляры бота.',
    'rate_limited': 'API ограничил частоту запросов. Подождите и повторите проверку.',
    'provider_unavailable': 'API временно недоступен. Повторите проверку позднее.',
    'invalid_response': 'API вернул неожиданный ответ. Сохраните этот диагностический отчёт для разбора.',
    'invalid_configuration': 'Проверьте параметры бота в .env и выполните manage.py doctor.',
    'api_error': 'API отклонил запрос. Для разбора нужны error_code, http_status и stage из этого отчёта.',
    'internal_error': 'Внутренняя ошибка. Для разбора нужны тип ошибки и безопасный журнал приложения.',
}
_STAGES = {'initialize', 'get_me', 'webhook_info', 'subscriptions', 'polling',
           'configuration', 'send_message', 'answer_callback', 'unknown'}


class BotCheckError(RuntimeError):
    def __init__(self, code, *, stage='unknown'):
        self.code = code if code in _ACTIONS else 'internal_error'
        self.stage = stage if stage in _STAGES else 'unknown'
        super().__init__(self.code)


def _telegram_error_code(error):
    from telegram.error import Conflict, Forbidden, InvalidToken, NetworkError, RetryAfter, TimedOut

    if isinstance(error, InvalidToken):
        return 'unauthorized'
    if isinstance(error, Forbidden):
        return 'forbidden'
    if isinstance(error, Conflict):
        return 'polling_conflict'
    if isinstance(error, RetryAfter):
        return 'rate_limited'
    # Inspect exception types only: HTTP client exception text can contain the
    # token-bearing Telegram URL, proxy passwords or private response bodies.
    import httpx
    current, visited = error, set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, ssl.SSLError):
            return 'tls_trust_failed'
        if isinstance(current, socket.gaierror):
            return 'dns_error'
        if isinstance(current, httpx.ProxyError):
            return 'proxy_error'
        if isinstance(current, httpx.ConnectTimeout):
            return 'connect_timeout'
        if isinstance(current, httpx.ReadTimeout):
            return 'read_timeout'
        current = current.__cause__ or current.__context__
    if isinstance(error, TimedOut):
        return 'timeout'
    if isinstance(error, NetworkError):
        return 'network_error'
    return 'internal_error'


def error_details(platform, error, *, stage=None):
    """Return only known codes and fixed advice, never str(error) or payloads."""
    from max_api import MaxApiError
    from polling_lock import PollingAlreadyRunning

    status, retryable = None, False
    if isinstance(error, MaxApiError):
        safe = error.diagnostic()
        code, status = safe['code'], safe['http_status']
        stage = stage or safe['stage']
        retryable = safe['retryable']
    elif isinstance(error, BotCheckError):
        code = error.code
        stage = stage or error.stage
    elif isinstance(error, PollingAlreadyRunning):
        code = 'polling_already_running'
    elif platform == 'telegram':
        code = _telegram_error_code(error)
    else:
        code = 'internal_error'
    stage = stage or getattr(error, 'diagnostic_stage', 'unknown')
    stage = stage if stage in _STAGES else 'unknown'
    advice_code = code
    if status == 401:
        advice_code = 'unauthorized'
    elif status == 403:
        advice_code = 'forbidden'
    elif status == 429:
        advice_code = 'rate_limited'
    elif status is not None and status >= 500:
        advice_code = 'provider_unavailable'
    elif code in {'invalid_json', 'invalid_subscriptions', 'invalid_updates'}:
        advice_code = 'invalid_response'
    action = _ACTIONS.get(advice_code, _ACTIONS['api_error'])
    if platform == 'telegram' and advice_code == 'tls_trust_failed':
        action = 'Проверка TLS Telegram не прошла. Проверьте системное время, доверенные сертификаты и HTTPS-прокси; не отключайте TLS.'
    return {'error_code': code, 'http_status': status, 'stage': stage,
            'retryable': bool(retryable), 'action': action}


def _username_report(configured, actual):
    expected = str(configured or '').strip().lstrip('@')
    actual = actual if isinstance(actual, str) else ''
    return {'username_configured': bool(expected), 'api_username_present': bool(actual),
            'configured_username_matches': expected.casefold() == actual.casefold() if expected else None}


def _check_max():
    from max_api import MaxApiClient

    stage = 'configuration'
    try:
        if not config.MAX_BOT_TOKEN:
            raise BotCheckError('token_missing', stage=stage)
        try:
            api = MaxApiClient(config.MAX_BOT_TOKEN, base_url=config.MAX_API_BASE_URL,
                               ca_bundle=config.MAX_CA_BUNDLE or None, timeout=10, retries=0)
        except (ValueError, TypeError):
            raise BotCheckError('invalid_configuration', stage=stage) from None
        stage = 'get_me'
        profile = api.get_me()
        if profile.get('is_bot') is not True or type(profile.get('user_id')) is not int:
            raise BotCheckError('invalid_response', stage=stage)
        identity = _username_report(config.MAX_BOT_USERNAME, profile.get('username'))
        stage = 'subscriptions'
        subscriptions = api.get_subscriptions()
        if subscriptions:
            return {'status': 'error', **identity, 'active_webhook_count': len(subscriptions),
                    **error_details('max', BotCheckError('active_webhook', stage=stage))}
        return {'status': 'ok', **identity, 'active_webhook_count': 0}
    except Exception as error:
        return {'status': 'error', **error_details('max', error, stage=stage)}


async def _check_telegram():
    from telegram import Bot
    from telegram.request import HTTPXRequest

    stage = 'configuration'
    try:
        if not config.BOT_TOKEN:
            raise BotCheckError('token_missing', stage=stage)
        # Use the same library as runtime, including its proxy/TLS behaviour.
        # Do not log its exception strings or HTTP request URLs.
        for name in ('httpx', 'httpcore', 'telegram'):
            logging.getLogger(name).setLevel(logging.CRITICAL + 1)
        proxy_options = {'proxy': config.TELEGRAM_PROXY_URL} if config.TELEGRAM_PROXY_URL else {}
        request = HTTPXRequest(connect_timeout=5, read_timeout=10, write_timeout=10, **proxy_options)
        bot = Bot(config.BOT_TOKEN, request=request)
        stage = 'get_me'
        async with bot:
            identity = _username_report(config.BOT_USERNAME, bot.username)
            stage = 'webhook_info'
            webhook = await bot.get_webhook_info()
            if webhook.url:
                return {'status': 'error', **identity, 'active_webhook_count': 1,
                        **error_details('telegram', BotCheckError('active_webhook', stage=stage))}
            return {'status': 'ok', **identity, 'active_webhook_count': 0}
    except Exception as error:
        return {'status': 'error', **error_details('telegram', error, stage=stage)}


def check_bots(platform='all'):
    """Read bot identity and webhook metadata only; never consume bot updates."""
    if platform not in {'max', 'telegram', 'all'}:
        raise ValueError('Unknown bot platform')
    bots = {}
    for name in ('max', 'telegram'):
        if platform not in {'all', name}:
            continue
        enabled = config.MAX_BOT_ENABLED if name == 'max' else config.BOT_ENABLED
        if not enabled:
            bots[name] = {'enabled': False, 'status': 'skipped', 'reason': 'disabled'}
            continue
        report = _check_max() if name == 'max' else asyncio.run(_check_telegram())
        bots[name] = {'enabled': True, **report}
    return {'read_only': True, 'polling': 'not_started', 'messages': 'not_sent',
            'yclients': 'not_accessed', 'modem': 'not_accessed', 'bots': bots}
