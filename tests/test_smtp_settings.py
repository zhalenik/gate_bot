from unittest.mock import MagicMock

import pytest
from flask import Flask, session

from test_identity_product import identities
from smtp_settings import SmtpSettings, smtp_connection
from smtp_web import create_blueprint
from storage import config


VALUES = {'enabled': True, 'host': 'smtp.example.invalid', 'port': 465, 'security': 'ssl',
          'username': 'sender@example.invalid', 'password': 'synthetic-smtp-password',
          'from_name': 'GateBot', 'from_email': 'sender@example.invalid'}


def test_global_settings_encrypt_password_and_preserve_it_on_blank(identities):
    settings = SmtpSettings(identities.service)
    public = settings.save('owner', VALUES)
    assert public['has_password'] and 'password' not in public
    assert 'synthetic-smtp-password' not in str(public)
    assert settings.effective()['password'] == VALUES['password']
    settings.save('owner', {'from_name': 'Updated sender', 'password': ''})
    assert settings.effective()['password'] == VALUES['password']
    with settings.store.connect() as db:
        ciphertext = db.execute('SELECT ciphertext FROM application_settings WHERE name="smtp"').fetchone()[0]
    assert VALUES['password'] not in ciphertext and VALUES['host'] not in ciphertext
    settings.save('owner', {'enabled': False, 'clear_password': True})
    assert not settings.public('owner')['has_password']


def test_environment_is_fallback_until_first_saved_configuration(identities, monkeypatch):
    monkeypatch.setattr(config, 'MAILING_SMTP_SERVER', 'existing.example.invalid')
    monkeypatch.setattr(config, 'MAILING_SMTP_PASSWORD', 'existing-private-test-value')
    settings = SmtpSettings(identities.service)
    assert settings.public('owner')['source'] == 'environment'
    assert settings.effective()['host'] == 'existing.example.invalid'
    settings.save('owner', VALUES)
    monkeypatch.setattr(config, 'MAILING_SMTP_SERVER', 'different.example.invalid')
    assert settings.effective()['host'] == VALUES['host']
    assert config.MAILING_SMTP_PASSWORD == 'existing-private-test-value'


def test_branch_admin_cannot_read_write_or_check_global_smtp(identities, monkeypatch):
    settings = SmtpSettings(identities.service)
    opened = MagicMock()
    monkeypatch.setattr('smtp_settings.smtp_connection', opened)
    for action in (lambda: settings.public('north'), lambda: settings.save('north', VALUES), lambda: settings.check('north')):
        with pytest.raises(PermissionError):
            action()
    opened.assert_not_called()
    with settings.store.connect() as db:
        assert db.execute('SELECT count(*) FROM application_settings').fetchone()[0] == 0


@pytest.mark.parametrize('change', [
    {'host': 'https://smtp.example.invalid'}, {'from_name': 'A\r\nB'},
    {'port': True}, {'security': 'none'}, {'from_email': 'Name <test@example.invalid>'},
    {'clear_password': True}, {'password': None}, {'unexpected': 'private'},
])
def test_invalid_settings_do_not_overwrite_working_configuration(identities, change):
    settings = SmtpSettings(identities.service)
    settings.save('owner', VALUES)
    with pytest.raises(ValueError):
        settings.save('owner', change)
    assert settings.effective() == VALUES


def test_starttls_authentication_happens_only_after_verified_tls(monkeypatch):
    server = MagicMock()
    factory = MagicMock()
    factory.return_value.__enter__.return_value = server
    monkeypatch.setattr('smtp_settings.smtplib.SMTP', factory)
    with smtp_connection({**VALUES, 'security': 'starttls', 'port': 587}) as connection:
        assert connection is server
    names = [call[0] for call in server.mock_calls]
    assert names == ['ehlo', 'starttls', 'ehlo', 'login']
    context = server.starttls.call_args.kwargs['context']
    assert context.check_hostname and context.verify_mode == 2


def test_connection_check_sends_no_email_and_hides_server_error(identities, monkeypatch):
    settings = SmtpSettings(identities.service)
    settings.save('owner', VALUES)
    factory = MagicMock()
    server = factory.return_value.__enter__.return_value
    server.noop.return_value = (250, b'OK')
    monkeypatch.setattr('smtp_settings.smtplib.SMTP_SSL', factory)
    assert settings.check('owner')['status'] == 'success'
    server.send_message.assert_not_called()
    import smtplib
    server.login.side_effect = smtplib.SMTPAuthenticationError(535, b'SECRET PROVIDER DETAILS')
    result = settings.check('owner')
    assert result['status'] == 'error' and 'SECRET' not in str(result)


def test_send_uses_current_saved_settings_and_obeys_global_outbound(identities, monkeypatch):
    import smtp_mailing
    settings = SmtpSettings(identities.service)
    settings.save('owner', VALUES)
    monkeypatch.setattr(smtp_mailing, 'SmtpSettings', lambda: settings)
    monkeypatch.setattr(config, 'OUTBOUND_ENABLED', True)
    monkeypatch.setattr(config, 'SMTP_ENABLED', False)  # Saved UI settings override this fallback.
    factory = MagicMock()
    server = factory.return_value.__enter__.return_value
    monkeypatch.setattr('smtp_settings.smtplib.SMTP_SSL', factory)
    assert smtp_mailing.send_email('recipient@example.invalid', 'Test', '<p>Example</p>')
    message = server.send_message.call_args.args[0]
    assert str(message['From']) == 'GateBot <sender@example.invalid>'
    settings.save('owner', {'enabled': False})
    assert not smtp_mailing.send_email('recipient@example.invalid', 'Test', '<p>Example</p>')
    settings.save('owner', {'enabled': True})
    monkeypatch.setattr(config, 'OUTBOUND_ENABLED', False)
    assert not smtp_mailing.send_email('recipient@example.invalid', 'Test', '<p>Example</p>')
    assert server.send_message.call_count == 1


def test_web_enforces_superadmin_and_csrf_before_transport(identities, monkeypatch):
    app = Flask(__name__, template_folder='../templates')
    app.secret_key = 'isolated-smtp-tests-secret-key'
    app.config['TESTING'] = True
    provider = lambda: {'auth': identities.auth, 'service': identities.service}
    app.register_blueprint(create_blueprint(provider))
    @app.before_request
    def csrf():
        from flask import request, abort
        if request.method == 'POST' and request.headers.get('X-CSRF-Token') != session.get('csrf'):
            abort(403)
    client = app.test_client()
    factory = MagicMock()
    monkeypatch.setattr('smtp_settings.smtplib.SMTP_SSL', factory)
    assert client.get('/admin/api/smtp').status_code == 401
    def login(name):
        with app.test_request_context():
            admin = identities.auth.get_admin(name)
            fingerprint = identities.auth._session_fingerprint(admin)
        with client.session_transaction() as current:
            current.update(admin_logged_in=True, admin_username=name, admin_auth_version=fingerprint, csrf='test-csrf')
    login('north')
    for path in ('/admin/smtp', '/admin/api/smtp'):
        assert client.get(path).status_code == 403
    for path in ('/admin/api/smtp', '/admin/api/smtp/check'):
        assert client.post(path, json=VALUES, headers={'X-CSRF-Token':'test-csrf'}).status_code == 403
    login('owner')
    assert client.post('/admin/api/smtp', json=VALUES).status_code == 403
    response = client.post('/admin/api/smtp', json=VALUES, headers={'X-CSRF-Token':'test-csrf'})
    assert response.status_code == 200 and 'password' not in response.json['settings']
    assert client.get('/admin/api/smtp').json['settings']['host'] == VALUES['host']
    factory.assert_not_called()
