"""YC identity UI contracts in Chrome; remote lookups are simulated locally."""
import os
from unittest.mock import Mock

import pytest

from storage.sql_documents import DocumentStore, migrate_legacy, read_document, write_document
from test_ui_browser import browser_ui, sign_in
from test_web_security import web


pytestmark = pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')


@pytest.fixture
def sync_ui(web, tmp_path, monkeypatch):
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    identity = web.module.product_services().identity
    forbidden = Mock(side_effect=AssertionError('A UI contract test must not contact YCLIENTS'))
    monkeypatch.setattr('yclients_api.YClientsClient.request', forbidden)
    return web, identity, forbidden


def reply(route, **payload):
    route.fulfill(status=200, json={'status': 'success', **payload})


def test_card_refresh_preserves_drafts_requires_saved_phone_and_keeps_id_on_ambiguity(sync_ui, browser_ui, tmp_path):
    web, identity, forbidden = sync_ui
    page, origin, expect, errors = browser_ui
    web.links.update_user(web.token_a, {'id_ycl': 2468})
    original = web.links.load_links()[web.token_a]
    requests = []
    def lookup(route):
        requests.append(route.request.post_data_json)
        if len(requests) == 1:
            web.links.update_user(web.token_a, {'id_ycl': 8642})
            reply(route, result={'status': 'updated', 'id_ycl': 8642, 'message': 'ID получен из YCLIENTS.'})
        else:
            reply(route, result={'status': 'ambiguous', 'id_ycl': None, 'message': 'Найдено несколько совпадений. ID не изменён.'})
    page.route('**/admin/api/yclients/client-id', lookup)
    sign_in(page, origin, 'tenant-a')
    page.locator('#usersTableBody .client-name-link').click()
    modal = page.get_by_role('dialog', name='Редактировать пользователя', exact=True)
    refresh = modal.get_by_role('button', name='Обновить ID из YCLIENTS', exact=True)
    modal.get_by_label('Имя', exact=True).fill('Unsaved name')
    modal.get_by_label('Email', exact=True).fill('draft@example.invalid')
    modal.get_by_label('Телефон', exact=True).fill('+15550900111')
    expect(refresh).to_be_disabled()
    expect(modal.locator('[data-yclients-id-hint]')).to_contain_text('Сначала сохраните')
    assert requests == []
    modal.get_by_label('Телефон', exact=True).fill(original['phone'])
    expect(refresh).to_be_enabled()
    refresh.click()
    expect(modal.get_by_label('YCLIENTS ID', exact=True)).to_have_value('8642')
    expect(modal.get_by_label('Имя', exact=True)).to_have_value('Unsaved name')
    expect(modal.get_by_label('Email', exact=True)).to_have_value('draft@example.invalid')
    assert requests == [{'token': web.token_a}]
    saved = web.links.load_links()[web.token_a]
    assert saved['user_name'] == original['user_name'] and saved['email'] == original['email']
    refresh.click()
    expect(modal.locator('[data-yclients-id-hint]')).to_contain_text('несколько совпадений')
    expect(modal.get_by_label('YCLIENTS ID', exact=True)).to_have_value('8642')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert modal.locator('.client-yc-id-control').evaluate('(node)=>node.scrollWidth<=node.clientWidth')
    modal.locator('.modal-content').screenshot(path=str(tmp_path / 'yclients-id-card-mobile.png'))
    modal.get_by_role('button', name='Отмена', exact=True).click()
    page.locator('#usersTableBody .client-name-link').click()
    expect(page.get_by_label('YCLIENTS ID', exact=True)).to_have_value('8642')
    assert web.calls == [] and not errors
    forbidden.assert_not_called()


def test_batch_starts_only_by_click_reports_progress_and_counts(sync_ui, browser_ui, tmp_path):
    web, identity, forbidden = sync_ui
    page, origin, expect, errors = browser_ui
    started = False; reads = 0; posts = []
    def sync(route):
        nonlocal started, reads
        if route.request.method == 'POST':
            posts.append(route.request.post_data_json); started = True
            reply(route, sync={'id': 'offline-batch', 'state': 'queued', 'total': 3, 'processed': 0, 'message': 'Проверка поставлена в очередь.'})
        elif not started:
            reply(route, sync={'state': 'idle', 'total': 0, 'processed': 0})
        else:
            reads += 1
            reply(route, sync={'id': 'offline-batch', 'state': 'running' if reads == 1 else 'complete',
                               'total': 3, 'processed': 1 if reads == 1 else 3, 'updated': 1,
                               'unchanged': 0 if reads == 1 else 1, 'not_found': 0 if reads == 1 else 1})
    page.route('**/admin/api/yclients/client-ids*', sync)
    sign_in(page, origin, 'tenant-a')
    page.goto(origin + '/admin/integrations')
    panel = page.locator('#yclientsClientSyncPanel')
    start = panel.get_by_role('button', name='Обновить ID всех клиентов', exact=True)
    expect(start).to_be_enabled(); assert posts == []
    expect(page.locator('#integrationBranch').locator('..')).to_be_hidden()
    start.click()
    expect(start).to_be_disabled()
    expect(panel.locator('#yclientsClientSyncSummary')).to_contain_text('Проверено 3 из 3', timeout=10000)
    expect(start).to_be_enabled()
    expect(panel).to_contain_text('Обновлено: 1')
    expect(panel).to_contain_text('Не найдены: 1')
    assert posts == [{'branch_id': '12'}] and reads >= 2
    page.set_viewport_size({'width': 390, 'height': 844})
    panel.screenshot(path=str(tmp_path / 'yclients-id-batch-mobile.png'))
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    assert panel.evaluate('(node)=>node.scrollWidth<=node.clientWidth')
    assert web.calls == [] and not errors
    forbidden.assert_not_called()


def test_branch_switch_ignores_delayed_old_batch_and_standalone_has_no_controls(sync_ui, browser_ui):
    web, identity, forbidden = sync_ui
    page, origin, expect, errors = browser_ui
    pending = []; posts = []
    def sync(route):
        if route.request.method == 'POST':
            posts.append(route.request.post_data_json); pending.append(route)
        else:
            branch = route.request.url.rsplit('=', 1)[-1]
            reply(route, sync={'state': 'idle', 'total': 0, 'processed': 0, 'message': 'Филиал ' + branch})
    page.route('**/admin/api/yclients/client-ids*', sync)
    sign_in(page, origin)
    page.goto(origin + '/admin/integrations')
    expect(page.locator('#yclientsClientSyncStart')).to_be_enabled()
    page.locator('#yclientsClientSyncStart').click()
    page.locator('#integrationBranch').select_option('123')
    assert pending
    reply(pending.pop(), sync={'state': 'running', 'total': 99, 'processed': 1, 'message': 'Старый филиал'})
    expect(page.locator('#yclientsClientSyncStatus')).to_have_text('Филиал 123')
    expect(page.locator('#yclientsClientSyncStart')).to_be_enabled()
    expect(page.locator('#yclientsClientSyncSummary')).not_to_contain_text('99')
    assert posts == [{'branch_id': '12'}]
    with identity.product.connect() as db:
        branches = read_document(db, 'branches.json')
        branches['12']['yclients_enabled'] = False
        write_document(db, 'branches.json', branches)
    sign_in(page, origin, 'tenant-a')
    page.locator('#usersTableBody .client-name-link').click()
    expect(page.get_by_role('button', name='Обновить ID из YCLIENTS', exact=True)).to_have_count(0)
    expect(page.get_by_label('YCLIENTS ID', exact=True)).to_have_count(0)
    page.goto(origin + '/admin/integrations')
    expect(page.locator('#yclientsClientSyncPanel')).to_have_count(0)
    assert web.calls == [] and not errors
    forbidden.assert_not_called()
