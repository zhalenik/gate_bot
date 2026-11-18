"""MAX admin delivery requires an explicit account binding, not a client ID."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from max_api import MaxApiError
from storage import config
from test_product_integration import product


def enable_max(identity, username='supertest', event='sms_received'):
    identity.save_admin_preferences(username, {'events': {event: {'max': True}}})


@pytest.fixture
def max_transport(monkeypatch):
    import max_bot
    api = Mock()
    ready = threading.Event()
    ready.set()
    monkeypatch.setattr(max_bot, '_READY', ready)
    monkeypatch.setattr(max_bot, '_RUNTIME', SimpleNamespace(api=api))
    monkeypatch.setattr(config, 'OUTBOUND_ENABLED', True)
    return api


def test_client_max_id_does_not_enable_admin_delivery_until_verified(product, max_transport):
    p = product
    identity, out = p.services.identity, p.services.notifications
    # Reproduce the existing-account case: MAX is present on the client card,
    # but no administrator has yet followed the private binding link.
    identity.attach_console_account('supertest', '+70000000002', client_token=p.token)
    p.links.bind_identity('max', '92', token=p.token)
    enable_max(identity)
    prefs = identity.admin_preferences('supertest')
    assert prefs['events']['sms_received']['max'] is True
    assert prefs['channels']['max'] is False
    assert identity.bot_admin('max', '92') is None
    out.event('sms_received', None, {'event_id': 'before-binding', 'text': 'Example incoming SMS'})
    with out.store.connect() as db:
        assert db.execute('SELECT count(*) FROM message_outbox WHERE channel="max"').fetchone()[0] == 0

    ticket = identity.issue_bind_ticket('supertest', 'max')
    identity.bind_ticket('max', '92', ticket)
    assert identity.admin_preferences('supertest')['channels']['max'] is True
    assert identity.bot_admin('max', '92')['is_super'] is True
    out.event('sms_received', None, {'event_id': 'after-binding', 'text': 'Example incoming SMS'})
    out.process_once()
    max_transport.send_message.assert_called_once_with(user_id=92, text='Входящая SMS\nExample incoming SMS')
    with out.store.connect() as db:
        assert db.execute('SELECT state FROM message_outbox WHERE channel="max"').fetchone()['state'] == 'accepted'
    # A repeated event or worker tick cannot cause a duplicate notification.
    out.event('sms_received', None, {'event_id': 'after-binding', 'text': 'Example incoming SMS'})
    out.process_once()
    assert max_transport.send_message.call_count == 1


def test_max_admin_binding_is_rechecked_before_delivery(product, max_transport):
    identity, out = product.services.identity, product.services.notifications
    enable_max(identity)
    identity.bind_ticket('max', '92', identity.issue_bind_ticket('supertest', 'max'))
    out.event('sms_received', None, {'event_id': 'binding-revoked'})
    with out.store.connect() as db:
        db.execute('DELETE FROM bot_bindings WHERE platform="max"')
    out.process_once()
    max_transport.send_message.assert_not_called()
    with out.store.connect() as db:
        assert db.execute('SELECT state FROM message_outbox WHERE channel="max"').fetchone()['state'] == 'skipped'


def test_max_admin_notifications_keep_branch_and_superadmin_scope(product):
    identity = product.services.identity
    for username, user_id in [('supertest', '92'), ('branchtest', '93')]:
        identity.bind_ticket('max', user_id, identity.issue_bind_ticket(username, 'max'))
        events = {'gate_requested': {'max': True}}
        if username == 'supertest':
            events['sms_received'] = {'max': True}
        else:
            with pytest.raises(PermissionError):
                enable_max(identity, username)
        identity.save_admin_preferences(username, {'events': events})
    assert {c['target'] for c in identity.contacts_for_admin_events(10, 'gate_requested')} == {'92', '93'}
    assert {c['target'] for c in identity.contacts_for_admin_events(20, 'gate_requested')} == {'92'}
    assert {c['target'] for c in identity.contacts_for_admin_events(None, 'sms_received')} == {'92'}


def test_max_outbox_keeps_safe_error_diagnostics_without_automatic_resend(product, max_transport, monkeypatch):
    import max_bot
    audit = Mock()
    monkeypatch.setattr(max_bot, '_audit', audit)
    max_transport.send_message.side_effect = MaxApiError('chat.denied', status_code=403, stage='send_message')
    out = product.services.notifications
    context = {'idempotency_key': 'rejected-max-effect'}
    result = out.send('max', '92', 'Private synthetic message', context)
    assert result == {'status': 'unknown', 'code': 'chat.denied', 'http_status': 403,
                      'stage': 'send_message', 'retryable': False}
    audit.assert_called_once_with('BOT_NOTIFICATION_ERROR', level='ERROR', api_code='chat.denied', http_status=403)
    with out.store.connect() as db:
        stored = db.execute('SELECT result FROM message_effects').fetchone()['result']
    assert json.loads(stored) == result
    assert 'Private synthetic message' not in stored
    assert out.send('max', '92', 'Private synthetic message', context)['status'] == 'unknown'
    assert max_transport.send_message.call_count == 1
