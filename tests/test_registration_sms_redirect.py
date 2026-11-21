"""Local registration routing is opt-in and cannot verify a phone in production."""
from registration_samples import agreements
import re
import pytest
from storage import config
from registration_sms import sms_recipient
from test_identity_product import identities, PHONE, PASSWORD
from test_gate_qr import qr

DESTINATION = '+15550999999'


@pytest.fixture
def redirect(monkeypatch):
    monkeypatch.setattr(config,'ENVIRONMENT','development')
    monkeypatch.setattr(config,'EXPLICIT_DEVELOPMENT',True)
    monkeypatch.setattr(config,'DEV_REGISTRATION_SMS_REDIRECT_ENABLED',True)
    monkeypatch.setattr(config,'DEV_REGISTRATION_SMS_PREFIX','+1555010')
    monkeypatch.setattr(config,'DEV_REGISTRATION_SMS_PHONE',DESTINATION)


@pytest.mark.parametrize('environment',['production','staging','test','Production',''])
def test_non_development_never_redirects_even_when_enabled(redirect,monkeypatch,environment):
    monkeypatch.setattr(config,'ENVIRONMENT',environment)
    assert sms_recipient(PHONE)==PHONE


def test_disabled_missing_explicit_environment_and_unmatched_numbers(redirect,monkeypatch):
    assert sms_recipient('+15550200101')=='+15550200101'
    monkeypatch.setattr(config,'EXPLICIT_DEVELOPMENT',False)
    assert sms_recipient(PHONE)==PHONE
    monkeypatch.setattr(config,'EXPLICIT_DEVELOPMENT',True)
    monkeypatch.setattr(config,'DEV_REGISTRATION_SMS_REDIRECT_ENABLED',False)
    assert sms_recipient(PHONE)==PHONE


@pytest.mark.parametrize('key,value',[
    ('DEV_REGISTRATION_SMS_PREFIX',''),('DEV_REGISTRATION_SMS_PREFIX','+'),
    ('DEV_REGISTRATION_SMS_PHONE',''),('DEV_REGISTRATION_SMS_PHONE','invalid'),
])
def test_invalid_local_configuration_fails_before_any_delivery(redirect,identities,monkeypatch,key,value):
    monkeypatch.setattr(config,key,value)
    raw=identities.service.register_branch(None,'Test','Owner',PHONE,yclients_enabled=False)['registration_token']
    with pytest.raises(ValueError):identities.service.request_otp(raw,registration=True, documents=agreements(identities.service))
    assert not identities.sent


@pytest.mark.parametrize('registration',[True,False])
def test_original_phone_remains_identity_and_redirect_only_changes_sms_target(redirect,identities,registration):
    s=identities
    if registration:
        raw=s.service.register_branch(None,'Test','Owner',PHONE,yclients_enabled=False)['registration_token']
    else:
        raw=s.service.invite('owner','11',phone=PHONE)['link'].rsplit('/',1)[1]
        assert s.sent[-1][1]==PHONE  # Invitation itself is not redirected.
    response=s.service.request_otp(raw,registration=registration, documents=agreements(s.service))
    assert s.sent[-1][1]==DESTINATION and response['phone_hint'].endswith(PHONE[-4:])
    assert '_development_redirect' not in response['delivery']
    assert s.service.otp_status(raw,registration=registration)['ready']
    code=re.search(r'\b[0-9]{6}\b',s.sent[-1][2]).group()
    if registration:
        s.service.complete_registration(raw,response['challenge_id'],code,'test-owner@example.invalid',PASSWORD,billing=s.billing, documents=agreements(s.service))
    else:
        s.service.accept_invitation(raw,response['challenge_id'],code,'Test owner','test-owner@example.invalid',PASSWORD, documents=agreements(s.service))
    assert s.service.web_admin('test-owner@example.invalid')['phone']==PHONE


def test_qr_and_other_sms_keep_original_destination(redirect,qr):
    response=qr.svc.request_code(qr.raw,PHONE,'offline-browser','127.0.0.1')
    assert response['ready'] and qr.base.sent[-1][1]==PHONE
    qr.base.service._send('sms',PHONE,'Ordinary notice',{'purpose':'notification'})
    assert qr.base.sent[-1][1]==PHONE


@pytest.mark.parametrize('change',['production','disabled','target_changed','prefix_changed','implicit_environment'])
def test_development_code_cannot_be_consumed_after_configuration_changes(redirect,identities,monkeypatch,change):
    s=identities
    raw=s.service.register_branch(None,'Test','Owner',PHONE,yclients_enabled=False)['registration_token']
    response=s.service.request_otp(raw,registration=True, documents=agreements(s.service))
    code=re.search(r'\b[0-9]{6}\b',s.sent[-1][2]).group()
    # Hardware reconciliation does not erase the original redirect marker.
    s.service.delivery_status=lambda *args:{'status':'network_accepted','delivery':'network_accepted'}
    if change=='production':monkeypatch.setattr(config,'ENVIRONMENT','production')
    elif change=='disabled':monkeypatch.setattr(config,'DEV_REGISTRATION_SMS_REDIRECT_ENABLED',False)
    elif change=='target_changed':monkeypatch.setattr(config,'DEV_REGISTRATION_SMS_PHONE','+15550888888')
    elif change=='prefix_changed':monkeypatch.setattr(config,'DEV_REGISTRATION_SMS_PREFIX','+1555020')
    else:monkeypatch.setattr(config,'EXPLICIT_DEVELOPMENT',False)
    assert not s.service.otp_status(raw,registration=True)['ready']
    with pytest.raises(ValueError):
        s.service.complete_registration(raw,response['challenge_id'],code,'test-owner@example.invalid',PASSWORD,billing=s.billing, documents=agreements(s.service))
    with pytest.raises(PermissionError):s.service.web_admin('test-owner@example.invalid')
