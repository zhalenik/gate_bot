"""Chrome checks for the shared palette, phone entry and audience-specific UI."""
import os

import pytest

from test_web_security import web
from test_ui_browser import browser_ui, sign_in

pytestmark = pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')


def sql_state(web, tmp_path, monkeypatch):
    from storage.sql_documents import DocumentStore, migrate_legacy
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    return web.module.product_services().identity


def test_palette_retains_pass_statuses_and_normalizes_dynamic_phone_inputs(web, browser_ui, tmp_path):
    page, origin, expect, errors = browser_ui
    page.goto(origin + '/admin/login')
    expect(page.locator('.header-signin')).to_have_count(0)
    assert page.locator('body').evaluate('node=>getComputedStyle(node).backgroundColor') == 'rgb(239, 240, 232)'
    assert page.locator('.btn-primary').evaluate('node=>getComputedStyle(node).backgroundColor') == 'rgb(211, 72, 43)'
    web.links.toggle_user_access(web.token_a, True)
    web.links.toggle_user_access(web.token_b, False)
    sign_in(page, origin)
    expect(page.locator('.btn-pass-permanent')).to_be_visible()
    assert page.locator('.btn-pass-permanent').evaluate('node=>getComputedStyle(node).backgroundColor') == 'rgb(234, 245, 238)'
    assert page.locator('.btn-pass-denied').evaluate('node=>getComputedStyle(node).backgroundColor') == 'rgb(255, 240, 241)'
    page.goto(origin + '/admin/add-user')
    phone = page.locator('#phone')
    for raw in ('8 (900) 000-00-11', '900 000 00 11', '+7 (900) 000-00-11', '7 900 000 00 11'):
        phone.fill(raw); phone.blur(); expect(phone).to_have_value('+79000000011')
    phone.fill('+44 (20) 7946-0958'); phone.blur(); expect(phone).to_have_value('+442079460958')
    phone.evaluate("input=>{input.value='8 (900) 000-00-33';input.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertFromPaste'}));}")
    expect(phone).to_have_value('+79000000033')
    page.evaluate("""() => {const form=document.createElement('form');form.id='dynamicPhoneTest';form.innerHTML='<input type="tel" name="phone"><button>Save</button>';form.onsubmit=event=>{event.preventDefault();form.dataset.sent=new FormData(form).get('phone');};document.body.append(form);} """)
    page.locator('#dynamicPhoneTest input').fill('8 (900) 000-00-22')
    page.locator('#dynamicPhoneTest').evaluate('form=>form.requestSubmit()')
    expect(page.locator('#dynamicPhoneTest')).to_have_attribute('data-sent', '+79000000022')
    page.locator('#dynamicPhoneTest').evaluate('node=>node.remove()')
    page.screenshot(path=str(tmp_path / 'palette-client-form.png'), full_page=True)
    page.goto(origin + '/admin/telephony')
    page.wait_for_selector('.connection')
    semantic = page.evaluate("""() => {const root=document.querySelector('.connection'),bars=root.querySelector('.signal-bars');root.classList.remove('error','warning');const good=getComputedStyle(bars).color;root.classList.add('error');const bad=getComputedStyle(bars).color;return {good,bad};}""")
    assert semantic == {'good': 'rgb(38, 148, 84)', 'bad': 'rgb(217, 48, 62)'}
    assert not web.calls


def test_single_branch_admin_settings_and_selectors(web, browser_ui, tmp_path, monkeypatch):
    sql_state(web, tmp_path, monkeypatch)
    web.branches.update_settings('12', {'yclients_enabled': False})
    page, origin, expect, errors = browser_ui
    sign_in(page, origin, 'tenant-a')
    expect(page.locator('#activeBranchSelect')).to_have_count(0)
    page.goto(origin + '/admin/settings')
    expect(page.locator('#settingsYclients')).to_be_hidden()
    expect(page.locator('#settingsBranchId')).to_be_hidden()
    expect(page.locator('#settingsHardwareHint')).to_have_count(0)
    expect(page.locator('#settingsButtons [data-field="phone"]').first).to_be_enabled()
    expect(page.locator('#addSettingsButton')).to_be_enabled()
    page.goto(origin + '/admin/invitations')
    expect(page.locator('input[name=phone]')).to_be_visible()
    expect(page.locator('#identityContent select[name=branch_id]')).to_have_count(0)
    page.goto(origin + '/admin/subscription')
    expect(page.locator('#billingBranch')).to_be_hidden()
    expect(page.locator('#subscriptionPeriod')).to_contain_text('Подписка действительна до:')
    web.branches.update_settings('12', {'yclients_enabled': True})
    page.goto(origin + '/admin/integrations')
    expect(page.locator('#integrationBranch')).to_be_hidden()
    page.goto(origin + '/admin/settings')
    expect(page.locator('#settingsBranchId')).to_be_visible()
    expect(page.locator('#settingsYclients')).to_be_hidden()
    assert not web.calls


def test_client_preferences_guest_access_and_landing_actions(web, browser_ui, tmp_path, monkeypatch):
    sql_state(web, tmp_path, monkeypatch)
    web.links.toggle_user_access(web.token_a, True)
    with web.links.store.transaction() as links:
        links[web.token_a]['is_temporary'] = True
    page, origin, expect, errors = browser_ui
    for path in ('/preferences/' + web.token_b, '/verify-email/offline-example-token'):
        page.goto(origin + path)
        expect(page.locator('.header-signin')).to_have_count(0)
        expect(page.locator('a.brand')).to_have_count(0)
        expect(page.get_by_role('link', name='Вход в кабинет', exact=True)).to_have_count(0)
    page.goto(origin + '/opn/' + web.token_a)
    expect(page.locator('#gateActions')).to_be_visible()
    expect(page.locator('#preferencesLink')).to_be_hidden()
    expect(page.locator('#adminContacts a')).to_have_attribute('href', 'tel:+70000000999')
    web.links.toggle_user_access(web.token_a, False)
    page.reload()
    expect(page.get_by_role('link', name='Настроить уведомления')).to_have_count(0)
    web.links.toggle_user_access(web.token_b, False)
    page.goto(origin + '/opn/' + web.token_b)
    contact = page.locator('.support-actions a').first
    preferences = page.locator('.support-preferences a')
    expect(contact).to_have_attribute('href', 'tel:+70000000999')
    expect(preferences).to_be_visible()
    assert contact.evaluate('node=>getComputedStyle(node).backgroundColor') == 'rgb(211, 72, 43)'
    assert preferences.evaluate('node=>getComputedStyle(node).backgroundColor') == 'rgb(239, 237, 246)'
    assert preferences.bounding_box()['y'] >= contact.bounding_box()['y'] + contact.bounding_box()['height'] + 20
    page.goto(origin + '/')
    expect(page.locator('.landing-actions .btn-primary')).to_have_attribute('href', '/register-branch')
    expect(page.locator('.landing-actions .btn-action')).to_have_attribute('href', '/admin/login')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(tmp_path / 'palette-landing-mobile.png'), full_page=True)
