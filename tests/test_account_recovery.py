"""Offline identity proof, account boundaries and one-time password recovery."""
from registration_samples import agreements
import re
from flask import Flask,session
import pytest
from test_identity_product import identities,accept,PASSWORD,PHONE
from identity_service import valid_email,valid_phone,OtpRateLimit
from password_recovery import PasswordRecovery
from storage.sql_documents import read_document,write_document

BROWSER='offline-browser-capability-1234567890'
NEW_PASSWORD='new-offline-account-password'

def request(state,phone=PHONE,browser=BROWSER):
    service=PasswordRecovery(state.service)
    result=service.request_code(phone,browser,'127.0.0.1')
    code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    return service,result['request_token'],code

@pytest.mark.parametrize('email',['login','bad@domain','a..b@example.invalid','a\n@example.invalid','<bad>@example.invalid','x@-bad.invalid','a@@example.invalid'])
def test_email_validation_rejects_malformed_values(email):
    with pytest.raises(ValueError):valid_email(email)

def test_email_and_phone_normalization():
    assert valid_email('  Person+Tag@EXAMPLE.INVALID  ')=='person+tag@example.invalid'
    assert valid_email('owner@почта.рф')=='owner@xn--80a1acny.xn--p1ai'
    assert valid_phone('8 (920) 000.00-00')=='+79200000000'
    assert valid_phone('920\u00a0000 00 00')=='+79200000000'
    assert valid_phone('+81 3 1234 5678')=='+81312345678'
    assert valid_phone('+9924412345')=='+9924412345'
    with pytest.raises(ValueError):valid_phone('+1555000ABC')

def test_legacy_marketplace_setup_requires_verified_password_recovery(identities):
    state=identities
    def flag_unfinished():
        with state.product.connect() as db:
            admins=read_document(db,'admins.json')
            admins['north']['marketplace_password_setup_required']=True
            write_document(db,'admins.json',admins)
    flag_unfinished()
    assert not state.auth.verify_admin('north',PASSWORD)
    service,raw,code=request(state,'+15550100888')
    reset=service.verify_code(raw,code,BROWSER)
    service.complete(reset,BROWSER,NEW_PASSWORD,NEW_PASSWORD)
    assert state.auth.verify_admin('north',NEW_PASSWORD)
    # Earlier releases left the flag set on accounts already recovered by SMS.
    flag_unfinished()
    state.service.import_legacy()
    assert state.auth.verify_admin('north',NEW_PASSWORD)

def test_new_ordinary_accounts_require_email_and_keep_otp_unused_on_bad_email(identities):
    state=identities;invite=state.service.invite('owner','11',client_token='example-client')
    raw=invite['link'].rsplit('/',1)[1];otp=state.service.request_otp(raw, documents=agreements(state.service))
    code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    with pytest.raises(ValueError):state.service.accept_invitation(raw,otp['challenge_id'],code,'Example','plain-login',PASSWORD, documents=agreements(state.service))
    result=state.service.accept_invitation(raw,otp['challenge_id'],code,'Example','owner@почта.рф',PASSWORD, documents=agreements(state.service))
    assert state.auth.verify_admin('owner@почта.рф',PASSWORD)
    assert result['username']=='owner@xn--80a1acny.xn--p1ai'

@pytest.mark.parametrize('field,value',[('name','Changed'),('branch_name','Changed branch'),('phone','+15550000123'),('branch_id','100'),('yclients_enabled',False)])
def test_registration_cannot_complete_with_changed_verified_details(identities,field,value):
    state=identities
    raw=state.service.register_branch('99','Original branch','Original name',PHONE)['registration_token']
    otp=state.service.request_otp(raw,registration=True, documents=agreements(state.service));code=re.search(r'\b[0-9]{6}\b',state.sent[-1][2]).group()
    details={'name':'Original name','branch_name':'Original branch','phone':PHONE,'branch_id':'99','yclients_enabled':True}
    with pytest.raises(ValueError):
        state.service.complete_registration(raw,otp['challenge_id'],code,'new@example.invalid',PASSWORD,details={**details,field:value}, documents=agreements(state.service))
    assert state.branches.get_branch('99') is None
    assert 'new@example.invalid' not in state.auth.load_admins()
    state.service.complete_registration(raw,otp['challenge_id'],code,'new@example.invalid',PASSWORD,details=details, documents=agreements(state.service))

def test_new_email_is_login_and_welcome_runs_once_in_transaction(identities):
    state=identities;welcomes=[]
    def welcome(person_id,branch,username):
        with state.product.connect() as db:
            person=db.execute('SELECT * FROM persons WHERE id=?',(person_id,)).fetchone()
            assert person['email']==username and not person['email_verified']
            assert username in read_document(db,'admins.json')
        welcomes.append((person_id,branch,username))
    state.service.welcome_sink=welcome
    result,raw,otp,code=accept(state,username='  New.Admin@Example.Invalid ')
    assert result['username']=='new.admin@example.invalid' and len(welcomes)==1
    assert state.auth.verify_admin(' NEW.ADMIN@EXAMPLE.INVALID ',PASSWORD)
    assert state.auth.verify_admin('north',PASSWORD) # Legacy login retained.
    with pytest.raises(ValueError):state.service.accept_invitation(raw,otp['challenge_id'],code,'Name','again@example.invalid',PASSWORD, documents=agreements(state.service))
    assert len(welcomes)==1

def test_request_does_not_mutate_account_and_reset_invalidates_sessions(identities):
    state=identities;accepted,*_=accept(state);state.clock[0]+=61
    app=Flask(__name__);app.secret_key='offline-recovery-secret'
    with app.test_request_context():
        assert state.auth.login_admin(accepted['username'],PASSWORD)
        before=state.auth.load_admins()
        recovery,raw,code=request(state)
        assert state.auth.load_admins()==before and state.auth.get_current_admin()
        reset=recovery.verify_code(raw,code,BROWSER)
        assert state.auth.load_admins()==before and state.auth.get_current_admin()
        with pytest.raises(ValueError):recovery.complete(reset,BROWSER,NEW_PASSWORD,'different')
        result=recovery.complete(reset,BROWSER,NEW_PASSWORD,NEW_PASSWORD)
        assert result['username']==accepted['username']
        assert state.auth.get_current_admin() is None
        assert state.auth.verify_admin(accepted['username'],NEW_PASSWORD)
        assert not state.auth.verify_admin(accepted['username'],PASSWORD)
        with pytest.raises(ValueError):recovery.complete(reset,BROWSER,NEW_PASSWORD,NEW_PASSWORD)
        with pytest.raises(ValueError):recovery.verify_code(raw,code,BROWSER)

def test_reset_challenge_bound_to_browser_expiry_attempt_budget_and_shared_limits(identities):
    state=identities;accept(state);state.clock[0]+=61
    recovery,raw,code=request(state)
    with pytest.raises(ValueError):recovery.verify_code(raw,code,'another-browser-capability-1234567890')
    with pytest.raises(OtpRateLimit):recovery.request_code(PHONE,BROWSER)
    wrong='999999' if code!='999999' else '000000'
    for _ in range(5):
        with pytest.raises(ValueError):recovery.verify_code(raw,wrong,BROWSER)
    with pytest.raises(ValueError):recovery.verify_code(raw,code,BROWSER)
    state.clock[0]+=61;recovery,raw,code=request(state);state.clock[0]+=301
    with pytest.raises(ValueError):recovery.verify_code(raw,code,BROWSER)

@pytest.mark.parametrize('phone',['+15550100999','+15559990000'])
def test_unknown_and_super_accounts_get_no_sms_or_account_mutation(identities,phone):
    state=identities;before=state.auth.load_admins();count=len(state.sent)
    result=PasswordRecovery(state.service).request_code(phone,BROWSER)
    assert result['request_token'] and result['retry_after']==60
    assert len(state.sent)==count and state.auth.load_admins()==before

def test_reset_never_uses_registration_redirect_and_revokes_bot_admin_proof(identities,monkeypatch):
    state=identities;accepted,*_=accept(state)
    ticket=state.service.issue_bind_ticket(accepted['username'],'telegram');state.service.bind_ticket('telegram','5500123',ticket)
    pending=state.service.issue_bind_ticket(accepted['username'],'max')
    monkeypatch.setattr('registration_sms.sms_recipient',lambda phone:pytest.fail('Reset cannot use development redirects'))
    state.clock[0]+=61;recovery,raw,code=request(state)
    assert state.sent[-1][1]==PHONE
    reset=recovery.verify_code(raw,code,BROWSER);recovery.complete(reset,BROWSER,NEW_PASSWORD,NEW_PASSWORD)
    assert state.service.bot_admin('telegram','5500123') is None
    with pytest.raises(ValueError):state.service.bind_ticket('max','5500123',pending)

@pytest.mark.parametrize('change',['disabled','branch'])
def test_account_mutation_after_otp_invalidates_reset_capability(identities,change):
    state=identities;accepted,*_=accept(state);state.clock[0]+=61
    recovery,raw,code=request(state);reset=recovery.verify_code(raw,code,BROWSER)
    if change=='branch':
        with state.product.connect() as db:db.execute('INSERT INTO branch_memberships VALUES(?,?,"active",?)',(accepted['person_id'],'12',state.clock[0]))
    with state.auth.store.transaction() as admins:admins[accepted['username']]['disabled' if change=='disabled' else 'branch_id']=True if change=='disabled' else 12
    with pytest.raises(ValueError):recovery.complete(reset,BROWSER,NEW_PASSWORD,NEW_PASSWORD)
    assert state.auth.verify_password(PASSWORD,state.auth.load_admins()[accepted['username']]['password_hash'])

def test_verified_reset_session_expires_after_ten_minutes(identities):
    state=identities;accept(state);state.clock[0]+=61
    recovery,raw,code=request(state);reset=recovery.verify_code(raw,code,BROWSER);state.clock[0]+=601
    with pytest.raises(ValueError):recovery.complete(reset,BROWSER,NEW_PASSWORD,NEW_PASSWORD)

def test_legacy_extra_memberships_preserved_but_no_role_grant_or_invites(identities):
    state=identities;accepted,*_=accept(state);person=accepted['person_id']
    second=state.links.create_link({'branch_id':12,'phone':PHONE,'user_name':'Historical','gate_access':True,'access_source':'manual'})
    with state.product.connect() as db:
        db.execute('INSERT INTO branch_memberships VALUES(?,?,"active",?)',(person,'12',state.clock[0]))
        links=read_document(db,'links.json');links[second].update(admin_person_id=person,admin_role_active=True)
        write_document(db,'links.json',links);write_document(db,'admins.json',read_document(db,'admins.json'))
    assert state.links.load_links()[second]['admin_role_active'] is False
    assert state.links.load_links()[second]['gate_access'] is True
    with state.product.connect() as db:assert db.execute('SELECT 1 FROM branch_memberships WHERE person_id=? AND branch_id="12"',(person,)).fetchone()
    with pytest.raises(ValueError):state.service.invite('owner','12',phone=PHONE)
    with state.product.connect() as db:
        with pytest.raises(ValueError):state.service._membership(db,person,'12')
    assert next(row for row in state.service.list_admins('owner','12') if row['person_id']==person)['status']=='inactive'

def test_historical_memberships_do_not_count_as_remaining_administrators(identities):
    state=identities;accepted,*_=accept(state)
    north=state.service.web_admin('north')['person_id']
    with state.product.connect() as db:
        db.execute('INSERT INTO branch_memberships VALUES(?,?,"active",?)',(north,'12',state.clock[0]))
    with state.auth.store.transaction() as admins:admins['north']['branch_id']=12
    with pytest.raises(ValueError,match='другого администратора'):
        state.service.revoke_admin('owner',accepted['person_id'],'11')

def test_revoke_target_must_belong_to_branch_and_archived_role_keeps_other_sessions(identities):
    state=identities;accepted,*_=accept(state);person=accepted['person_id']
    before=state.auth.load_admins()
    for value in (None,[],state.service.web_admin('owner')['person_id']):
        with pytest.raises(ValueError):state.service.revoke_admin('north',value,'11')
    assert state.auth.load_admins()==before
    with state.product.connect() as db:db.execute('INSERT INTO branch_memberships VALUES(?,?,"active",?)',(person,'12',state.clock[0]))
    state.service.revoke_admin('owner',person,'12')
    assert state.auth.load_admins()==before
    assert state.service.web_admin(accepted['username'])['branch_ids']==['11']
