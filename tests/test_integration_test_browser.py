"""Integration test-send controls in Chrome; delivery transports stay mocked."""
import os

import pytest

from messenger_service import MessengerService
from storage import config
from storage.sql_documents import read_document, write_document
from test_integration_test_delivery import test_delivery_web, messenger_web, web
from test_ui_browser import browser_ui, sign_in


pytestmark = pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')


def send_from_form(page):
    with page.expect_response(lambda response: response.url.endswith('/admin/api/integration-test') and response.request.method == 'POST') as sent:
        page.locator('#integrationTestSend').click()
    assert sent.value.status == 200
    return sent.value.request.post_data_json, sent.value.json()['delivery']


def test_manual_and_client_chatpush_then_yclients_use_exact_recipients(test_delivery_web, browser_ui):
    state = test_delivery_web
    page, origin, expect, errors = browser_ui
    MessengerService(state.identity).save('tenant-a', '12', 'yclients')
    before = state.web.links.load_links()
    sign_in(page, origin, 'tenant-a')
    page.goto(origin + '/admin/integrations')
    panel = page.locator('#integrationTestPanel')
    expect(panel).to_be_visible()
    expect(page.locator('#integrationBranch').locator('..')).to_be_hidden()
    page.locator('#integrationTestPhone').fill('89990000101')
    page.locator('#integrationTestText').fill('Проверка ручного номера')
    expect(page.locator('#integrationTestSend')).to_be_enabled()
    payload, result = send_from_form(page)
    assert payload['phone'] == '+79990000101' and payload['provider'] == 'chatpush'
    assert 'client_token' not in payload and 'yclients_id' not in payload
    assert result['status'] == 'accepted' and state.cp.call_count == 1
    assert page.locator('#integrationTestStatus').text_content()
    page.locator('#integrationTestRecipientMode').select_option('client')
    page.locator('#integrationTestClientSearch').fill('Client A')
    page.locator('#integrationTestClient').select_option(state.web.token_a)
    payload_client, result = send_from_form(page)
    assert payload_client['client_token'] == state.web.token_a and 'phone' not in payload_client
    assert payload_client['request_id'] != payload['request_id'] and state.cp.call_count == 2
    page.locator('#integrationTestProvider').select_option('yclients')
    expect(page.locator('#integrationTestRecipientMode')).to_be_hidden()
    expect(page.locator('#integrationTestPhone')).to_be_hidden()
    expect(page.locator('#integrationTestClient option[value="' + state.web.token_a + '"]')).to_have_count(1)
    expect(page.locator('#integrationTestClient option[value="' + state.token_no_yc + '"]')).to_have_count(0)
    expect(page.locator('#integrationTestClient option[value="' + state.web.token_b + '"]')).to_have_count(0)
    page.locator('#integrationTestClient').select_option(state.web.token_a)
    payload_yc, result = send_from_form(page)
    assert payload_yc['provider'] == 'yclients' and payload_yc['client_token'] == state.web.token_a
    assert 'phone' not in payload_yc and 'yclients_id' not in payload_yc and 'client_id' not in payload_yc
    assert result['status'] == 'accepted' and state.yc.call_count == 1
    assert state.web.links.load_links() == before
    assert MessengerService(state.identity).settings('tenant-a', '12')['selected'] == 'yclients'
    assert state.web.calls == [] and not errors


def test_uncertain_result_keeps_request_id_and_never_retries_automatically(test_delivery_web, browser_ui):
    state = test_delivery_web
    page, origin, expect, errors = browser_ui
    state.cp.return_value = {'status': 'unknown', 'code': 'transport_error', 'confirmed': False}
    sign_in(page, origin, 'tenant-a')
    page.goto(origin + '/admin/integrations')
    page.locator('#integrationTestPhone').fill('+15550900001')
    expect(page.locator('#integrationTestSend')).to_be_enabled()
    first, delivery = send_from_form(page)
    assert delivery['status'] == 'unknown'
    expect(page.locator('#integrationTestSend')).to_have_text('Проверить результат')
    page.wait_for_timeout(300)
    assert state.cp.call_count == 1
    second, delivery = send_from_form(page)
    assert second['request_id'] == first['request_id'] and state.cp.call_count == 1
    conflicts = []
    def conflict(route):
        conflicts.append(route.request.post_data_json)
        route.fulfill(status=409, json={'status': 'error', 'message': 'Данные получателя изменились. Выберите его заново.'})
    page.route('**/admin/api/integration-test', conflict)
    for _ in range(2):
        with page.expect_response(lambda response: response.url.endswith('/admin/api/integration-test') and response.request.method == 'POST') as rejected:
            page.locator('#integrationTestSend').click()
        assert rejected.value.status == 409
        expect(page.locator('#integrationTestStatus')).to_contain_text('Выберите его заново')
    assert all(item['request_id'] == first['request_id'] for item in conflicts)
    assert state.cp.call_count == 1
    page.unroute('**/admin/api/integration-test', conflict)
    page.locator('#integrationTestText').fill('Другой самостоятельный тест')
    third, delivery = send_from_form(page)
    assert third['request_id'] != first['request_id'] and state.cp.call_count == 2
    assert state.web.calls == [] and not errors


def test_double_submission_is_disabled_and_branch_switch_clears_recipient(test_delivery_web, browser_ui, tmp_path):
    state = test_delivery_web
    page, origin, expect, errors = browser_ui
    sign_in(page, origin)
    page.goto(origin + '/admin/integrations')
    page.locator('#integrationTestPhone').fill('+15550900002')
    expect(page.locator('#integrationTestSend')).to_be_enabled()
    held = []
    def hold(route):
        if route.request.method == 'POST':
            held.append(route)
        else:
            route.continue_()
    page.route('**/admin/api/integration-test', hold)
    page.locator('#integrationTestSend').click()
    expect(page.locator('#integrationTestSend')).to_be_disabled()
    assert len(held) == 1
    page.locator('#integrationTestPanel form').dispatch_event('submit')
    assert len(held) == 1 and state.cp.call_count == 0
    with page.expect_response(lambda response: response.url.endswith('/admin/api/integration-test') and response.request.method == 'POST') as sent:
        held[0].continue_()
    assert sent.value.status == 200
    expect(page.locator('#integrationTestSend')).to_be_enabled()
    assert state.cp.call_count == 1
    page.unroute('**/admin/api/integration-test', hold)
    page.locator('#integrationBranch').select_option('123')
    expect(page.locator('#integrationTestPhone')).to_have_value('')
    page.locator('#integrationTestRecipientMode').select_option('client')
    expect(page.locator('#integrationTestClient option[value="' + state.web.token_b + '"]')).to_have_count(1)
    expect(page.locator('#integrationTestClient option[value="' + state.web.token_a + '"]')).to_have_count(0)
    expect(page.locator('#integrationTestSend')).to_be_disabled()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.locator('#integrationTestPanel').evaluate('(node) => node.scrollWidth <= node.clientWidth')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#integrationTestPanel').screenshot(path=str(tmp_path / 'integration-test-mobile.png'))
    assert state.web.calls == [] and not errors


def test_disabled_delivery_and_standalone_never_offer_an_active_send(test_delivery_web, browser_ui, monkeypatch):
    state = test_delivery_web
    page, origin, expect, errors = browser_ui
    monkeypatch.setattr(config, 'OUTBOUND_ENABLED', False)
    sign_in(page, origin, 'tenant-a')
    page.goto(origin + '/admin/integrations')
    page.locator('#integrationTestPhone').fill('+15550900003')
    expect(page.locator('#integrationTestSend')).to_be_disabled()
    expect(page.locator('#integrationTestConnection')).not_to_have_text('Проверяем подключение…')
    with state.identity.product.connect() as db:
        branches = read_document(db, 'branches.json')
        branches['12']['yclients_enabled'] = False
        write_document(db, 'branches.json', branches)
    page.reload()
    expect(page.locator('#integrationTestPanel')).to_have_count(0)
    state.cp.assert_not_called()
    state.yc.assert_not_called()
    assert state.web.calls == [] and not errors
