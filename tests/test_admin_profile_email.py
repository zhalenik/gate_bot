"""An administrator uses their own client contact, without a second address."""
from registration_samples import agreements
import re
from unittest.mock import Mock

import pytest

from product_notifications import ProductNotifications
from storage import config
from storage.sql_documents import read_document, write_document
from test_identity_product import identities, accept, PASSWORD, PHONE


def own_token(state, username):
    admin=state.service.web_admin(username)
    return next(token for token, client in state.links.load_links().items()
                if client['person_id']==admin['person_id'] and
                (admin['is_super'] or str(client['branch_id'])==str(admin['branch_id'])))


@pytest.mark.parametrize('flow', ['invitation', 'standalone'])
def test_registration_contact_is_ready_without_separate_email_confirmation(identities, flow):
    state=identities
    if flow=='invitation':
        result,*_=accept(state)
    else:
        reg=state.service.register_branch(None,'Local','Owner',PHONE,yclients_enabled=False)
        otp=state.service.request_otp(reg['registration_token'],registration=True, documents=agreements(state.service))
        code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
        result=state.service.complete_registration(reg['registration_token'],otp['challenge_id'],code,'registered@example.invalid',PASSWORD, documents=agreements(state.service))
    prefs=state.service.admin_preferences(result['username'])
    assert prefs['email']==result['username']==state.links.load_links()[own_token(state,result['username'])]['email']
    assert prefs['channels']['email'] and not prefs['email_verified']
    assert not any(channels['email'] for channels in prefs['events'].values())
    state.service.save_admin_preferences(result['username'], {'events': {'gate_requested': {'email': True}}})
    contacts=state.service.contacts_for_admin_events(result['branch_id'],'gate_requested')
    assert any(c['username']==result['username'] and c['target']==result['username'] for c in contacts)
    assert all(channel=='sms' for channel,*_ in state.sent)


def test_existing_client_address_wins_over_login_and_updates_from_both_forms(identities):
    state=identities
    state.links.update_user('example-client',{'email':'profile@example.invalid'})
    result,*_=accept(state);username=result['username']
    assert state.service.admin_preferences(username)['email']=='profile@example.invalid'
    state.links.update_user('example-client',{'email':'card-edit@example.invalid'})
    assert state.service.admin_preferences(username)['email']=='card-edit@example.invalid'
    state.service.admin_email(username,'settings-edit@example.invalid')
    assert state.links.load_links()['example-client']['email']=='settings-edit@example.invalid'
    assert state.service.admin_preferences(username)['channels']['email']
    with state.product.connect() as db:
        assert db.execute('SELECT email FROM persons WHERE id=?',(result['person_id'],)).fetchone()[0]==username
    assert state.auth.verify_admin(username,PASSWORD)
    state.service.admin_email(username,'')
    assert not state.service.admin_preferences(username)['channels']['email']
    state.service.import_legacy()
    assert state.service.admin_preferences(username)['email']==''


def test_primary_branch_contact_does_not_read_or_update_other_membership(identities):
    state=identities;result,*_=accept(state)
    with state.product.connect() as db:
        links=read_document(db,'links.json')
        links['other-profile']={**links['example-client'],'branch_id':12,'email':'other@example.invalid'}
        write_document(db,'links.json',links)
        db.execute('INSERT INTO branch_memberships VALUES(?,?,"active",?)',(result['person_id'],'12',state.clock[0]))
    state.service.admin_email(result['username'],'primary@example.invalid')
    assert state.links.load_links()['other-profile']['email']=='other@example.invalid'
    assert state.service.admin_preferences(result['username'])['email']=='primary@example.invalid'
    state.service.save_admin_preferences(result['username'],{'events':{'gate_error':{'email':True}}})
    assert not state.service.contacts_for_admin_events('12','gate_error')


def test_super_uses_own_profile_without_creating_more_clients(identities):
    state=identities;before=state.links.load_links()
    state.service.admin_email('owner','super@example.invalid')
    assert state.service.admin_preferences('owner')['email']=='super@example.invalid'
    state.service.admin_email('owner','changed@example.invalid')
    assert state.service.admin_preferences('owner')['email']=='changed@example.invalid'
    assert state.links.load_links()==before


def test_old_queued_email_is_skipped_and_new_event_uses_new_profile(identities,monkeypatch):
    state=identities
    state.service.admin_email('owner','before@example.invalid')
    state.service.save_admin_preferences('owner',{'events':{'gate_error':{'email':True}}})
    sender=Mock(return_value={'status':'accepted'})
    notifications=ProductNotifications(state.service,state.links,state.branches,state.billing,sender=sender)
    monkeypatch.setattr(notifications,'_channel_ready',lambda channel:True)
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    notifications.event('gate_error','11',{'event_id':'old-contact'})
    state.service.admin_email('owner','after@example.invalid')
    notifications.process_once()
    sender.assert_not_called()
    notifications.event('gate_error','11',{'event_id':'current-contact'})
    notifications.process_once()
    sender.assert_called_once()
    assert sender.call_args.args[:2]==('email','after@example.invalid')
    state.service.admin_email('owner','')
    assert not state.service.contacts_for_admin_events('11','gate_error')


def test_public_bearer_cannot_redirect_an_admin_contact(identities):
    state=identities;result,*_=accept(state)
    original=state.links.load_links()['example-client']['email']
    payload={'enabled':True,'email':'redirect@example.invalid','channels':{'email':True}}
    with pytest.raises(PermissionError):state.service.save_prefs_for_token('example-client',payload)
    with pytest.raises(PermissionError):state.service.save_prefs_for_token('example-client',payload,actor_username='north')
    assert state.links.load_links()['example-client']['email']==original
    state.service.save_prefs_for_token('example-client',{**payload,'email':original})
    state.service.save_prefs_for_token('example-client',payload,actor_username=result['username'])
    assert state.service.admin_preferences(result['username'])['email']=='redirect@example.invalid'


def test_invalid_stored_email_cannot_become_an_admin_recipient(identities):
    state=identities
    with state.product.connect() as db:
        db.execute('UPDATE persons SET email=? WHERE id=?',('invalid\r\naddress',state.service.web_admin('owner')['person_id']))
    state.service.save_admin_preferences('owner',{'events':{'sms_received':{'email':True}}})
    assert not state.service.admin_preferences('owner')['channels']['email']
    assert not state.service.contacts_for_admin_events(None,'sms_received')
