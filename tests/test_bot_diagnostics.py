"""Bot diagnosis reads only identity/subscriptions and never reveals responses."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import requests

import bot_diagnostics as diagnostics
from max_api import MaxApiClient, MaxApiError
from telegram.error import InvalidToken, TimedOut


@pytest.fixture
def settings(monkeypatch):
    value = SimpleNamespace(
        MAX_BOT_ENABLED=True, MAX_BOT_TOKEN='synthetic-max-secret',
        MAX_BOT_USERNAME='example_bot', MAX_API_BASE_URL='https://platform-api2.max.ru', MAX_CA_BUNDLE='',
        BOT_ENABLED=True, BOT_TOKEN='123456:synthetic-telegram-secret', BOT_USERNAME='example_bot',
    )
    monkeypatch.setattr(diagnostics, 'config', value)
    return value


def max_transport(monkeypatch, replies):
    import max_api
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        item = replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(status_code=200, content=b'present', json=lambda: item)

    def client(*args, **kwargs):
        return MaxApiClient(*args, **kwargs, session=SimpleNamespace(request=request), sleep=lambda _: None)

    monkeypatch.setattr(max_api, 'MaxApiClient', client)
    return calls


def profile(**overrides):
    return {'is_bot': True, 'user_id': 1234, 'username': 'example_bot',
            'name': 'private display name', 'description': 'private description', **overrides}


def assert_no_private_values(report):
    text = json.dumps(report)
    for forbidden in ('synthetic-max-secret', 'synthetic-telegram-secret', 'private display name',
                      'private description', 'example_bot', '1234', 'private.invalid', 'private-exception'):
        assert forbidden not in text


def test_max_preflight_reads_identity_and_subscriptions_only(settings, monkeypatch):
    calls = max_transport(monkeypatch, [profile(), {'subscriptions': []}])
    report = diagnostics.check_bots('max')
    assert report['bots']['max'] == {'enabled': True, 'status': 'ok',
        'username_configured': True, 'api_username_present': True,
        'configured_username_matches': True, 'active_webhook_count': 0}
    assert [(method, url.split('.ru')[-1]) for method, url, _ in calls] == [
        ('GET', '/me'), ('GET', '/subscriptions')]
    assert all(call[2]['verify'] is True and call[2]['allow_redirects'] is False for call in calls)
    assert report['polling'] == 'not_started'
    assert report['messages'] == 'not_sent'
    assert report['yclients'] == report['modem'] == 'not_accessed'
    assert_no_private_values(report)


def test_webhook_is_reported_without_url_or_mutations(settings, monkeypatch):
    calls = max_transport(monkeypatch, [profile(), {'subscriptions': [{'url': 'https://private.invalid/secret'}]}])
    report = diagnostics.check_bots('max')
    assert report['bots']['max']['error_code'] == 'active_webhook'
    assert report['bots']['max']['stage'] == 'subscriptions'
    assert report['bots']['max']['active_webhook_count'] == 1
    assert len(calls) == 2 and all(call[0] == 'GET' for call in calls)
    assert_no_private_values(report)


def test_missing_username_does_not_prevent_authorization_check(settings, monkeypatch):
    settings.MAX_BOT_USERNAME = ''
    max_transport(monkeypatch, [profile(), {'subscriptions': []}])
    report = diagnostics.check_bots('max')['bots']['max']
    assert report['status'] == 'ok'
    assert report['username_configured'] is False
    assert report['configured_username_matches'] is None


def test_wrong_username_is_detected_without_echoing_names(settings, monkeypatch):
    max_transport(monkeypatch, [profile(username='other_private_bot'), {'subscriptions': []}])
    report = diagnostics.check_bots('max')
    assert report['bots']['max']['configured_username_matches'] is False
    assert 'other_private_bot' not in json.dumps(report)


@pytest.mark.parametrize('overrides', [{'is_bot': False}, {'user_id': True}, {'user_id': '1234'}])
def test_invalid_max_identity_is_not_success(settings, monkeypatch, overrides):
    calls = max_transport(monkeypatch, [profile(**overrides)])
    report = diagnostics.check_bots('max')
    assert report['bots']['max']['error_code'] == 'invalid_response'
    assert report['bots']['max']['stage'] == 'get_me'
    assert len(calls) == 1


@pytest.mark.parametrize('error,code', [
    (requests.exceptions.SSLError('private-exception'), 'tls_trust_failed'),
    (requests.exceptions.ReadTimeout('private-exception'), 'read_timeout'),
    (requests.exceptions.ProxyError('https://private.invalid/password'), 'proxy_error'),
])
def test_max_probe_classifies_failure_without_retries_or_private_message(settings, monkeypatch, error, code):
    calls = max_transport(monkeypatch, [error])
    report = diagnostics.check_bots('max')
    assert report['bots']['max']['error_code'] == code
    assert report['bots']['max']['stage'] == 'get_me'
    assert len(calls) == 1
    assert_no_private_values(report)


def test_invalid_max_origin_is_configuration_error(settings, monkeypatch):
    settings.MAX_API_BASE_URL = 'https://private.invalid/secret?token=synthetic-max-secret'
    calls = max_transport(monkeypatch, [])
    report = diagnostics.check_bots('max')
    assert report['bots']['max']['error_code'] == 'invalid_configuration'
    assert calls == []
    assert_no_private_values(report)


def test_http_401_has_actionable_advice_and_safe_metadata():
    report = diagnostics.error_details('max', MaxApiError('private_leak', status_code=401, stage='get_me'))
    assert report['error_code'] == 'api_error'
    assert report['http_status'] == 401
    assert 'токен' in report['action']
    assert 'private_leak' not in json.dumps(report)


def telegram_transport(monkeypatch, *, error=None, url=''):
    import telegram
    import telegram.request
    calls = []
    class FakeBot:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            calls.append('getMe')
            if error:
                raise error
            self.username = 'example_bot'
            return self
        async def get_webhook_info(self):
            calls.append('getWebhookInfo')
            return SimpleNamespace(url=url)
        async def __aexit__(self, *args):
            calls.append('close')
    monkeypatch.setattr(telegram, 'Bot', FakeBot)
    monkeypatch.setattr(telegram.request, 'HTTPXRequest', lambda **kwargs: None)
    return calls


def test_telegram_probe_reads_identity_webhook_and_closes(settings, monkeypatch):
    calls = telegram_transport(monkeypatch)
    report = diagnostics.check_bots('telegram')
    assert report['bots']['telegram']['status'] == 'ok'
    assert report['bots']['telegram']['configured_username_matches'] is True
    assert calls == ['getMe', 'getWebhookInfo', 'close']
    assert_no_private_values(report)


def test_telegram_webhook_is_never_deleted_or_disclosed(settings, monkeypatch):
    calls = telegram_transport(monkeypatch, url='https://private.invalid/secret')
    report = diagnostics.check_bots('telegram')
    assert report['bots']['telegram']['error_code'] == 'active_webhook'
    assert calls == ['getMe', 'getWebhookInfo', 'close']
    assert_no_private_values(report)


@pytest.mark.parametrize('error,code', [(TimedOut('private-exception'), 'timeout'),
                                      (InvalidToken('private-exception'), 'unauthorized')])
def test_telegram_failure_does_not_echo_token_bearing_error(settings, monkeypatch, error, code):
    telegram_transport(monkeypatch, error=error)
    report = diagnostics.check_bots('telegram')
    assert report['bots']['telegram']['error_code'] == code
    assert report['bots']['telegram']['stage'] == 'get_me'
    assert_no_private_values(report)


def test_telegram_nested_transport_cause_is_used_without_stringifying():
    error = TimedOut('private-exception')
    error.__cause__ = httpx.ReadTimeout('https://private.invalid/synthetic-telegram-secret')
    error.diagnostic_stage = 'initialize'
    report = diagnostics.error_details('telegram', error)
    assert report['error_code'] == 'read_timeout'
    assert report['stage'] == 'initialize'
    assert_no_private_values(report)


def test_disabled_or_missing_token_never_calls_network(settings, monkeypatch):
    calls = max_transport(monkeypatch, [])
    settings.MAX_BOT_ENABLED = settings.BOT_ENABLED = False
    assert all(item['status'] == 'skipped' for item in diagnostics.check_bots()['bots'].values())
    settings.MAX_BOT_ENABLED = True
    settings.MAX_BOT_TOKEN = ''
    assert diagnostics.check_bots('max')['bots']['max']['error_code'] == 'token_missing'
    assert calls == []


def test_check_bots_cli_reports_error_with_nonzero_exit(settings, monkeypatch, capsys):
    import manage
    import sys
    settings.MAX_BOT_TOKEN = ''
    monkeypatch.setattr(sys, 'argv', ['manage.py', 'check-bots', '--platform', 'max'])
    with pytest.raises(SystemExit) as error:
        manage.main()
    assert error.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output['bots']['max']['error_code'] == 'token_missing'
