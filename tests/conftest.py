"""Offline tests must never touch credentials, CRM, bots or physical hardware."""
import os
import ipaddress
from pathlib import Path
import tempfile
import socket
import sys
import pytest

_TEST_STATE = tempfile.TemporaryDirectory(prefix='gatebot-tests-')
# Persisted installation settings must not override these offline guards.
# The production loader remains unchanged outside this pytest process.
import system_config_bootstrap
_production_settings_loader = system_config_bootstrap.apply_startup_overrides
def _offline_settings_loader(project_dir, environment=None):
    return _production_settings_loader(Path(_TEST_STATE.name)/'bootstrap', environment)
system_config_bootstrap.apply_startup_overrides = _offline_settings_loader
os.environ['GATEBOT_DATA_DIR'] = _TEST_STATE.name
os.environ['GATEBOT_LOG_DIR'] = str(Path(_TEST_STATE.name) / 'logs')
os.environ['SECRET_KEY'] = 'test-only-session-key-with-at-least-32-characters'
os.environ['GATEBOT_ENV'] = 'development'
os.environ['DEV_REGISTRATION_SMS_REDIRECT_ENABLED'] = 'false'
os.environ['DEV_REGISTRATION_SMS_PREFIX'] = ''
os.environ['DEV_REGISTRATION_SMS_PHONE'] = ''
for name in ('BOT_ENABLED','MAX_BOT_ENABLED','YCLIENTS_ENABLED','SCHEDULER_ENABLED','OUTBOUND_ENABLED','SMTP_ENABLED','SIM800_ENABLED'):
    os.environ[name] = 'false'
os.environ['GATE_DRY_RUN'] = 'true'
for name in ('BOT_TOKEN','MAX_BOT_TOKEN','YC_PARTNER_TOKEN','YC_USER_TOKEN','MAILING_SMTP_PASSWORD'):
    os.environ[name] = ''
os.environ['BOOTSTRAP_SUPERADMIN_PHONE'] = ''
os.environ['YC_MARKETPLACE_CONNECT_ENABLED'] = 'false'
os.environ['YC_MARKETPLACE_APPLICATION_ID'] = ''
os.environ['YC_MARKETPLACE_SYSTEM_USER_TOKEN'] = ''

@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    # Windows implements socketpair() using a loopback TCP connection. asyncio
    # needs that local pipe even when every API transport is a fake.
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def check_host(host):
        if host is None:
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except (ValueError, TypeError):
            pass
        raise AssertionError('External network access is forbidden in offline tests')

    def check_address(sock, address):
        if sock.family == getattr(socket, 'AF_UNIX', None):
            return
        if not isinstance(address, tuple) or not address:
            raise AssertionError('External network access is forbidden in offline tests')
        check_host(address[0])

    def connect(sock, address):
        check_address(sock, address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check_address(sock, address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        check_host(host)
        return original_getaddrinfo(host, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError('External network access is forbidden in offline tests')

    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)
    monkeypatch.setattr(socket, 'getaddrinfo', getaddrinfo)
    monkeypatch.setattr(socket, 'create_connection', forbidden)

    if sys.platform == 'win32':
        # Proactor uses ConnectEx directly and bypasses socket.connect. Keep
        # numeric external addresses blocked without replacing the Windows loop.
        from asyncio.windows_events import IocpProactor
        original_proactor_connect = IocpProactor.connect

        def proactor_connect(proactor, sock, address):
            check_address(sock, address)
            return original_proactor_connect(proactor, sock, address)

        monkeypatch.setattr(IocpProactor, 'connect', proactor_connect)
