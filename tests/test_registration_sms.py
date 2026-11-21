"""Registration SMS regressions. Every transport is synthetic and offline."""
from registration_samples import agreements
import json
import os
import pytest
from identity_service import OtpRateLimit
from sms_submit import encode_submit
from test_identity_product import identities, PHONE
from test_sim800c import fake_modem


def test_verification_code_is_one_cyrillic_sms_and_cooldown_is_precise(identities):
    s=identities
    raw=s.service.invite('owner','11',phone=PHONE)['link'].rsplit('/',1)[1]
    response=s.service.request_otp(raw, documents=agreements(s.service))
    assert len(encode_submit(PHONE,s.sent[-1][2]))==1
    assert response['retry_after']==60 and response['ready']
    s.clock[0]+=17.2
    with pytest.raises(OtpRateLimit) as limit:s.service.request_otp(raw, documents=agreements(s.service))
    assert limit.value.retry_after==43
    assert s.service.otp_status(raw)['retry_after']==43
    s.clock[0]+=43
    new=s.service.request_otp(raw, documents=agreements(s.service))
    assert new['challenge_id']!=response['challenge_id']


def test_status_rechecks_transport_and_never_exposes_code_or_another_invite(identities):
    s=identities
    raw=s.service.invite('owner','11',phone=PHONE)['link'].rsplit('/',1)[1]
    s.service.request_otp(raw, documents=agreements(s.service))
    s.service.delivery_status=lambda *args:{'status':'failed','code':'sms_network_timeout','private':'not public'}
    status=s.service.otp_status(raw)
    assert not status['ready'] and status['delivery']['code']=='sms_network_timeout'
    assert PHONE not in json.dumps(status) and 'private' not in status['delivery']
    other=s.service.invite('owner','12',phone='+15550200202')['link'].rsplit('/',1)[1]
    assert s.service.otp_status(other)['challenge_id'] is None


@pytest.mark.parametrize('cops,spn,expected',[
    ('+COPS: 0,0,"MTS RUS"','', 'MTS RUS'),
    ('+COPS: 0,2,"25020"','+CSPN: "Tele2",0', 'Tele2'),
    ('+COPS: 0,0,"004D00540053"','', 'MTS'),
    ('+COPS: 0','', None),
])
def test_operator_name_reading_does_not_select_network_or_dial(cops,spn,expected):
    modem,port=fake_modem({'AT+COPS?':'\r\n'+cops+'\r\nOK\r\n','AT+CSPN?':'\r\n'+spn+'\r\nOK\r\n'})
    try:
        assert modem.diagnostics().get('operator')==expected
        assert not any(c.startswith(('ATD','AT+COPS=')) for c in port.commands)
    finally:modem.close()


def test_optional_operator_command_rejection_preserves_modem_health():
    modem,port=fake_modem({'AT+COPS?':'\r\nERROR\r\n','AT+CSPN?':'\r\nERROR\r\n'})
    try:assert modem.diagnostics()['state']=='ok'
    finally:modem.close()
