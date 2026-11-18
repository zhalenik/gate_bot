"""Real Chrome palette preview/publish/recovery with isolated state and no sends."""
import os
import json
from pathlib import Path

import pytest

from test_ui_browser import web, browser_ui, sign_in
from test_palette_settings import color_entry
from storage.sql_documents import DocumentStore, migrate_legacy


pytestmark = pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')


def select_color(page, entry):
    page.locator('#paletteSearch').fill(entry['id'])
    card = page.locator('[data-color-id="' + entry['id'] + '"]')
    card.wait_for(state='visible')
    return card


def color(locator, property_name='backgroundColor'):
    return locator.evaluate('(node, key) => getComputedStyle(node)[key]', property_name)


def test_super_palette_preview_save_public_application_alpha_reset_and_emergency_view(web, browser_ui, tmp_path, monkeypatch):
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    page, origin, expect, errors = browser_ui
    sign_in(page, origin)
    assert page.goto(origin + '/admin/palette').status == 200
    expect(page.locator('#paletteColors')).to_have_attribute('aria-busy', 'false')
    view = page.request.get(origin + '/admin/api/palette').json()
    accent = color_entry(view)
    card = select_color(page, accent)
    card.locator('details').click()
    expect(card).to_contain_text('theme.css')
    expect(card).to_contain_text('--accent')
    preview_button = page.locator('#palettePreviewSamples .btn-primary')
    assert color(preview_button) == 'rgb(211, 72, 43)'
    card.locator('[data-field=hex]').fill('#123456')
    card.locator('[data-field=hex]').blur()
    expect(page.locator('#paletteSave')).to_be_enabled()
    expect(preview_button).to_have_css('background-color', 'rgb(18, 52, 86)')
    assert page.request.get(origin + '/admin/api/palette').json()['overrides'] == {}
    page.locator('#palettePreviewEnabled').uncheck()
    expect(preview_button).to_have_css('background-color', 'rgb(211, 72, 43)')
    page.locator('#palettePreviewEnabled').check()
    page.locator('#paletteDiscard').click()
    expect(preview_button).to_have_css('background-color', 'rgb(211, 72, 43)')
    expect(page.locator('#paletteSave')).to_be_disabled()

    card = select_color(page, accent)
    card.locator('[data-field=hex]').fill('#123456')
    card.locator('[data-field=hex]').blur()
    with page.expect_response(lambda r: r.url.endswith('/admin/api/palette') and r.request.method == 'POST') as saved:
        page.locator('#paletteSave').click()
    assert saved.value.status == 200
    expect(page.locator('#paletteSave')).to_be_disabled()
    page.reload()
    expect(page.locator('#paletteColors')).to_have_attribute('aria-busy', 'false')
    expect(page.locator('#palettePreviewSamples .btn-primary')).to_have_css('background-color', 'rgb(18, 52, 86)')
    page.goto(origin + '/')
    expect(page.locator('.landing-actions .btn-primary')).to_have_css('background-color', 'rgb(18, 52, 86)')
    page.goto(origin + '/admin/add-user')
    expect(page.locator('button.btn-primary').first).to_have_css('background-color', 'rgb(18, 52, 86)')

    page.goto(origin + '/admin/palette?defaults=1')
    expect(page.locator('#paletteColors')).to_have_attribute('aria-busy', 'false')
    expect(page.locator('#palettePreviewEnabled')).not_to_be_checked()
    expect(page.locator('#palettePreviewSamples .btn-primary')).to_have_css('background-color', 'rgb(211, 72, 43)')
    assert page.request.get(origin + '/admin/api/palette').json()['overrides'][accent['id']] == '#123456'
    page.goto(origin + '/admin/palette')
    expect(page.locator('#paletteColors')).to_have_attribute('aria-busy', 'false')
    card = select_color(page, accent)
    card.locator('[data-field=alpha]').fill('25')
    card.locator('[data-field=alpha]').blur()
    with page.expect_response(lambda r: r.url.endswith('/admin/api/palette') and r.request.method == 'POST') as alpha:
        page.locator('#paletteSave').click()
    assert alpha.value.status == 200
    assert page.request.get(origin + '/admin/api/palette').json()['overrides'][accent['id']] == '#12345640'

    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    screenshots = Path.cwd() / 'private' / 'browser-checks'
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / 'palette-mobile.png'), full_page=True)
    page.set_viewport_size({'width': 1360, 'height': 900})
    page.screenshot(path=str(screenshots / 'palette-desktop.png'), full_page=True)
    page.locator('#paletteResetAll').click()
    page.locator('#paletteConfirmReset').click()
    assert page.request.get(origin + '/admin/api/palette').json()['overrides']
    with page.expect_response(lambda r: r.url.endswith('/admin/api/palette/reset') and r.request.method == 'POST'):
        page.locator('#paletteSave').click()
    assert page.request.get(origin + '/admin/api/palette').json()['overrides'] == {}
    expect(page.locator('#palettePreviewSamples .btn-primary')).to_have_css('background-color', 'rgb(211, 72, 43)')
    assert not web.calls
    sign_in(page, origin, 'tenant-a')
    expect(page.locator('a[href="/admin/palette"]')).to_have_count(0)
    assert page.goto(origin + '/admin/palette').status == 403
    assert page.request.get(origin + '/admin/api/palette').status == 403
    assert not errors


def test_palette_config_import_export_and_conflict_preserve_unsaved_work(web, browser_ui, tmp_path, monkeypatch):
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    page, origin, expect, errors = browser_ui
    sign_in(page, origin)
    page.goto(origin + '/admin/palette')
    expect(page.locator('#paletteColors')).to_have_attribute('aria-busy', 'false')
    model = page.request.get(origin + '/admin/api/palette').json()
    accent, ink = color_entry(model), color_entry(model, '--ink')
    config = {'format': 'gatebot-palette-v1', 'catalog_version': model['catalog_version'],
              'overrides': {accent['id']: '#224466'}}
    def upload(value):
        page.locator('#paletteImportFile').set_input_files({
            'name': 'palette.json', 'mimeType': 'application/json', 'buffer': json.dumps(value).encode()})
    upload(config)
    expect(page.locator('#paletteNotice')).to_contain_text('Конфиг загружен в черновик')
    card = select_color(page, accent)
    expect(card.locator('[data-field=hex]')).to_have_value('#224466')
    assert page.request.get(origin + '/admin/api/palette').json()['overrides'] == {}
    with page.expect_download() as downloaded:
        page.locator('#paletteExport').click()
    data = json.loads(Path(downloaded.value.path()).read_text(encoding='utf-8'))
    assert downloaded.value.suggested_filename == 'gatebot-palette.json' and data == config
    upload({**config, 'overrides': {accent['id']: 'url(https://example.invalid/pixel)'}})
    expect(page.locator('#paletteNotice')).to_contain_text('неверное HEX')
    expect(card.locator('[data-field=hex]')).to_have_value('#224466')
    upload({**config, 'overrides': {'unknown-color': '#FFFFFF'}})
    expect(page.locator('#paletteNotice')).to_contain_text('неизвестный цвет')
    expect(card.locator('[data-field=hex]')).to_have_value('#224466')
    page.locator('#palettePreviewEnabled').uncheck()
    csrf = page.locator('meta[name=csrf-token]').get_attribute('content')
    concurrent = page.request.post(origin + '/admin/api/palette', data={
        'revision': model['revision'], 'catalog_version': model['catalog_version'],
        'overrides': {ink['id']: '#121212'}}, headers={'X-CSRF-Token': csrf})
    assert concurrent.status == 200
    with page.expect_response(lambda r: r.url.endswith('/admin/api/palette') and r.request.method == 'POST') as conflict:
        page.locator('#paletteSave').click()
    assert conflict.value.status == 409
    expect(page.locator('#paletteNotice')).to_contain_text('другом окне')
    expect(card.locator('[data-field=hex]')).to_have_value('#224466')
    assert page.request.get(origin + '/admin/api/palette').json()['overrides'] == {ink['id']: '#121212'}
    page.once('dialog', lambda dialog: dialog.accept())
    page.locator('.palette-conflict-reload').click()
    expect(page.locator('#paletteNotice')).to_contain_text('Сохранённая палитра загружена')
    expect(select_color(page, accent).locator('[data-field=hex]')).to_have_value(accent['default'])
    expect(page.locator('#paletteSave')).to_be_disabled()
    expect(page.locator('#palettePage h1')).to_have_css('color', 'rgb(18, 18, 18)')
    assert not web.calls and not errors


def test_linked_color_swatches_follow_parent_without_creating_an_override(web, browser_ui, tmp_path, monkeypatch):
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    page, origin, expect, errors = browser_ui
    sign_in(page, origin)
    page.goto(origin + '/admin/palette')
    expect(page.locator('#paletteColors')).to_have_attribute('aria-busy', 'false')
    model = page.request.get(origin + '/admin/api/palette').json()
    accent, blue = color_entry(model), color_entry(model, '--blue')
    assert blue['source_value'] == 'var(--accent)'
    page.locator('#paletteCategory').select_option('Основная палитра')
    parent = page.locator('[data-color-id="' + accent['id'] + '"]')
    alias = page.locator('[data-color-id="' + blue['id'] + '"]')
    # Keep both existing cards mounted: a filtered re-render could hide a stale
    # swatch bug by calculating the dependent value only when it reappears.
    parent.locator('[data-field=hex]').fill('#2468AC')
    parent.locator('[data-field=hex]').blur()
    expect(alias.locator('[data-field=hex]')).to_have_value('#2468AC')
    expect(alias.locator('.palette-swatch span')).to_have_css('background-color', 'rgb(36, 104, 172)')
    expect(alias).to_have_attribute('data-configured', 'false')
    with page.expect_response(lambda r: r.url.endswith('/admin/api/palette') and r.request.method == 'POST'):
        page.locator('#paletteSave').click()
    saved = page.request.get(origin + '/admin/api/palette').json()
    current_alias = next(row for row in saved['entries'] if row['id'] == blue['id'])
    assert current_alias['current'] == '#2468AC' and current_alias['overridden'] is False
    assert saved['overrides'] == {accent['id']: '#2468AC'}

    alias.locator('[data-field=hex]').fill('#ABCDEF')
    alias.locator('[data-field=hex]').blur()
    expect(alias).to_have_attribute('data-configured', 'true')
    with page.expect_response(lambda r: r.url.endswith('/admin/api/palette') and r.request.method == 'POST'):
        page.locator('#paletteSave').click()
    saved = page.request.get(origin + '/admin/api/palette').json()
    assert saved['overrides'] == {accent['id']: '#2468AC', blue['id']: '#ABCDEF'}
    parent.locator('[data-field=hex]').fill('#13579B')
    parent.locator('[data-field=hex]').blur()
    expect(alias.locator('[data-field=hex]')).to_have_value('#ABCDEF')
    assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--blue').trim().toUpperCase()") == '#ABCDEF'

    alias.locator('[data-action=reset-color]').click()
    expect(alias.locator('[data-field=hex]')).to_have_value('#13579B')
    expect(alias.locator('.palette-swatch span')).to_have_css('background-color', 'rgb(19, 87, 155)')
    expect(alias).to_have_attribute('data-configured', 'false')
    with page.expect_response(lambda r: r.url.endswith('/admin/api/palette') and r.request.method == 'POST'):
        page.locator('#paletteSave').click()
    assert page.request.get(origin + '/admin/api/palette').json()['overrides'] == {accent['id']: '#13579B'}
    assert not web.calls and not errors
