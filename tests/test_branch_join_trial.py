"""SMS onboarding and durable trial eligibility. All contacts and transports are offline fixtures."""
from datetime import datetime, timezone
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from branch_join import BranchJoin
from branch_lifecycle import BranchLifecycleService
from storage.sql_documents import read_document, write_document
from test_identity_product import identities, PASSWORD, PHONE
from timeutil import end_of_day


def code(state):
    return re.search(r'\b\d{6}\b', state.sent[-1][2]).group()


def join_flow(state, phone, crm=True):
    with state.product.connect() as db:
        branches = read_document(db, 'branches.json')
        branches['11'].update(yclients_enabled=crm,timezone='Asia/Vladivostok')
        write_document(db, 'branches.json', branches)
    directory = SimpleNamespace(_availability=Mock(return_value={'ready': True}),crm=Mock())
    directory.crm.find_clients_by_phone.return_value = []
    service = BranchJoin(state.service, state.links, directory)
    raw = service.admin_view('north','11','create')['url'].rsplit('/',1)[1]
    with pytest.raises(PermissionError):
        service.admin_view('north','12','create')
    with pytest.raises(ValueError):
        service.profile(raw,'browser')
    challenge = service.code(raw,'browser',phone,'127.0.0.1')['challenge_id']
    assert state.sent[-1][1] == phone
    with pytest.raises(ValueError):
        service.verify(raw,'other',challenge,code(state))
    service.verify(raw,'browser',challenge,code(state))
    directory.crm.find_clients_by_phone.assert_not_called()
    return service, raw, directory.crm.find_clients_by_phone


@pytest.mark.parametrize('crm',[False, True])
def test_new_guest_end_of_branch_day_and_rotation(identities, crm):
    state = identities
    svc, raw, lookup = join_flow(state, '+15550100222', crm=crm)
    assert svc.profile(raw,'browser') == {'name':'','email':'','existing':False}
    assert lookup.call_count == int(crm)
    with pytest.raises(ValueError):
        svc.finish(raw,'other','Guest','')
    url = svc.finish(raw,'browser','Guest','guest@example.invalid')['url']
    assert svc.finish(raw,'browser','Guest','')['url'] == url
    record = state.links.load_links()[url.split('/')[-1]]
    assert record['is_temporary'] and record['gate_access'] and record['branch_id']==11
    assert record['access_expires'] == end_of_day(datetime.fromtimestamp(state.clock[0], timezone.utc),'Asia/Vladivostok').isoformat()
    svc.admin_view('north','11','rotate')
    with pytest.raises(ValueError):
        svc.finish(raw,'browser','Guest','')


def test_yclients_lookup_failure_retry_and_existing_block(identities):
    state = identities
    state.links.toggle_user_access('example-client',False)
    svc, raw, lookup = join_flow(state, PHONE)
    lookup.side_effect = RuntimeError('offline')
    with pytest.raises(ValueError):
        svc.profile(raw,'browser')
    with pytest.raises(ValueError):
        svc.finish(raw,'browser','Guest','')
    lookup.side_effect = None
    lookup.return_value = [{'phone':PHONE,'name':'CRM guest','email':'crm@example.invalid','id':91}]
    profile = svc.profile(raw,'browser')
    assert profile['existing'] and profile['email']=='crm@example.invalid'
    assert svc.finish(raw,'browser','','')['url']=='/opn/example-client'
    card = state.links.load_links()['example-client']
    assert card['admin_blocked'] and card['id_ycl']==91


def test_crm_match_gets_own_profile_and_appointment_controls_access(identities):
    state = identities
    phone = '+15550100222'
    svc, raw, lookup = join_flow(state,phone)
    lookup.return_value = [{'phone':phone,'name':'A','email':'crm@example.invalid','id':81}]
    profile = svc.profile(raw,'browser')
    assert profile == {'name':'A','email':'crm@example.invalid','existing':False}
    token = svc.finish(raw,'browser',profile['name'],profile['email'])['url'].split('/')[-1]
    card = state.links.load_links()[token]
    assert card['id_ycl']==81 and not card['is_temporary'] and not card['gate_access']
    at=datetime.fromtimestamp(state.clock[0],timezone.utc)
    state.links.reconcile_appointments(11,{81:{'id':81,'phone':phone}},at=at)
    assert state.links.load_links()[token]['gate_access']


def register_trial(state, branch, phone):
    state.clock[0] += 62
    registration = state.service.register_branch(branch,'Example','Admin',phone,yclients_enabled=bool(branch))
    ticket=registration['registration_token']
    challenge=state.service.request_otp(ticket,registration=True)['challenge_id']
    result=state.service.complete_registration(ticket,challenge,code(state),'join@example.invalid',PASSWORD,billing=state.billing)
    return result['branch_id'], state.billing.subscription_view(result['branch_id'],at=state.clock[0])['status']


def test_trial_identity_survives_real_branch_deletion_and_is_separate_by_type(identities):
    state=identities
    delete=BranchLifecycleService(state.service)
    other='+15550100188'
    # Same phone across systems, and different salons for the same phone, are independent.
    for salon, phone, expected in [(None,PHONE,'trial'),('91',PHONE,'trial'),('92',PHONE,'trial'),
                                   ('91',other,'cancelled'),(None,other,'trial'),(None,PHONE,'cancelled')]:
        branch,status = register_trial(state,salon,phone)
        assert status==expected
        if expected=='cancelled':
            state.billing.grant_plan_gift(branch,actor='owner',days=7,quota=12,mode='replace',at=state.clock[0])
            assert state.billing.subscription_view(branch,at=state.clock[0])['available']
        preview=delete.preview('owner',branch)
        delete.delete('owner',branch,confirmation=branch,revision=preview['revision'])
    with state.product.connect() as db:
        marks = db.execute('SELECT * FROM trial_redemptions').fetchall()
        assert len(marks)==4 and all(len(row['digest'])==64 for row in marks)


def test_split_historical_owner_client_preserves_operator_channels_and_bearer(identities):
    s=identities
    owner=s.service.web_admin('owner')['person_id']
    s.service.bind_ticket('telegram','291',s.service.issue_bind_ticket('owner','telegram'))
    with s.product.connect() as db:
        cards=read_document(db,'links.json');card=cards['example-client']
        db.execute('UPDATE branch_clients SET person_id=? WHERE legacy_token=?',(owner,'example-client'))
        card.update(person_id=owner,admin_role_active=True,admin_person_id=owner,email='old@example.invalid')
        write_document(db,'links.json',cards)
    s.service.import_legacy()
    card=s.links.load_links()['example-client']
    assert card['person_id']!=owner and card['gate_access'] and not card.get('admin_role_active')
    assert s.service.admin_preferences('owner')['email']=='old@example.invalid'
    s.links.update_user('example-client',{'phone':'+15550100200','email':'client@example.invalid'})
    assert s.service.bot_admin('telegram','291')['person_id']==owner
    assert s.service.admin_preferences('owner')['email']=='old@example.invalid'
