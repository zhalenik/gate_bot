"""Client consent, actual channel readiness and guest boundaries; offline only."""
import json
from types import SimpleNamespace

import pytest

from bot_workflows import BotWorkflows
from identity_service import CLIENT_CHANNELS
from storage.sql_documents import read_document, write_document
from test_identity_product import identities


def store_legacy_preferences(state, channels, enabled=True):
    client=state.links.load_links()['example-client']
    with state.product.connect() as db:
        db.execute('INSERT OR REPLACE INTO notification_preferences VALUES(?,"client","11",?)',
                   (client['person_id'],json.dumps({'enabled':enabled,'channels':channels})))


def test_legacy_chatpush_selection_migrates_without_repurposing_sms(identities,monkeypatch):
    state=identities
    monkeypatch.setattr(state.service,'_client_messengers_state',lambda *args:{'connected':True,'provider':'chatpush','reason':'ready'})
    store_legacy_preferences(state,{'chatpush':True,'sms':True,'telegram':False})
    prefs=state.service.prefs_for_token('example-client')
    assert set(prefs['channels'])==set(CLIENT_CHANNELS)
    assert prefs['channels']['messengers'] and prefs['effective_channels']['messengers']
    assert 'chatpush' not in prefs['allowed_channels'] and 'sms' not in prefs['allowed_channels']
    store_legacy_preferences(state,{'sms':True})
    prefs=state.service.prefs_for_token('example-client')
    assert not any(prefs['channels'].values())


def test_real_client_mailing_flag_and_binding_override_old_saved_preferences(identities):
    state=identities
    store_legacy_preferences(state,{'telegram':True,'max':True})
    state.links.bind_identity('telegram','71001',token='example-client')
    prefs=state.service.prefs_for_token('example-client')
    assert prefs['connected']['telegram'] and prefs['effective_channels']['telegram']
    assert prefs['channels']['max'] and not prefs['effective_channels']['max']
    state.links.set_mailing_enabled('example-client',False)
    prefs=state.service.prefs_for_token('example-client')
    assert not prefs['enabled'] and not any(prefs['effective_channels'].values())
    assert prefs['channels']['telegram']


def test_channel_toggle_without_email_preserves_contact_and_does_not_create_override(identities):
    state=identities
    with state.links.store.transaction() as links:
        links['example-client'].update(email='crm@example.invalid',email_override=False)
    state.service.save_prefs_for_token('example-client',{'enabled':True,'channels':{'telegram':True}})
    client=state.links.load_links()['example-client']
    assert client['email']=='crm@example.invalid' and not client['email_override']
    with pytest.raises(ValueError):
        state.service.save_prefs_for_token('example-client',{'enabled':True,'channels':{'sms':True}})


def test_unsubscribe_preserves_link_rights_and_channel_choices(identities):
    state=identities;before=state.links.load_links()['example-client']
    state.links.bind_identity('telegram','71001',token='example-client')
    state.service.save_prefs_for_token('example-client',{'enabled':False,'channels':{'telegram':True}})
    prefs=state.service.prefs_for_token('example-client')
    assert not prefs['enabled'] and prefs['channels']['telegram']
    assert state.links.verify_token('example-client') is not None
    after=state.links.load_links()['example-client']
    for key in ('gate_access','access_expires','branch_id','phone'):assert after.get(key)==before.get(key)
    state.service.save_prefs_for_token('example-client',{'enabled':True,'channels':prefs['channels']})
    assert state.service.prefs_for_token('example-client')['effective_channels']['telegram']


def test_standalone_filters_messenger_option_before_provider_lookup(identities,monkeypatch):
    state=identities
    store_legacy_preferences(state,{'messengers':True,'telegram':True})
    state.branches.update_settings('11',{'yclients_enabled':False})
    def unwanted_lookup(*args):raise AssertionError('Standalone must not resolve a messenger provider')
    monkeypatch.setattr(state.service,'_client_messengers_state',unwanted_lookup)
    prefs=state.service.prefs_for_token('example-client')
    assert prefs['allowed_channels']==['email','telegram','max']
    for key in ('channels','connected','effective_channels'):
        assert set(prefs[key])=={'email','telegram','max'}
    assert 'messengers' not in prefs
    before=state.links.load_links()['example-client']
    with pytest.raises(ValueError):
        state.service.save_prefs_for_token('example-client',{'enabled':True,'channels':{'messengers':True}})
    assert state.links.load_links()['example-client']==before and state.sent==[]


def test_standalone_unsubscribe_accepts_old_page_payload_and_resume_is_explicit(identities):
    state=identities
    state.links.bind_identity('telegram','71001',token='example-client')
    state.links.bind_identity('max','71002',token='example-client')
    store_legacy_preferences(state,{'messengers':True,'telegram':True,'max':True})
    state.branches.update_settings('11',{'yclients_enabled':False})
    before=state.links.load_links()['example-client']
    prefs=state.service.save_prefs_for_token('example-client',{'enabled':False,'channels':{'messengers':True,'telegram':True,'max':True}})
    assert not prefs['enabled'] and not any(prefs['effective_channels'].values())
    assert 'messengers' not in prefs['channels']
    after=state.links.load_links()['example-client']
    for key in ('gate_access','access_expires','branch_id','phone'):assert after.get(key)==before.get(key)
    resumed=state.service.save_prefs_for_token('example-client',{'enabled':True,'channels':{'max':True}})
    assert resumed['enabled'] and resumed['effective_channels']['max']
    assert not resumed['effective_channels']['telegram']
    assert state.sent==[]


def test_guest_cannot_open_or_change_preferences_or_obtain_connection_links(identities):
    state=identities;old=state.service.preferences_link_for_token('example-client').rsplit('/',1)[1]
    with state.links.store.transaction() as links:links['example-client']['is_temporary']=True
    for token in ('example-client',old):
        with pytest.raises(PermissionError):state.service.prefs_for_token(token)
        with pytest.raises(PermissionError):state.service.save_prefs_for_token(token,{'enabled':True,'channels':{}})
        with pytest.raises(PermissionError):state.service.verify_email_request(token)
        with pytest.raises(PermissionError):state.service.client_bot_link(token,'telegram')
    with pytest.raises(PermissionError):state.service.preferences_link_for_token('example-client')


def test_guest_bot_menu_omits_settings_but_retains_regular_client_settings(identities):
    state=identities
    clients=[('guest',{'branch_id':11,'is_temporary':True}),('regular',{'branch_id':12,'is_temporary':False})]
    access=SimpleNamespace(identity=state.service,branches=state.branches,_identity_records=lambda *args:clients)
    storage=SimpleNamespace(get_checkpoint=lambda *args:{})
    actions=BotWorkflows(access,storage).actions('telegram','71001')
    assert [a['url'].rsplit('/',1)[-1] for a in actions]==['regular']


def test_expired_regular_client_can_keep_preferences(identities):
    state=identities
    state.links.set_expiry_at('example-client','2020-01-01T00:00:00+00:00')
    state.service.save_prefs_for_token('example-client',{'enabled':True,'channels':{'telegram':True}})
    assert state.service.prefs_for_token('example-client')['channels']['telegram']
    assert state.links.verify_token('example-client') is None


def test_preferences_ticket_cannot_follow_client_to_another_branch(identities):
    state=identities;raw=state.service.preferences_link_for_token('example-client').rsplit('/',1)[1]
    with state.product.connect() as db:
        links=read_document(db,'links.json');links['example-client']['branch_id']=12;write_document(db,'links.json',links)
    with pytest.raises(ValueError):state.service.prefs_for_token(raw)
