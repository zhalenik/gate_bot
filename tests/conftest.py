"""Offline tests must never touch credentials, CRM, bots or physical hardware."""
import os
from pathlib import Path
import tempfile
import socket
import pytest

_TEST_STATE = tempfile.TemporaryDirectory(prefix='gatebot-tests-')
os.environ['GATEBOT_DATA_DIR'] = _TEST_STATE.name
os.environ['GATEBOT_LOG_DIR'] = str(Path(_TEST_STATE.name) / 'logs')
os.environ['SECRET_KEY'] = 'test-only-session-key-with-at-least-32-characters'
os.environ['GATEBOT_ENV'] = 'development'
for name in ('BOT_ENABLED','MAX_BOT_ENABLED','YCLIENTS_ENABLED','SCHEDULER_ENABLED','OUTBOUND_ENABLED','SMTP_ENABLED','SIM800_ENABLED'):
    os.environ[name] = 'false'
os.environ['GATE_DRY_RUN'] = 'true'
for name in ('BOT_TOKEN','MAX_BOT_TOKEN','YC_PARTNER_TOKEN','YC_USER_TOKEN','MAILING_SMTP_PASSWORD'):
    os.environ[name] = ''

@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Network access is forbidden in offline tests')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
