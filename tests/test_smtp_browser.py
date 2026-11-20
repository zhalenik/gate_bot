"""Synthetic-browser acceptance of installation-wide SMTP settings."""
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from test_ui_browser import web, browser_ui, sign_in


pytestmark = pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')


def test_smtp_configuration_save_password_retention_and_super_scope(web, browser_ui, tmp_path, monkeypatch):
    from smtp_settings import SmtpSettings
    from storage.sql_documents import DocumentStore, migrate_legacy
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path/'admins.json'))
    identity = web.module.product_services().identity
    factory = MagicMock()
    monkeypatch.setattr('smtp_settings.smtplib.SMTP_SSL', factory)
    monkeypatch.setattr('smtp_settings.smtplib.SMTP', factory)
    page, origin, expect, errors = browser_ui
    sign_in(page, origin)
    response = page.goto(origin+'/admin/smtp')
    assert response.status == 200
    expect(page.locator('[data-shared-header]')).to_have_count(1)
    expect(page.locator('#smtpForm button[type=submit]')).to_be_enabled()
    page.get_by_label('Отправлять email', exact=True).check()
    page.get_by_label('SMTP-сервер', exact=True).fill('smtp.example.invalid')
    page.get_by_label('Защищённое соединение', exact=True).select_option('starttls')
    expect(page.get_by_label('Порт', exact=True)).to_have_value('587')
    page.get_by_label('Логин', exact=True).fill('sender@example.invalid')
    page.get_by_label('Пароль приложения', exact=True).fill('browser-synthetic-password')
    page.get_by_label('Имя отправителя', exact=True).fill('GateBot Test')
    page.get_by_label('Email отправителя', exact=True).fill('sender@example.invalid')
    with page.expect_response(lambda r:r.url.endswith('/admin/api/smtp') and r.request.method == 'POST') as saved:
        page.get_by_role('button', name='Сохранить', exact=True).click()
    assert saved.value.status == 200 and 'password' not in saved.value.json()['settings']
    expect(page.locator('#smtpNotice')).to_have_text('Настройки email сохранены')
    expect(page.get_by_label('Пароль приложения', exact=True)).to_have_value('')
    page.get_by_label('Имя отправителя', exact=True).fill('Updated GateBot')
    with page.expect_response(lambda r:r.url.endswith('/admin/api/smtp') and r.request.method == 'POST'):
        page.get_by_role('button', name='Сохранить', exact=True).click()
    expect(page.locator('#smtpPasswordHint')).to_contain_text('Пароль сохранён')
    assert SmtpSettings(identity).effective()['password'] == 'browser-synthetic-password'
    screenshots = Path.cwd()/'private'/'browser-checks'
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots/'smtp-desktop.png'), full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(screenshots/'smtp-mobile.png'), full_page=True)
    sign_in(page, origin, 'tenant-a')
    expect(page.locator('a[href="/admin/smtp"]')).to_have_count(0)
    assert page.goto(origin+'/admin/smtp').status == 403
    assert page.request.get(origin+'/admin/api/smtp').status == 403
    factory.assert_not_called()
