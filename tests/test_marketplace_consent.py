import pytest

from marketplace_consent import current_installer


def event(second, before, after, source, actor):
    return {'changed_at': f'2026-01-01 12:00:{second:02d}', 'status_from': before,
            'status_to': after, 'source': source, 'user_id': actor}


def status(*events):
    return {'connection_status': {'salon_id': '101', 'marketplace_application_id': 202, 'status': 'active'},
            'logs': list(events)}


def test_latest_marketplace_activation_wins_independent_of_log_order():
    first = event(1, 'uninstalled', 'active', 'marketplace', 1)
    stop = event(2, 'active', 'uninstalled', 'marketplace', 3)
    latest = event(3, 'uninstalled', 'active', 'marketplace', 9)
    assert current_installer(status(latest, first, stop), 101, '202') == '9'
    assert current_installer(status(first, stop), 101, '202') is None


@pytest.mark.parametrize('delay', [0, 1])
def test_partner_callback_uses_user_of_adjacent_consent(delay):
    consent = event(2, 'uninstalled', 'pending', 'marketplace', 9)
    callback = event(2 + delay, 'pending', 'active', 'partner_api', 1)
    assert current_installer(status(callback, consent), 101, 202) == '9'


def test_callback_never_uses_an_actor_from_an_older_installation():
    stale = event(1, 'uninstalled', 'pending', 'marketplace', 9)
    stop = event(2, 'pending', 'uninstalled', 'marketplace', 9)
    callback = event(3, 'pending', 'active', 'partner_api', 1)
    assert current_installer(status(stale, stop, callback), 101, 202) is None
    assert current_installer(status(callback), 101, 202) is None


@pytest.mark.parametrize('changes', [{'salon_id': '102'}, {'marketplace_application_id': '203'},
                                     {'salon_id': None}, {'status': 'frozen'}])
def test_status_must_identify_this_active_installation(changes):
    data = status(event(1, 'uninstalled', 'active', 'marketplace', 9))
    data['connection_status'].update(changes)
    assert current_installer(data, 101, 202) is None


@pytest.mark.parametrize('changes', [{'user_id': True}, {'changed_at': 'broken'}, {'changed_at': '2026-02-31 12:00:00'},
                                     {'source': 'partner_api'}, {'marketplace_application_id': 203}])
def test_untrusted_or_malformed_log_never_supplies_an_identity(changes):
    row = event(1, 'uninstalled', 'active', 'marketplace', 9)
    assert current_installer(status({**row, **changes}), 101, 202) is None


def test_ambiguous_order_does_not_fall_back_to_any_actor():
    first = event(1, 'uninstalled', 'active', 'marketplace', 9)
    other = {**first, 'user_id': 11}
    assert current_installer(status(first, other), 101, 202) is None
    assert current_installer(status(first, {**other, 'changed_at': '2026-01-01T13:00:00+03:00'}), 101, 202) is None
