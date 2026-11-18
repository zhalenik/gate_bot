"""GET-only phone lookup: complete pages and exact, unmasked identities."""
import pytest

from storage import config
from yclients_api import YClientsError, YClientsReadOnlyError
from test_yclients_readonly import Response, client, crm_config


PHONE = '+79990000101'


def row(identifier, phone=PHONE, company=1, **fields):
    return {'id': identifier, 'phone': phone, 'company_id': company, 'name': 'Synthetic',
            'email': 'synthetic@example.invalid', **fields}


def page(rows, *, total=None, number=None, meta=None):
    details = dict(meta or {})
    if total is not None:
        details['total_count'] = total
    if number is not None:
        details['page'] = number
    return Response({'success': True, 'data': rows, 'meta': details})


def test_exact_phone_matches_use_documented_get_filter_and_minimal_result(crm_config):
    api, session = client(crm_config, [page([
        row(11, 79990000101, extra='private response field'),
        row(12, '+179990000101'),  # Contains the search digits but is not equal.
    ], total=2, number=1)])
    result = api.find_clients_by_phone(1, '8 (999) 000-01-01')
    assert result == [{'id': 11, 'phone': PHONE, 'name': 'Synthetic', 'email': 'synthetic@example.invalid'}]
    url, kwargs = session.calls[0]
    assert url == 'https://api.yclients.ru/api/v1/clients/1'
    assert kwargs['params'] == {'phone': '79990000101', 'page': 1, 'count': 100}
    assert kwargs['headers']['Authorization'] == 'Bearer synthetic-partner-token, User synthetic-user-token-1'
    assert kwargs['allow_redirects'] is False and kwargs['timeout'] == (5, 20)
    assert kwargs.get('verify', True) is True
    assert 'phone' not in url and 'extra' not in result[0]


def test_all_exact_matches_are_returned_only_after_complete_pagination(crm_config):
    api, session = client(crm_config, [
        page([row(11), row(12, '+179990000101')], total='3', number='1'),
        page([row('13', '89990000101')], total=3, number=2),
    ])
    assert [item['id'] for item in api.find_clients_by_phone(1, '9990000101', page_size=2)] == [11, 13]
    assert [options['params']['page'] for _, options in session.calls] == [1, 2]


def test_missing_total_requires_a_short_terminal_page(crm_config):
    api, session = client(crm_config, [page([row(11), row(12)]), page([])])
    assert len(api.find_clients_by_phone(1, PHONE, page_size=2)) == 2
    assert len(session.calls) == 2


def test_empty_search_has_no_match(crm_config):
    api, session = client(crm_config, [page([], total=0, number=1)])
    assert api.find_clients_by_phone(1, PHONE) == []
    assert len(session.calls) == 1


@pytest.mark.parametrize('value', ['', None, True, 79990000101, '+7***0000101', '+7 999 abc 01 01',
                                   '+7999+0000101', '++79990000101', '123456', ['79990000101']])
def test_invalid_search_phone_never_reaches_transport(crm_config, value):
    api, session = client(crm_config, [])
    with pytest.raises(YClientsError):
        api.find_clients_by_phone(1, value)
    assert session.calls == []


@pytest.mark.parametrize('rows', [
    [row(1, company=2)], [row(True)], [row(0)], [row(-1)], [row('1/2')], [row(None)],
    [row(1, None)], [row(1, '')], [row(1, '+7 (999) XXX-01-01')], [row(1, {'hidden': True})],
    [row(1, True)], [row(1, 79990000101.0)], [row(1, '++79990000101')],
    [row(1), row(2, 'hidden')],  # A visible match must not conceal another masked candidate.
    [row(1), row(1)], ['not a client'],
])
def test_invalid_or_masked_candidate_fails_closed(crm_config, rows):
    api, _ = client(crm_config, [page(rows, total=len(rows))])
    with pytest.raises(YClientsError):
        api.find_clients_by_phone(1, PHONE)


@pytest.mark.parametrize('responses', [
    [page([row(1), row(2)], total=3), page([], total=3)],
    [page([row(1), row(2)], total=3), page([row(3)], total=4)],
    [page([row(1), row(2)], total=3), page([row(3)])],
    [page([row(1), row(2)]), page([row(3)], total=3)],
    [page([row(1), row(2)], total=3), page([row(2)], total=3)],
    [page([row(1), row(2)], total=1)],
    [page([row(1), row(2), row(3)], total=3)],
    [page([row(1)], total=1, number=2)],
    [page([row(1)], total=1, number=True)],
    [page([row(1)], total=1, number='not-page')],
    [Response({'success': True, 'data': [row(1)], 'meta': ['invalid']})],
    [Response({'success': True, 'data': {}})],
])
def test_incomplete_inconsistent_or_malformed_pages_are_not_returned(crm_config, responses):
    api, _ = client(crm_config, responses)
    with pytest.raises(YClientsError):
        api.find_clients_by_phone(1, PHONE, page_size=2)


@pytest.mark.parametrize('total', [-1, 'NaN', 1.5, True, '-1', {}, []])
def test_malformed_totals_fail_closed(crm_config, total):
    api, _ = client(crm_config, [page([], total=total)])
    with pytest.raises(YClientsError):
        api.find_clients_by_phone(1, PHONE)


@pytest.mark.parametrize('branch', [True, 0, -1, '1/2', '01', None, '١'])
def test_bad_branch_is_rejected_before_transport(crm_config, branch):
    api, session = client(crm_config, [])
    with pytest.raises(YClientsError):
        api.find_clients_by_phone(branch, PHONE)
    assert not session.calls


@pytest.mark.parametrize('size', [True, 0, -1, 201, '100'])
def test_bad_page_size_is_rejected_before_transport(crm_config, size):
    api, session = client(crm_config, [])
    with pytest.raises(YClientsError):
        api.find_clients_by_phone(1, PHONE, page_size=size)
    assert not session.calls


@pytest.mark.parametrize('path', ['clients/2', 'clients/1/', 'clients/1/bulk',
                                'company/1/clients/search', '../clients/1', 'clients/1?phone=123'])
def test_new_allowlist_remains_exact_and_branch_scoped(crm_config, path):
    api, session = client(crm_config, [])
    with pytest.raises(YClientsReadOnlyError):
        api.request('GET', path, 1)
    assert not session.calls


def test_post_cannot_use_the_shared_collection_path(crm_config):
    api, session = client(crm_config, [])
    with pytest.raises(YClientsReadOnlyError):
        api.request('POST', 'clients/1', 1)
    assert not session.calls


def test_inactive_or_standalone_connection_cannot_search(crm_config, monkeypatch):
    api, session = client(crm_config, [])
    monkeypatch.setattr(config, 'YCLIENTS_ENABLED', False)
    with pytest.raises(YClientsError):
        api.find_clients_by_phone(1, PHONE)
    monkeypatch.setattr(config, 'YCLIENTS_ENABLED', True)
    with crm_config.store.transaction() as branches:
        branches['1']['yclients_marketplace_state'] = 'frozen'
    with pytest.raises(YClientsError):
        api.find_clients_by_phone(1, PHONE)
    with crm_config.store.transaction() as branches:
        branches['1']['yclients_enabled'] = False
    with pytest.raises(YClientsReadOnlyError):
        api.find_clients_by_phone(1, PHONE)
    assert not session.calls
