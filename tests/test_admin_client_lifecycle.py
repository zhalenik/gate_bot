"""Role and client removal are atomic; no real identities or delivery."""
from concurrent.futures import ThreadPoolExecutor
import pytest

from link_manager import access_is_active
from storage.sql_documents import read_document,write_document
from test_identity_product import identities,accept,PASSWORD


def admin_token(state,username):
    person=state.service.web_admin(username)['person_id']
    return next(token for token,client in state.links.load_links().items() if client.get('person_id')==person)


def test_last_branch_admin_cannot_be_revoked_or_deleted_even_by_super(identities):
    state=identities;token=admin_token(state,'north')
    role=state.service.client_admin_state('owner',token)
    assert role['is_admin'] and role['last_admin'] and not role['can_delete'] and not role['can_revoke']
    for actor in ('north','owner'):
        with pytest.raises(ValueError):state.service.delete_client(actor,token,confirm_admin=True)
        with pytest.raises(ValueError):state.service.revoke_client_admin(actor,token)
    assert state.auth.verify_admin('north',PASSWORD) and token in state.links.load_links()
    with pytest.raises(ValueError):state.links.delete_link(token)
    with pytest.raises(ValueError):state.service.delete_client('owner',admin_token(state,'owner'),confirm_admin=True)


def test_client_delete_requires_confirmation_revokes_role_and_removes_index(identities):
    state=identities;result,*_=accept(state)
    role=state.service.client_admin_state('north','example-client')
    assert role['is_admin'] and role['can_delete'] and not role['last_admin']
    with pytest.raises(ValueError):state.service.delete_client('owner','example-client')
    assert state.auth.verify_admin('new-admin@example.invalid',PASSWORD)
    assert state.service.delete_client('north','example-client',confirm_admin=True)=={'deleted':True,'admin_revoked':True}
    assert not state.auth.verify_admin('new-admin@example.invalid',PASSWORD)
    assert 'example-client' not in state.links.load_links()
    with state.product.connect() as db:
        assert not db.execute('SELECT 1 FROM branch_clients WHERE legacy_token="example-client"').fetchone()
    state.service.import_legacy()
    assert 'example-client' not in state.links.load_links()
    assert not any(row['status']=='active' for row in state.service.list_admins('owner','11') if row['person_id']==result['person_id'])


def test_revoke_without_delete_keeps_manual_client_grant_and_other_branch_scope(identities):
    state=identities;accept(state)
    role=state.service.revoke_client_admin('north','example-client')
    assert not role['is_admin']
    assert access_is_active(state.links.load_links()['example-client'])
    other=state.links.create_link({'branch_id':12,'phone':'+15550100900','user_name':'Other'})
    with pytest.raises(PermissionError):state.service.delete_client('north',other)
    with pytest.raises(PermissionError):state.service.client_admin_state('north',other)


def test_startup_repairs_only_missing_active_admin_client_without_activating_branch(identities):
    state=identities;token=admin_token(state,'north');person=state.service.web_admin('north')['person_id']
    before=state.auth.load_admins()['north']
    with state.branches.store.transaction() as branches:
        branches['11'].update(enabled=False,onboarding_status='pending_verification')
    # Reproduce the former writer: deleted document plus stale client index.
    with state.product.connect() as db:
        links=read_document(db,'links.json');del links[token]
        import json
        db.execute('UPDATE documents SET payload=? WHERE name="links.json"',(json.dumps(links),))
        assert db.execute('SELECT 1 FROM branch_clients WHERE legacy_token=?',(token,)).fetchone()
    counts=state.service.import_legacy()
    assert counts['repaired_admin_clients']==1
    repaired=admin_token(state,'north');assert repaired!=token
    assert access_is_active(state.links.load_links()[repaired])
    assert not state.branches.get_branch(11).enabled and state.auth.load_admins()['north']==before
    with state.product.connect() as db:
        assert not db.execute('SELECT 1 FROM branch_clients WHERE legacy_token=?',(token,)).fetchone()
    assert state.service.import_legacy()['repaired_admin_clients']==0
    state.links.toggle_user_access(repaired,False)
    state.service.import_legacy()
    assert state.links.load_links()[repaired]['admin_blocked']


def test_concurrent_revocations_leave_one_branch_admin(identities):
    state=identities;accept(state)
    targets=[admin_token(state,'north'),'example-client']
    def revoke(token):
        try:state.service.revoke_client_admin('owner',token);return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(revoke,targets))
    assert sum(results)==1
    assert sum(row['status']=='active' for row in state.service.list_admins('owner','11'))==1
