"""Document publication, version changes during SMS verification and proof."""
from io import BytesIO
import os
import re

import pytest

from legal_documents import publish
from registration_samples import agreements
from test_identity_product import identities, PASSWORD, PHONE
from test_ui_browser import browser_ui
from test_web_security import web, login


def test_current_editions_are_bound_to_the_verified_challenge(identities):
    state = identities
    identity = state.service
    raw = identity.register_branch('98', 'Branch', 'Owner', PHONE)['registration_token']
    first = agreements(identity)
    for invalid in (None, {}, {'terms':first['terms']}, {**first,'terms':True}):
        with pytest.raises(ValueError):
            identity.request_otp(raw, registration=True, documents=invalid)
    assert not state.sent
    challenge = identity.request_otp(raw, registration=True, documents=first)['challenge_id']
    code = re.search(r'\b\d{6}\b', state.sent[-1][2]).group()
    with state.product.connect() as db:
        old = db.execute('SELECT content FROM registration_documents WHERE id=?', (first['terms'],)).fetchone()[0]
        updated = publish(db, 'terms', 'terms.txt', b'Updated service rules', 'owner')
        assert updated != first['terms']
    second = agreements(identity)
    for value in (first, second):
        with pytest.raises(ValueError):
            identity.complete_registration(raw, challenge, code, 'owner@example.invalid', PASSWORD, documents=value)
    assert state.branches.get_branch('98') is None
    state.clock[0] += 61
    challenge = identity.request_otp(raw, registration=True, documents=second)['challenge_id']
    code = re.search(r'\b\d{6}\b', state.sent[-1][2]).group()
    with pytest.raises(ValueError):
        identity.complete_registration(raw, challenge, 'bad', 'owner@example.invalid', PASSWORD, documents=second)
    with state.product.connect() as db:
        assert not db.execute('SELECT 1 FROM registration_consent WHERE accepted IS NOT NULL').fetchone()
    person = identity.complete_registration(raw, challenge, code, 'owner@example.invalid', PASSWORD, documents=second)['person_id']
    with state.product.connect() as db:
        rows = db.execute('SELECT * FROM registration_consent WHERE accepted IS NOT NULL').fetchall()
        assert len(rows) == 2 and {row['document_id'] for row in rows} == set(second.values())
        assert all((row['participant'],row['branch_id'],row['phone'],row['verification_channel'],row['challenge']) ==
                   (person,'98',PHONE,'sms',challenge) and row['accepted'] >= row['presented'] for row in rows)
        assert db.execute('SELECT content FROM registration_documents WHERE id=?', (first['terms'],)).fetchone()[0] == old


def test_document_library_is_owner_only_and_archives_public_editions(web, tmp_path, monkeypatch):
    from storage.sql_documents import DocumentStore, migrate_legacy
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path/'admins.json'))
    client = web.app.test_client()
    assert client.get('/admin/documents').status_code in (302,401)
    token = login(client)
    assert client.get('/admin/documents').status_code == 403
    def upload(who, token, name, content):
        return who.post('/admin/documents/terms', data={'csrf_token':token, 'document':(BytesIO(content),name)})
    assert upload(client,token,'terms.txt',b'tenant').status_code == 403
    owner = web.app.test_client()
    token = login(owner,'super')
    assert 'Договор оферты' in owner.get('/admin/documents').text
    service = web.module.product_services().identity
    before = agreements(service)
    old = client.get(f"/documents/terms/{before['terms']}")
    assert old.status_code == 200 and len(old.data) > 200
    assert upload(owner,'missing','terms.txt',b'bad').status_code == 403
    assert upload(owner,token,'new.pdf',b'not a PDF').status_code == 303
    assert agreements(service) == before
    updated = b'New edition\n'*110000
    assert upload(owner,token,'new.txt',updated).status_code == 303
    after = agreements(service)
    assert after['terms'] != before['terms']
    anonymous = web.app.test_client()
    assert anonymous.get(f"/documents/terms/{before['terms']}").data == old.data
    assert anonymous.get(f"/documents/terms/{after['terms']}").data == updated
    assert anonymous.get(f"/documents/admin-privacy/{after['terms']}").status_code == 404


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1', reason='Opt-in browser acceptance')
def test_client_qr_has_one_unchecked_choice_and_displays_the_document(web, browser_ui, tmp_path, monkeypatch):
    from storage.sql_documents import DocumentStore, migrate_legacy
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path/'admins.json'))
    web.branches.update_settings(12, {'yclients_enabled':False})
    services = web.module.product_services()
    sent = []
    services.identity.sender = lambda *args: sent.append(args) or {'status':'sent'}
    raw = web.module.product_provider()['branch_join'].admin_view('tenant-a',12,'create')['url'].rsplit('/',1)[1]
    page,origin,expect,errors = browser_ui
    page.goto(origin+'/join/'+raw)
    expect(page.get_by_role('checkbox')).to_have_count(1)
    expect(page.get_by_role('checkbox')).not_to_be_checked()
    page.get_by_role('link',name='Прочитать согласие').click()
    dialog = page.get_by_role('dialog')
    expect(dialog).to_contain_text('ОГРНИП')
    dialog.get_by_role('button',name='Закрыть').click()
    page.locator('#phone').fill('+15550100221')
    page.get_by_role('button',name='Получить код').click()
    assert not sent
    page.get_by_role('checkbox').check()
    page.get_by_role('button',name='Получить код').click()
    expect(page.locator('#code')).to_be_visible()
    code = re.search(r'\b\d{6}\b', sent[-1][2]).group()
    page.locator('#code').fill(code)
    page.get_by_role('button',name='Подтвердить телефон').click()
    page.locator('#name').fill('Guest')
    page.get_by_role('button',name='Зарегистрироваться').click()
    page.wait_for_url('**/opn/*')
    with services.identity.product.connect() as db:
        row = db.execute('SELECT d.kind,c.branch_id,c.verification_channel FROM registration_consent c '
                         'JOIN registration_documents d ON c.document_id=d.id WHERE accepted IS NOT NULL').fetchall()
    assert [tuple(item) for item in row] == [('client-privacy','12','sms')]
