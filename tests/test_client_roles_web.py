"""Administrator/client HTTP lifecycle using temporary migrated state only."""
import os
import pytest

from storage.sql_documents import DocumentStore,migrate_legacy
from test_web_security import web,login,post,TEST_PASSWORD
from test_ui_browser import browser_ui,sign_in


@pytest.fixture
def roles_web(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    identity=web.module.product_services().identity
    identity.attach_console_account('tenant-a','+70000000101',client_token=web.token_a)
    identity.attach_console_account('tenant-b','+70000000202',client_token=web.token_b)
    return web,identity


def test_pending_branch_is_distinct_from_administrator_permanent_grant(roles_web):
    web,identity=roles_web
    with web.branches.store.transaction() as branches:
        branches['12'].update(enabled=False,onboarding_status='pending_verification')
    client=web.app.test_client();login(client,'super')
    user=next(row for row in client.get('/admin/api/users').json['users'] if row['token']==web.token_a)
    assert user['admin_role']['is_admin'] and user['admin_role']['last_admin']
    assert user['gate_access'] and not user['access_available']
    assert user['branch_access_reason']=='pending_verification'
    assert not web.calls


def test_last_admin_cannot_be_deleted_or_revoked_even_by_super(roles_web):
    web,identity=roles_web;client=web.app.test_client()
    for actor in ('tenant-a','super'):
        token=login(client,actor)
        for path in ('/admin/api/client-role/revoke','/admin/api/delete-user'):
            response=post(client,token,path,{'token':web.token_a,'confirm_admin':True})
            assert response.status_code==409
            assert web.token_a in web.links.load_links()
    assert web.auth.verify_admin('tenant-a',TEST_PASSWORD)
    with web.auth.store.transaction() as admins:
        admins['additional']={'name':'Additional','password_hash':admins['tenant-a']['password_hash'],
                              'branch_id':12,'is_super':False,'session_version':1}
    identity.attach_console_account('additional','+15550100333')
    token=login(client,'super')
    assert post(client,token,'/admin/api/delete-user',{'token':web.token_a}).status_code==409
    assert post(client,token,'/admin/api/delete-user',{'token':web.token_a,'confirm_admin':True}).status_code==200
    assert web.token_a not in web.links.load_links() and not web.auth.verify_admin('tenant-a',TEST_PASSWORD)
    assert web.auth.verify_admin('additional',TEST_PASSWORD) and not web.calls


def test_branch_delete_requires_super_csrf_confirmation_and_unchanged_preview(roles_web):
    web,identity=roles_web;client=web.app.test_client();base='/admin/api/branches/12'
    token=login(client,'tenant-a')
    assert client.get(base+'/deletion').status_code==403
    assert post(client,token,base+'/delete',{}).status_code==403
    token=login(client,'super');preview=client.get(base+'/deletion').json['preview']
    assert client.post(base+'/delete',json={'confirmation':'12','revision':preview['revision']}).status_code==403
    assert post(client,token,base+'/delete',{'confirmation':'123','revision':preview['revision']}).status_code==409
    web.links.update_user(web.token_a,{'user_name':'Updated after preview'})
    assert post(client,token,base+'/delete',{'confirmation':'12','revision':preview['revision']}).status_code==409
    preview=client.get(base+'/deletion').json['preview']
    other=web.links.load_links()[web.token_b]
    response=post(client,token,base+'/delete',{'confirmation':'12','revision':preview['revision']})
    assert response.status_code==200
    assert web.branches.get_branch(12) is None
    assert all(str(row['branch_id'])!='12' for row in web.links.load_links().values())
    assert web.links.load_links()[web.token_b]==other
    assert not web.auth.verify_admin('tenant-a',TEST_PASSWORD)
    assert web.auth.verify_admin('super',TEST_PASSWORD) and web.auth.verify_admin('tenant-b',TEST_PASSWORD)
    assert client.get('/admin/api/users').status_code==200 and not web.calls


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')
def test_super_deletes_branch_from_reviewable_browser_dialog(roles_web,browser_ui):
    web,identity=roles_web;page,origin,expect,errors=browser_ui
    sign_in(page,origin)
    page.goto(origin+'/admin/settings')
    page.locator('#settingsBranch').select_option('12')
    expect(page.locator('#branchDeletionPanel')).to_be_visible()
    page.locator('#deleteBranchButton').click()
    expect(page.locator('#branchDeletionDialog')).to_be_visible()
    expect(page.locator('#branchDeletionSummary')).to_contain_text('ID 12')
    expect(page.locator('#branchDeletionSubmit')).to_be_disabled()
    page.locator('#branchDeletionConfirmation').fill('123')
    expect(page.locator('#branchDeletionSubmit')).to_be_disabled()
    # A background refresh of the same branch must preserve this preview and
    # confirmation. The server still rejects a changed revision on submission.
    with page.expect_response('**/admin/api/branch-settings?branch_id=12'):
        page.locator('#settingsBranch').dispatch_event('change')
    expect(page.locator('#branchSettingsForm')).to_have_attribute('data-ready','true')
    expect(page.locator('#branchDeletionDialog')).to_be_visible()
    expect(page.locator('#branchDeletionConfirmation')).to_have_value('123')
    page.locator('#branchDeletionConfirmation').fill('12')
    with page.expect_response(lambda response:response.url.endswith('/admin/api/branches/12/delete')) as result:
        page.locator('#branchDeletionSubmit').click()
    assert result.value.status==200
    expect(page.locator('#settingsBranch option[value="12"]')).to_have_count(0)
    assert web.branches.get_branch(12) is None and web.branches.get_branch(123)
    assert not errors and not web.calls
