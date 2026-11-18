"""Standalone tenants keep stable identity, manual grants and ordinary billing."""
from concurrent.futures import ThreadPoolExecutor
import re
from unittest.mock import Mock

import pytest

from branch_manager import BranchManager, INTERNAL_BRANCH_ID_START
from billing_service import month_at
from link_manager import access_is_active
from scheduler import Scheduler
from storage import config
from storage.sql_documents import read_document, write_document
from yclients_api import YClientsClient, YClientsReadOnlyError
from test_identity_product import identities, PASSWORD, PHONE


def standalone(state, username='standalone@example.invalid'):
    reg=state.service.register_branch(None,'Local access','Local owner',PHONE,yclients_enabled=False)
    otp=state.service.request_otp(reg['registration_token'],registration=True)
    code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    return state.service.complete_registration(reg['registration_token'],otp['challenge_id'],code,username,PASSWORD,billing=state.billing)


def test_registration_without_crm_auto_activates_unlimited_trial_and_no_routes(identities):
    state=identities;result=standalone(state);branch=state.branches.get_branch(result['branch_id'])
    assert branch.branch_id>=INTERNAL_BRANCH_ID_START
    assert branch.enabled and not branch.yclients_enabled and branch.id_origin=='internal' and not branch.buttons
    assert result['status']=='active' and state.auth.verify_admin('standalone@example.invalid',PASSWORD)
    with state.product.connect() as db:
        raw=read_document(db,'branches.json')[result['branch_id']]
        assert raw['hardware_routes_verified'] is False
        terms=db.execute('SELECT * FROM billing_terms WHERE branch_id=? AND source="trial"',(result['branch_id'],)).fetchall()
        assert len(terms)==1 and terms[0]['quota'] is None
        assert terms[0]['starts']==state.clock[0]
        assert terms[0]['ends']==month_at(state.clock[0],2,'Europe/Moscow')
    with pytest.raises(ValueError):state.service.activate_branch('owner',result['branch_id'],state.billing)


def test_default_legacy_and_crm_registration_keep_old_ids_and_pending_review(identities):
    state=identities;original=set(state.branches.load_branches())
    assert all(branch.yclients_enabled for branch in state.branches.get_all_branches())
    reg=state.service.register_branch('00123','CRM branch','CRM owner',PHONE)
    otp=state.service.request_otp(reg['registration_token'],registration=True);code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    result=state.service.complete_registration(reg['registration_token'],otp['challenge_id'],code,'crm-owner@example.invalid',PASSWORD)
    assert result['status']=='pending_verification' and result['branch_id']=='123'
    assert not state.branches.get_branch('123').enabled and state.branches.get_branch('123').yclients_enabled
    assert original<=set(state.branches.load_branches())
    with state.product.connect() as db:assert not db.execute('SELECT 1 FROM billing_terms WHERE branch_id="123" AND source="trial"').fetchone()


def test_ids_skip_existing_and_pending_and_are_concurrency_safe(identities):
    state=identities;start=INTERNAL_BRANCH_ID_START
    state.branches.add_branch({'branch_id':start,'name':'Existing numeric','buttons':[]})
    state.service.register_branch(str(start+1),'Pending CRM','Example',PHONE)
    def register(index):
        return state.service.register_branch(None,'Local','Example','+1555020'+str(index).zfill(4),yclients_enabled=False)
    with ThreadPoolExecutor(max_workers=4) as pool:rows=list(pool.map(register,range(8)))
    with state.product.connect() as db:
        ids=[int(row['branch_id']) for row in db.execute('SELECT branch_id FROM branch_registrations WHERE yclients_enabled=0')]
    assert len(set(ids))==8 and min(ids)==start+2
    assert state.branches.get_branch(start).name=='Existing numeric'
    assert len(rows)==8


@pytest.mark.parametrize('value',[None,'false',0,1,{},[]])
def test_flag_is_strict_boolean(identities,value):
    with pytest.raises(ValueError):identities.service.register_branch(None,'Local','Owner',PHONE,yclients_enabled=value)


def test_standalone_cannot_claim_external_id_or_reenable_crm_on_internal_id(identities):
    state=identities
    with pytest.raises(ValueError):state.service.register_branch('11','Local','Owner',PHONE,yclients_enabled=False)
    result=standalone(state)
    with pytest.raises(ValueError):state.branches.update_settings(result['branch_id'],{'yclients_enabled':True})
    assert not state.branches.get_branch(result['branch_id']).yclients_enabled


def test_scheduler_skips_standalone_without_revoking_manual_or_appointment_rights(identities):
    state=identities;state.branches.update_settings('11',{'yclients_enabled':False})
    token=state.links.create_link({'branch_id':11,'phone':'+15550200111','gate_access':True,'access_source':'appointment'},expires_at='2099-01-01T00:00:00+00:00')
    before=state.links.load_links();crm=Mock();crm.records_for_day.return_value=[];events=[]
    result=Scheduler(state.links,state.branches,crm,event_sink=lambda *args:events.append(args)).run_once()
    assert result['11']=={'status':'skipped','reason':'standalone'}
    calls=[call.args[0] for call in crm.records_for_day.call_args_list]
    assert calls and set(calls)=={12}
    after=state.links.load_links()
    for key in ('example-client',token):assert after[key]==before[key] and access_is_active(after[key])
    assert not any(args[1]==11 for args in events)


def test_readonly_crm_boundary_rejects_standalone_before_credentials_or_network(identities,monkeypatch):
    state=identities;state.branches.update_settings('11',{'yclients_enabled':False});monkeypatch.setattr(config,'YCLIENTS_ENABLED',True)
    transport=Mock();credentials=Mock()
    client=YClientsClient(session=transport,branches=state.branches,credential_provider=credentials,pace=lambda:None)
    with pytest.raises(YClientsReadOnlyError):client.request('GET','records/11','11')
    credentials.assert_not_called();transport.get.assert_not_called()


def test_standalone_chatpush_rejected_for_preferences_invites_and_integrations(identities):
    state=identities
    state.service.save_prefs_for_token('example-client',{'enabled':True,'channels':{'messengers':True}})
    state.branches.update_settings('11',{'yclients_enabled':False})
    prefs=state.service.prefs_for_token('example-client')
    assert 'chatpush' not in prefs['allowed_channels'] and 'sms' not in prefs['channels']
    assert 'messengers' not in prefs['allowed_channels'] and 'messengers' not in prefs['channels']
    assert 'messengers' not in prefs['effective_channels']
    before=len(state.sent)
    with pytest.raises(PermissionError):state.service.invite('owner','11',client_token='example-client',channel='chatpush')
    with pytest.raises(ValueError):state.service.save_prefs_for_token('example-client',{'enabled':True,'email':'','channels':{'chatpush':True}})
    with pytest.raises(PermissionError):state.service.integration_status('owner','11')
    with pytest.raises(PermissionError):state.service.save_integration('owner','11','yclients',{'user_token':'offline-example'})
    assert state.service.integration_credentials('11','yclients')=={}
    assert state.service.integration_credentials('11','chatpush')=={}
    assert len(state.sent)==before


def test_registration_flag_migrates_existing_pending_rows_as_yclients(identities):
    state=identities
    with state.product.connect() as db:
        db.execute('ALTER TABLE branch_registrations DROP COLUMN yclients_enabled')
    from identity_service import IdentityService
    reopened=IdentityService(state.product,sender=state.service.sender,clock=state.service.clock)
    reg=reopened.register_branch('123','CRM','Owner',PHONE)
    with state.product.connect() as db:
        row=db.execute('SELECT yclients_enabled FROM branch_registrations WHERE token_hash=?',(reopened._hash(reg['registration_token']),)).fetchone()
        assert row['yclients_enabled']==1
