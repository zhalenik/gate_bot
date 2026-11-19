"""Offline account/trial invariants for a verified Marketplace onboarding."""
from unittest.mock import Mock

import pytest

from admin_auth import AdminAuth
from identity_service import default_admin_preferences
from marketplace_onboarding import (
    provision_marketplace, set_marketplace_password, validate_marketplace_target,
)
from storage.sql_documents import read_document, write_document
from test_identity_product import identities, PASSWORD


PROFILE = {'name': 'Marketplace owner', 'email': 'marketplace@example.invalid',
           'phone': '+15550100444', 'salon_name': 'Signed branch name'}


def provision(state, branch='33', profile=None, company=None):
    with state.product.connect() as db:
        return provision_marketplace(state.service, db, branch, profile or PROFILE,
                                     company or {}, billing=state.billing)


def test_verified_registration_creates_one_admin_client_and_trial_without_super_review(identities):
    state = identities
    welcome = Mock()
    state.service.welcome_sink = welcome
    original_super = state.auth.load_admins()['owner']
    created = provision(state, company={'title': 'Company name'})
    branch = state.branches.load_branches()['33']
    assert branch['name'] == 'Company name'
    assert branch['enabled'] and branch['onboarding_status'] == 'active'
    assert branch['yclients_connection_mode'] == 'marketplace'
    assert branch['buttons'] == [] and branch['hardware_routes_verified'] is False
    assert created['created'] and created['password_setup_required']
    assert state.service.web_admin(created['username'])['branch_ids'] == ['33']
    clients = [c for c in state.links.load_links().values() if c['branch_id'] == 33]
    assert len(clients) == 1
    assert clients[0]['person_id'] == created['person_id']
    assert clients[0]['admin_role_active'] and clients[0]['email'] == PROFILE['email']
    assert clients[0]['phone'] == PROFILE['phone']
    assert state.auth.load_admins()['owner'] == original_super
    with state.product.connect() as db:
        import json
        prefs = db.execute('SELECT payload FROM notification_preferences WHERE person_id=? AND audience="admin"',
                           (created['person_id'],)).fetchone()
        assert json.loads(prefs['payload']) == default_admin_preferences()
        trial = dict(db.execute('SELECT * FROM billing_terms WHERE branch_id="33" AND source="trial"').fetchone())
        assert trial['quota'] is None
        assert trial['starts'] == state.clock[0] and trial['ends'] > trial['starts']
    password_hash = state.auth.load_admins()[created['username']]['password_hash']
    state.clock[0] += 86400
    replay = provision(state, company={'title': 'Do not overwrite local name'})
    assert not replay['created'] and replay['person_id'] == created['person_id']
    assert state.auth.load_admins()[created['username']]['password_hash'] == password_hash
    assert state.branches.load_branches()['33']['name'] == 'Company name'
    with state.product.connect() as db:
        trials = db.execute('SELECT * FROM billing_terms WHERE branch_id="33" AND source="trial"').fetchall()
        assert len(trials) == 1 and dict(trials[0]) == trial
    welcome.assert_called_once_with(created['person_id'], '33', PROFILE['email'])


def test_old_pending_marketplace_branch_activates_with_existing_password_and_client(identities):
    state = identities
    with state.product.connect() as db:
        admin = state.service._admin(db, 'north')
        db.execute('UPDATE persons SET email=? WHERE id=?', (PROFILE['email'], admin['person_id']))
        branches = read_document(db, 'branches.json')
        branches['11'].update(yclients_connection_mode='marketplace', onboarding_status='pending_verification',
                              enabled=False, buttons=[{'id': 'configured-gate'}])
        write_document(db, 'branches.json', branches)
    before_clients = state.links.load_links()
    before_admin = state.auth.load_admins()['north']
    profile = {**PROFILE, 'phone': '+15550100888'}
    result = provision(state, branch='11', profile=profile)
    assert result['username'] == 'north' and not result['created']
    assert not result['password_setup_required']
    assert state.auth.verify_admin('north', PASSWORD)
    assert state.auth.load_admins()['north'] == before_admin
    assert state.links.load_links() == before_clients
    branch = state.branches.load_branches()['11']
    assert branch['enabled'] and branch['onboarding_status'] == 'active'
    assert branch['buttons'] == [{'id': 'configured-gate'}]
    with state.product.connect() as db:
        assert db.execute('SELECT trial_used FROM billing_accounts WHERE branch_id="11"').fetchone()[0] == 1


def test_old_pending_branch_preserves_existing_gift_instead_of_starting_trial(identities):
    state = identities
    with state.product.connect() as db:
        admin = state.service._admin(db, 'north')
        db.execute('UPDATE persons SET email=? WHERE id=?', (PROFILE['email'], admin['person_id']))
        branches = read_document(db, 'branches.json')
        branches['11'].update(yclients_connection_mode='marketplace', onboarding_status='pending_verification', enabled=False)
        write_document(db, 'branches.json', branches)
    state.billing.grant_plan_gift('11', actor='owner', days=45, quota=500, sms_quota=60, at=state.clock[0])
    before = state.billing.subscription_view('11', at=state.clock[0])
    result = provision(state, branch='11', profile={**PROFILE, 'phone': '+15550100888'})
    assert not result['created'] and state.branches.get_branch('11').enabled
    after = state.billing.subscription_view('11', at=state.clock[0])
    assert after['term_id'] == before['term_id'] and after['ends_at'] == before['ends_at']
    assert after['quota'] == before['quota'] == 500
    assert after['sms']['quota'] == before['sms']['quota'] == 60
    with state.product.connect() as db:
        assert not db.execute('SELECT trial_used FROM billing_accounts WHERE branch_id="11"').fetchone()[0]
        assert not db.execute('SELECT 1 FROM billing_terms WHERE branch_id="11" AND source="trial"').fetchone()


@pytest.mark.parametrize('change', ['disabled', 'revoked', 'super', 'different_phone', 'different_email', 'other_branch'])
def test_registration_cannot_restore_roles_or_claim_another_account(identities, change):
    state = identities
    created = provision(state)
    profile = dict(PROFILE)
    branch_id = '33'
    with state.product.connect() as db:
        admins = read_document(db, 'admins.json')
        admin = admins[created['username']]
        if change == 'disabled':
            admin['disabled'] = True
        elif change == 'super':
            admin['is_super'] = True
        elif change == 'revoked':
            db.execute('UPDATE branch_memberships SET status="revoked" WHERE person_id=?', (created['person_id'],))
        elif change == 'different_phone':
            profile['phone'] = '+15550100555'
        elif change == 'different_email':
            profile['email'] = 'other@example.invalid'
        elif change == 'other_branch':
            branch_id = '44'
        write_document(db, 'admins.json', admins)
    before_admins, before_branches = state.auth.load_admins(), state.branches.load_branches()
    with pytest.raises(PermissionError):
        provision(state, branch_id, profile)
    assert state.auth.load_admins() == before_admins
    assert state.branches.load_branches() == before_branches


@pytest.mark.parametrize('mode,enabled', [('direct', True), ('marketplace', False)])
def test_existing_manual_and_standalone_branches_are_not_converted(identities, mode, enabled):
    state = identities
    provision(state)
    with state.product.connect() as db:
        branches = read_document(db, 'branches.json')
        branches['33'].update(yclients_enabled=enabled, yclients_connection_mode=mode)
        write_document(db, 'branches.json', branches)
    with pytest.raises(PermissionError):
        provision(state)
    assert state.branches.load_branches()['33']['yclients_connection_mode'] == mode


def test_unrelated_existing_branch_is_not_claimed_and_preflight_is_read_only(identities):
    state = identities
    before = (state.auth.load_admins(), state.branches.load_branches(), state.links.load_links())
    with state.product.connect() as db:
        normalized = validate_marketplace_target(state.service, db, '33', {**PROFILE, 'email': 'Marketplace@EXAMPLE.invalid'})
        assert normalized['email'] == PROFILE['email']
        with pytest.raises(PermissionError):
            validate_marketplace_target(state.service, db, '11', PROFILE)
    assert before == (state.auth.load_admins(), state.branches.load_branches(), state.links.load_links())


def test_billing_failure_rolls_back_branch_account_and_client_together(identities):
    state = identities
    broken = Mock()
    broken.activate_trial.side_effect = ValueError('trial unavailable')
    before = (state.auth.load_admins(), state.branches.load_branches(), state.links.load_links())
    with pytest.raises(ValueError, match='trial unavailable'):
        with state.product.connect() as db:
            provision_marketplace(state.service, db, '33', PROFILE, {}, billing=broken)
    assert before == (state.auth.load_admins(), state.branches.load_branches(), state.links.load_links())


def test_password_setup_requires_matching_confirmation_and_is_one_time(identities):
    state = identities
    created = provision(state)
    username = created['username']
    old = state.auth.load_admins()[username]
    with pytest.raises(ValueError, match='совпадают'):
        with state.product.connect() as db:
            set_marketplace_password(state.service, db, username, PASSWORD, PASSWORD + '-other')
    with state.product.connect() as db:
        result = set_marketplace_password(state.service, db, username, PASSWORD, PASSWORD)
    assert not result['password_setup_required']
    saved = state.auth.load_admins()[username]
    assert AdminAuth.verify_password(PASSWORD, saved['password_hash'])
    assert saved['session_version'] == old['session_version'] + 1
    with pytest.raises(PermissionError):
        with state.product.connect() as db:
            set_marketplace_password(state.service, db, username, PASSWORD + '-new', PASSWORD + '-new')
    with pytest.raises(PermissionError):
        with state.product.connect() as db:
            set_marketplace_password(state.service, db, 'owner', PASSWORD, PASSWORD)
