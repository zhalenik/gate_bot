"""The two CRM connection modes cannot silently overwrite each other."""
import pytest

from test_yclients_marketplace import marketplace,begin
from test_identity_product import identities


def direct(state):
    state.identity.service.save_integration('owner','11','yclients',{'user_token':'offline-direct-token'},verified_direct=True)


@pytest.mark.parametrize('event',['freeze','uninstall'])
def test_late_marketplace_callback_preserves_confirmed_direct_credentials(marketplace,event):
    s=marketplace;direct(s)
    s.service.disconnect({'salon_id':11,'application_id':123,'partner_token':'offline-partner-key','event':event})
    assert s.identity.service.integration_credentials('11','yclients')['user_token']=='offline-direct-token'
    branch=s.identity.branches.load_branches()['11']
    assert branch['yclients_connection_source']=='direct' and 'yclients_marketplace_state' not in branch
    assert not s.identity.service.integration_status('owner','11')['yclients']['blocked']


@pytest.mark.parametrize('provider_state',['active','frozen','uninstalled'])
def test_marketplace_mode_does_not_allow_direct_credentials_even_after_disconnect(marketplace,provider_state):
    s=marketplace;s.service.connect(begin(s),s.browser,'owner','11')
    if provider_state!='active':
        s.service.disconnect({'salon_id':11,'application_id':123,'partner_token':'offline-partner-key','event':'freeze' if provider_state=='frozen' else 'uninstall'})
    with pytest.raises(PermissionError):direct(s)
    assert s.identity.branches.load_branches()['11']['yclients_connection_mode']=='marketplace'
    assert not s.identity.service.integration_status('owner','11')['yclients']['manual_allowed']


def test_explicit_new_marketplace_connection_can_replace_direct(marketplace):
    s=marketplace;direct(s)
    assert s.service.connect(begin(s),s.browser,'owner','11')['state']=='active'
    assert s.identity.service.integration_credentials('11','yclients')['user_token']=='offline-system-user-token'
    assert s.identity.branches.load_branches()['11']['yclients_connection_source']=='marketplace'
