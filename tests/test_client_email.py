"""Branch-scoped contact email editing, export and browser regressions."""
import csv
import io
import os

import pytest

from test_web_security import web, login, post
from test_ui_browser import browser_ui, sign_in


def test_contact_email_create_edit_clear_and_scope(web):
    client = web.app.test_client()
    csrf = login(client)
    response = post(client, csrf, '/admin/api/create-link', {
        'user_name': 'Contact email', 'phone': '+70000000303',
        'branch_id': 12, 'email': ' User@EXAMPLE.invalid ',
    })
    assert response.status_code == 200
    token = response.json['token']
    assert web.links.load_links()[token]['email'] == 'user@example.invalid'
    assert post(client, csrf, '/admin/api/update-user', {
        'token': token, 'email': 'NEW@EXAMPLE.invalid',
    }).status_code == 200
    assert post(client, csrf, '/admin/api/update-user', {
        'token': token, 'user_name': 'Updated contact',
    }).status_code == 200
    users = client.get('/admin/api/users').json['users']
    assert {user['branch_id'] for user in users} == {12}
    assert next(user for user in users if user['token'] == token)['email'] == 'new@example.invalid'
    assert post(client, csrf, '/admin/api/update-user', {
        'token': web.token_b, 'email': 'foreign@example.invalid',
    }).status_code == 404
    assert not web.links.load_links()[web.token_b].get('email')
    assert post(client, csrf, '/admin/api/update-user', {
        'token': token, 'email': '   ',
    }).status_code == 200
    assert web.links.load_links()[token]['email'] == ''
    assert 'example.invalid' not in str(web.audit)
    assert web.calls == []


@pytest.mark.parametrize('invalid', [None, False, [], {}, 15, 'wrong',
    'wrong@@example.invalid', 'wrong@example', 'a\r\nb@example.invalid',
    'a b@example.invalid', '.first@example.invalid', 'a' * 65 + '@example.invalid'])
def test_invalid_contact_email_is_rejected_without_partial_updates(web, invalid):
    client = web.app.test_client()
    csrf = login(client, 'super')
    before = web.links.load_links()
    response = post(client, csrf, '/admin/api/update-user', {
        'token': web.token_a, 'email': invalid, 'user_name': 'Must not change',
    })
    assert response.status_code == 400
    assert web.links.load_links() == before
    assert post(client, csrf, '/admin/api/create-link', {
        'user_name': 'Must not create', 'phone': '+70000000303',
        'branch_id': 12, 'email': invalid,
    }).status_code == 400
    assert web.links.load_links() == before


def test_email_csv_remains_scoped_and_formula_safe(web):
    web.links.update_user(web.token_a, {'email': '=contact@example.invalid'})
    web.links.update_user(web.token_b, {'email': 'other@example.invalid'})
    client = web.app.test_client()
    login(client)
    response = client.get('/admin/api/export-csv')
    reader = csv.DictReader(io.StringIO(response.get_data(as_text=True).split('\r\n', 1)[1]), delimiter=';')
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]['email'] == "'=contact@example.invalid"
    assert rows[0]['branch_id'] == '12'
    assert 'other@example.invalid' not in response.get_data(as_text=True)


def test_branch_contact_edit_preserves_verified_identity_and_other_branch(web, tmp_path, monkeypatch):
    from storage.sql_documents import DocumentStore, migrate_legacy, read_document, write_document
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path / 'admins.json'))
    identity = web.module.product_services().identity
    web.links.update_user(web.token_a, {'email': 'branch-a@example.invalid'})
    person_id = web.links.load_links()[web.token_a]['person_id']
    with identity.product.connect() as db:
        db.execute('UPDATE persons SET email=?,email_verified=1 WHERE id=?',
                   ('verified@example.invalid', person_id))
    other = web.links.create_link({
        'branch_id': 123, 'user_name': 'Same person elsewhere',
        'phone': '+70000000101', 'person_id': person_id,
        'email': 'branch-b@example.invalid',
    })
    with identity.product.connect() as db:
        db.execute('UPDATE branch_clients SET person_id=? WHERE legacy_token=?', (person_id, other))
        links = read_document(db, 'links.json')
        links[other]['person_id'] = person_id
        write_document(db, 'links.json', links)
    assert web.links.load_links()[other]['person_id'] == person_id
    client = web.app.test_client()
    csrf = login(client)
    assert post(client, csrf, '/admin/api/update-user', {
        'token': web.token_a, 'email': 'new-contact@example.invalid',
    }).status_code == 200
    assert web.links.load_links()[web.token_a]['email'] == 'new-contact@example.invalid'
    assert web.links.load_links()[other]['email'] == 'branch-b@example.invalid'
    with identity.product.connect() as db:
        person = db.execute('SELECT email,email_verified FROM persons WHERE id=?', (person_id,)).fetchone()
        assert dict(person) == {'email': 'verified@example.invalid', 'email_verified': 1}


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')
@pytest.mark.parametrize('username', ['super', 'tenant-a'])
def test_email_table_edit_search_sort_and_mobile(web, browser_ui, username):
    page, origin, expect, errors = browser_ui
    web.links.update_user(web.token_a, {'email': 'contact@example.invalid'})
    web.links.update_user(web.token_b, {'email': 'other@example.invalid'})
    sign_in(page, origin, username)
    page.locator('#searchInput').fill('CONTACT@')
    expect(page.locator('#usersTableBody tr')).to_have_count(1)
    expect(page.locator('#usersTableBody td[data-label=Email]')).to_have_text('contact@example.invalid')
    expect(page.locator('#usersTableBody td[data-label=Email]')).to_be_visible()
    if username == 'tenant-a':
        expect(page.locator('#usersTableBody .client-branch')).to_be_hidden()
    else:
        expect(page.locator('#usersTableBody .client-branch')).to_be_visible()
    page.locator('#usersTableBody [title=Редактировать]').click()
    email = page.get_by_label('Email', exact=True)
    expect(email).to_have_value('contact@example.invalid')
    email.fill('invalid')
    page.get_by_role('button', name='Сохранить', exact=True).click()
    expect(email).to_be_visible()
    assert web.links.load_links()[web.token_a]['email'] == 'contact@example.invalid'
    email.fill('renamed@example.invalid')
    page.get_by_role('button', name='Сохранить', exact=True).click()
    expect(page.locator('#usersTableBody tr')).to_have_count(0)
    page.locator('#searchInput').fill('renamed@')
    expect(page.locator('#usersTableBody td[data-label=Email]')).to_have_text('renamed@example.invalid')
    page.locator('th[onclick="sortUsersBy(\'email\')"]').focus()
    page.keyboard.press('Enter')
    expect(page.locator('th[onclick="sortUsersBy(\'email\')"]')).to_have_attribute('aria-sort', 'ascending')
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#usersTableBody [title=Редактировать]').click()
    page.get_by_label('Email', exact=True).fill('')
    page.get_by_role('button', name='Сохранить', exact=True).click()
    expect(page.locator('#usersTableBody tr')).to_have_count(0)
    page.locator('#searchInput').fill('Client A')
    expect(page.locator('#usersTableBody td[data-label=Email]')).to_have_text('—')
    expect(page.locator('#usersTableBody .btn-pass')).to_be_visible()
    assert web.links.load_links()[web.token_a]['email'] == ''
    assert web.calls == []


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')
def test_new_client_email_can_be_entered_in_browser(web, browser_ui):
    page, origin, expect, errors = browser_ui
    sign_in(page, origin, 'tenant-a')
    page.goto(origin + '/admin/add-user')
    page.get_by_label('Имя пользователя *').fill('Email contact')
    page.get_by_label('Номер телефона *').fill('+70000000303')
    page.get_by_label('Email', exact=True).fill('created@example.invalid')
    page.locator('#createLinkForm button[type=submit]').click()
    expect(page.locator('#linkResult')).to_be_visible()
    page.goto(origin + '/admin/users')
    page.locator('#searchInput').fill('created@')
    expect(page.locator('#usersTableBody tr')).to_have_count(1)
    expect(page.locator('#usersTableBody td[data-label=Email]')).to_have_text('created@example.invalid')
    assert web.calls == []
