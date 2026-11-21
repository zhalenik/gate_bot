"""External integration entry points, isolated from providers and physical hardware."""
from registration_samples import agreements
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from branch_join import BranchJoin
from qr_delivery import settings as qr_settings
from service_api import send_yclients_code
from storage import config
from storage.sql_documents import DocumentStore, migrate_legacy
from test_gate_qr import qr
from test_identity_product import identities, PHONE
from test_web_security import csrf, login, post, web
from test_ui_browser import browser_ui, sign_in


@pytest.fixture
def api(web, tmp_path, monkeypatch):
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path/'admins.json'))
    client = web.app.test_client()
    token = login(client, 'super')
    def issue(scopes, limit=30):
        response = post(client, token, '/admin/api/service-tokens', {'name':'Integration', 'scopes':scopes, 'rate_limit':limit})
        assert response.status_code == 201
        return response.json['id'], {'Authorization':'Bearer ' + response.json['token']}
    return SimpleNamespace(client=client, csrf=token, issue=issue, web=web, identity=web.module.product_provider()['service'])


def test_tokens_scope_count_limits_revocation_and_named_feedback(api):
    client = api.client
    post(client, api.csrf, '/api/client/feedback/send', {'token':api.web.token_a,'stars':4,'message':'First'})
    api.web.links.update_user(api.web.token_a, {'user_name':'Updated'})
    identifier, header = api.issue(['reviews'], 2)
    raw = header['Authorization'].split()[1]
    assert raw not in client.get('/admin/service-api').text
    assert '/codes/sms' in client.get('/admin/service-api/guide').text
    assert client.post('/api/v1/codes/sms', json={'phone':PHONE}).status_code == 401
    assert client.post('/api/v1/codes/sms', headers=header, json={'phone':PHONE}).status_code == 403
    reviews = client.get('/api/v1/reviews', headers=header)
    assert reviews.status_code == 200 and reviews.json[0]['author'] == 'Client A' and reviews.json[0]['branch_id'] == '12'
    assert 'person_id' not in reviews.json[0] and 'Client A' in client.get('/admin/feedback').text
    limited = client.get('/api/v1/reviews', headers=header)
    assert limited.status_code == 429 and int(limited.headers['Retry-After']) > 0
    with api.identity.product.connect() as db:
        row = db.execute('SELECT requests,digest FROM service_api_tokens WHERE id=?',(identifier,)).fetchone()
        assert row['requests'] == 3 and raw not in row['digest']
    url = '/admin/api/service-tokens/' + str(identifier)
    assert client.delete(url).status_code == 403
    assert client.delete(url, headers={'X-CSRF-Token':api.csrf}).status_code == 204
    assert client.get('/api/v1/reviews', headers=header).status_code == 401
    tenant = login(client)
    assert client.get('/admin/service-api').status_code == 403
    assert client.get('/admin/service-api/guide').status_code == 403
    assert post(client, tenant, '/admin/api/service-tokens', {}).status_code == 403


def test_code_delivery_to_queue_preserves_leading_zeroes_and_charges_only_branch(api, monkeypatch):
    import sim800c
    import service_api
    monkeypatch.setattr(service_api.secrets, 'randbelow', lambda n: 7)
    monkeypatch.setattr(config, 'OUTBOUND_ENABLED', True)
    out = api.web.module.product_provider()['notifications']
    queue = SimpleNamespace(dry_run=False,journal=out.telephony,add_manual=Mock(return_value={'status':'success','delivery':'queued'}))
    monkeypatch.setattr(sim800c, '_get_queue', lambda: queue)
    svc = api.web.module.product_provider()['billing']
    svc.grant_plan_gift('12',actor='super',quota=1,sms_quota=3,days=30,mode='replace',confirm_burn=True)
    _, header = api.issue(['system_sms','branch_sms','branch_chatpush'])
    def send(path, phone):
        return api.client.post('/api/v1/'+path, headers=header, json={'phone':phone})
    assert send('codes/sms', PHONE).json == '000007'
    assert send('branches/12/codes/sms', PHONE).status_code == 429
    assert send('branches/12/codes/chatpush', '+15550100102').status_code == 409
    assert send('branches/12345/codes/sms', '+15550100102').status_code == 404
    assert send('branches/12/codes/sms', '+15550100102').json == '000007'
    assert len(svc.sms.details(['12'])['items']) == 1 and queue.add_manual.call_count == 2
    assert len([r for r in out.telephony.snapshot()['records'].values() if '000007' in r['text']]) == 2
    with api.identity.product.connect() as db:
        assert db.execute('SELECT count(*) FROM identity_otp').fetchone()[0] == 0
    queue.dry_run = True
    rejected = send('codes/sms','+15550100103')
    assert rejected.status_code == 503 and rejected.json == {'error':'delivery_unavailable'}


def test_both_qr_flows_use_branch_choice_and_sms_billing_context(qr):
    base = qr.base
    base.service.save_integration('north', '11', 'chatpush', {'token':'synthetic-qr-token'})
    with base.product.connect() as db:
        assert qr_settings(base.service,db,11,'chatpush')['channel'] == 'chatpush'
    gate = qr.svc.request_code(qr.raw, PHONE, 'first', '127.0.0.1')
    assert gate['channel'] == 'chatpush' and base.sent[-1][0] == 'chatpush'
    assert qr.svc.challenge_status(qr.raw, 'first')['channel'] == 'chatpush'
    join = BranchJoin(base.service,base.links,None)
    raw = join.admin_view('north',11,'create')['url'].rsplit('/',1)[-1]
    for channel, phone in [('chatpush','+15550100102'),('sms','+15550100103')]:
        with base.product.connect() as db:
            qr_settings(base.service,db,11,channel)
        result = join.code(raw,'second',phone,'127.0.0.1', documents=agreements(join.identity, client=True))
        actual,target,_,context = base.sent[-1]
        assert result['channel'] == actual == channel and target == phone
        assert context['branch_id'] == '11' and 'billing_exempt' not in context
    new_branch = base.service.register_branch(None,'Branch','Owner','+15550100104',yclients_enabled=False)
    base.service.request_otp(new_branch['registration_token'],registration=True, documents=agreements(base.service))
    assert base.sent[-1][3]['billing_exempt'] == 'registration'
    invite = base.service.invite('north','11',phone='+15550100105')
    base.service.request_otp(invite['link'].rsplit('/',1)[-1], documents=agreements(base.service))
    assert base.sent[-1][3]['branch_id'] == '11' and 'billing_exempt' not in base.sent[-1][3]


def test_crm_otp_reads_exact_branch_before_sending_by_unique_id(identities, monkeypatch):
    from yclients_api import YClientsClient
    from yclients_delivery import YClientsDelivery
    service = identities.service
    monkeypatch.setattr(config, 'YCLIENTS_ENABLED', True)
    service.save_integration('north',11,'yclients',{'user_token':'synthetic-user','partner_token':'synthetic-partner'})
    search = Mock(return_value=[])
    send = Mock(return_value={'status':'accepted'})
    monkeypatch.setattr(YClientsClient,'find_clients_by_phone',search)
    monkeypatch.setattr(YClientsDelivery,'send',send)
    def run():
        return send_yclients_code(service, identities.branches, '11', PHONE, 'test code')
    assert run()['code'] == 'client_not_found'
    search.return_value = [{'id':1,'phone':PHONE},{'id':2,'phone':PHONE}]
    assert run()['code'] == 'client_ambiguous'
    send.assert_not_called()
    search.return_value = [{'id':9,'phone':PHONE}]
    assert run()['status'] == 'accepted'
    search.assert_called_with('11',PHONE)
    assert send.call_args.args[:3] == ('11',9,'test code')


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS') != '1', reason='Opt-in browser acceptance')
def test_owner_and_branch_settings_in_browser(web, browser_ui, tmp_path, monkeypatch):
    migrate_legacy(tmp_path, activate=True)
    monkeypatch.setattr(web.auth, 'store', DocumentStore(tmp_path/'admins.json'))
    page, origin, expect, errors = browser_ui
    sign_in(page, origin)
    page.goto(origin+'/admin/service-api')
    page.get_by_label('Название интеграции').fill('Offline integration')
    page.get_by_label('Чтение отзывов').check()
    page.get_by_role('button', name='Создать токен').click()
    expect(page.locator('#apiTokenValue')).to_be_visible()
    assert page.locator('#apiTokenValue').input_value().startswith('gbt_')
    page.get_by_role('button', name='Готово').click()
    page.once('dialog', lambda dialog: dialog.accept())
    page.get_by_role('button', name='Отозвать').click()
    expect(page.locator('#apiTokens tr')).to_have_count(0)
    page.goto(origin+'/admin/integrations')
    panel = page.locator('#qrDeliverySettings')
    expect(panel).to_be_hidden()
    service = web.module.product_services().identity
    service.save_integration('super',12,'chatpush',{'token':'synthetic-token'})
    page.reload()
    page.locator('#integrationBranch').select_option('12')
    expect(panel).to_be_visible()
    panel.locator('select').select_option('chatpush')
    panel.get_by_role('button',name='Сохранить').click()
    expect(panel.locator('[role=status]')).to_contain_text('сохранён')
    with service.product.connect() as db:
        assert qr_settings(service,db,12)['channel'] == 'chatpush'
    page.locator('#integrationBranch').select_option('123')
    expect(panel).to_be_hidden()
