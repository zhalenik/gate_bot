"""Offline acceptance of installation-wide palette configuration and boundaries."""
import pytest

from test_identity_product import identities
from test_web_security import web, login, post
from storage.sql_documents import DocumentStore, migrate_legacy
from palette_settings import PaletteSettings, PaletteConflict


def color_entry(view, property_name='--accent'):
    return next(entry for entry in view['entries'] if entry['editable'] and
                entry['locations'][0]['property'] == property_name and
                entry['locations'][0]['file'].endswith('theme.css'))


def payload(view, overrides):
    return {'revision': view['revision'], 'catalog_version': view['catalog_version'], 'overrides': overrides}


@pytest.fixture
def palette_web(web, tmp_path, monkeypatch):
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    web.module.product_services()
    return web


def test_saved_palette_persists_and_only_custom_values_are_public_css(identities):
    settings = PaletteSettings(identities.service)
    original = settings.public('owner')
    entry = color_entry(original)
    assert original['revision'] == 0 and original['overrides'] == {}
    assert entry['css_var'] not in settings.stylesheet()
    updated = settings.save('owner', payload(original, {entry['id']: '#123456'}))
    assert updated['revision'] == 1 and updated['overrides'] == {entry['id']: '#123456'}
    current = next(row for row in updated['entries'] if row['id'] == entry['id'])
    assert current['current'] == '#123456' and current['overridden'] is True
    restored = PaletteSettings(identities.service)
    assert restored.public('owner')['overrides'] == updated['overrides']
    css = restored.stylesheet()
    assert entry['css_var'] in css and '#123456' in css
    assert all(value not in css for value in ('owner', 'North', 'source_value', 'locations', 'file', '@import', 'url('))
    assert not identities.sent


def test_full_draft_save_and_reset_remove_previous_overrides(identities):
    settings = PaletteSettings(identities.service)
    view = settings.public('owner')
    accent, ink = color_entry(view), color_entry(view, '--ink')
    saved = settings.save('owner', payload(view, {accent['id']: '#112233', ink['id']: '#33445588'}))
    replaced = settings.save('owner', payload(saved, {ink['id']: '#33445588'}))
    assert replaced['overrides'] == {ink['id']: '#33445588'}
    assert accent['css_var'] not in settings.stylesheet()
    reset = settings.reset('owner', {'revision': replaced['revision'], 'catalog_version': replaced['catalog_version']})
    assert reset['overrides'] == {} and reset['revision'] > replaced['revision']
    assert '#33445588' not in settings.stylesheet()
    assert all(not entry['overridden'] for entry in reset['entries'])


def test_saved_colors_are_not_overwritten_by_another_tab_or_old_catalog(identities):
    settings = PaletteSettings(identities.service)
    old = settings.public('owner')
    entry = color_entry(old)
    saved = settings.save('owner', payload(old, {entry['id']: '#112233'}))
    with pytest.raises(PaletteConflict):
        settings.save('owner', payload(old, {entry['id']: '#445566'}))
    with pytest.raises(PaletteConflict):
        settings.reset('owner', {'revision': saved['revision'], 'catalog_version': 'outdated-catalog'})
    assert settings.public('owner')['overrides'] == {entry['id']: '#112233'}


@pytest.mark.parametrize('value', ['#123', '#12345', '#123456789', 'red', 'rgba(1,2,3,.5)',
                                 '#123456; color:red', 'url(https://example.invalid/pixel)',
                                 '</style><script>alert(1)</script>', True, None, {}, []])
def test_non_color_values_cannot_modify_css_or_saved_configuration(identities, value):
    settings = PaletteSettings(identities.service)
    view = settings.public('owner')
    entry = color_entry(view)
    saved = settings.save('owner', payload(view, {entry['id']: '#123456'}))
    with pytest.raises(ValueError):
        settings.save('owner', payload(saved, {entry['id']: value}))
    assert settings.public('owner')['overrides'] == saved['overrides']
    assert settings.public('owner')['revision'] == saved['revision']


def test_unknown_tokens_and_invalid_revision_do_not_partially_apply(identities):
    settings = PaletteSettings(identities.service)
    view = settings.public('owner')
    entry = color_entry(view)
    attempts = [payload(view, {entry['id']: '#123456', '--unknown-token': '#FFFFFF'}),
                payload(view, []), {**payload(view, {}), 'revision': True},
                {**payload(view, {}), 'revision': -1}, {**payload(view, {}), 'css': 'body{display:none}'}]
    for attempt in attempts:
        with pytest.raises(ValueError):
            settings.save('owner', attempt)
    assert settings.public('owner')['revision'] == view['revision']
    assert settings.public('owner')['overrides'] == {}


def test_service_enforces_current_super_role_for_read_save_and_reset(identities):
    settings = PaletteSettings(identities.service)
    view = settings.public('owner')
    for operation in (lambda: settings.public('north'), lambda: settings.save('north', payload(view, {})),
                      lambda: settings.reset('north', {'revision': view['revision'], 'catalog_version': view['catalog_version']})):
        with pytest.raises(PermissionError):
            operation()
    with identities.auth.store.transaction() as admins:
        admins['owner']['disabled'] = True
    with pytest.raises(PermissionError):
        settings.save('owner', payload(view, {}))


def test_http_routes_enforce_auth_csrf_revision_and_public_stylesheet(palette_web):
    state = palette_web
    anonymous = state.app.test_client()
    assert anonymous.get('/admin/palette').status_code == 401
    assert anonymous.get('/admin/api/palette').status_code == 401
    css = anonymous.get('/ui/palette.css')
    assert css.status_code == 200 and css.mimetype == 'text/css'
    assert css.headers.get('ETag')
    assert 'no-cache' in css.headers['Cache-Control']
    branch = state.app.test_client()
    branch_csrf = login(branch)
    assert branch.get('/admin/palette').status_code == 403
    assert branch.get('/admin/api/palette').status_code == 403
    assert '/admin/palette' not in branch.get('/admin/users').text
    assert post(branch, branch_csrf, '/admin/api/palette', {}).status_code == 403
    owner = state.app.test_client()
    csrf = login(owner, 'super')
    assert owner.get('/admin/palette').status_code == 200
    assert '/admin/palette' in owner.get('/admin/users').text
    view = owner.get('/admin/api/palette').json
    entry = color_entry(view)
    data = payload(view, {entry['id']: '#22446680'})
    assert owner.post('/admin/api/palette', json=data).status_code == 403
    assert post(owner, csrf, '/admin/api/palette', data).status_code == 200
    assert post(owner, csrf, '/admin/api/palette', data).status_code == 409
    assert post(owner, csrf, '/admin/api/palette/reset', {'revision': view['revision'], 'catalog_version': view['catalog_version']}).status_code == 409
    changed = anonymous.get('/ui/palette.css')
    assert changed.headers['ETag'] != css.headers['ETag']
    assert '#22446680' in changed.text and entry['css_var'] in changed.text
    assert anonymous.get('/ui/palette.css', headers={'If-None-Match': changed.headers['ETag']}).status_code == 304
    default_page = owner.get('/admin/palette?defaults=1')
    assert default_page.status_code == 200 and 'href="/ui/palette.css' not in default_page.text
    assert not state.calls
