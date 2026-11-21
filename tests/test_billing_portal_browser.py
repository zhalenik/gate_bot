"""Focused browser acceptance for the new billing account and catalog."""
import os

import pytest

from storage.sql_documents import DocumentStore, migrate_legacy
from test_ui_browser import browser_ui, sign_in
from test_web_security import web
from test_billing_portal import PDF

pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


@pytest.fixture
def portal(web,tmp_path,monkeypatch):
    migrate_legacy(tmp_path,activate=True)
    monkeypatch.setattr(web.auth,'store',DocumentStore(tmp_path/'admins.json'))
    services=web.module.product_services()
    plan=services.billing.catalog.get('plan','start')
    services.billing.catalog.save('plan',{**plan,'sms_quota':25},actor='super')
    offer=services.billing.catalog.publish_offer({'filename':'offer.pdf','url':'/admin/billing-assets/'+'a'*32+'.pdf','sha256':'a'*64},actor='super')
    order=services.billing.create_order('12','start',actor='tester',idempotency_key='synthetic-paid',mode='now',offer_version=offer['id'],accept_offer=True)
    services.billing.apply_payment(order['id'],'synthetic-payment',order['amount_kopecks'])
    return services


def test_super_catalog_edit_upload_archive_and_mobile(portal,web,browser_ui,tmp_path):
    page,origin,expect,errors=browser_ui
    sign_in(page,origin)
    page.goto(origin+'/admin/tariffs')
    expect(page.locator('#catalogCards .billing-catalog-card')).to_have_count(4)
    page.get_by_role('button',name='Создать тариф',exact=True).click()
    form=page.locator('#catalogEditorForm')
    form.locator('[name=name]').fill('Пилот 45')
    form.locator('[name=price]').fill('1250')
    form.locator('[name=duration_value]').fill('45');form.locator('[name=duration_unit]').select_option('days')
    form.locator('[name=quota]').fill('300');form.locator('[name=sms_quota]').fill('50')
    form.get_by_role('button',name='Сохранить',exact=True).click()
    card=page.locator('.billing-catalog-card').filter(has=page.get_by_role('heading',name='Пилот 45',exact=True))
    expect(card).to_contain_text('45 дн.');expect(card).to_contain_text('50 SMS')
    card.get_by_role('button',name='В архив',exact=True).click()
    expect(card).to_have_count(0)
    page.locator('#showArchived').check();expect(card).to_contain_text('В архиве')
    card.get_by_role('button',name='Восстановить',exact=True).click()
    expect(card).to_contain_text('Доступен для подключения')
    card.get_by_role('button',name='Изменить',exact=True).click()
    form.locator('[name=visible]').uncheck();form.get_by_role('button',name='Сохранить',exact=True).click()
    expect(card).to_contain_text('Скрыт от филиалов')
    page.goto(origin+'/admin/documents')
    page.locator('#offerUploadForm input[type=file]').set_input_files({'name':'new-offer.pdf','mimeType':'application/pdf','buffer':PDF})
    page.get_by_role('button',name='Опубликовать оферту',exact=True).click()
    expect(page.locator('#offerCurrent')).to_contain_text('new-offer.pdf')
    page.goto(origin+'/admin/tariffs')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(tmp_path/'billing-catalog-mobile.png'),full_page=True)
    assert not web.calls


def test_admin_plan_quote_consents_stub_and_cancel_autorenewal(portal,web,browser_ui,tmp_path):
    page,origin,expect,errors=browser_ui
    sign_in(page,origin,'tenant-a')
    page.goto(origin+'/admin/subscription')
    expect(page.locator('#billingAccount')).to_be_visible()
    expect(page.locator('#billingBranch')).to_be_hidden()
    expect(page.locator('#subscriptionSummary')).to_contain_text('100')
    expect(page.locator('#subscriptionSummary')).to_contain_text('25')
    before=portal.billing.subscription_view('12')
    page.locator('#renewalAction').click()
    renewal=page.locator('#renewalForm')
    expect(renewal.locator('[name=consent]')).not_to_be_checked()
    renewal.locator('[name=consent]').check();renewal.locator('[name=accept_offer]').check()
    renewal.get_by_role('button',name='Сохранить согласие',exact=True).click()
    expect(page.locator('#renewalAction')).to_have_text('Отключить автопродление')
    page.locator('#renewalAction').click();expect(page.locator('#renewalAction')).to_have_text('Разрешить автопродление')
    assert portal.billing.subscription_view('12')['ends_at']==before['ends_at']
    page.goto(origin+'/admin/tariffs')
    card=page.locator('.billing-catalog-card').filter(has=page.get_by_role('heading',name='Бизнес',exact=True))
    card.get_by_role('button',name='Выбрать тариф',exact=True).click()
    form=page.locator('#purchaseForm')
    expect(form.locator('[name=mode][value=period_end]')).to_be_checked()
    expect(form.locator('[name=auto_renew]')).not_to_be_checked()
    form.locator('[name=mode][value=now]').check()
    expect(page.locator('#purchaseBurn')).to_be_visible()
    form.locator('[name=accept_offer]').check();form.locator('[name=confirm_burn]').check()
    form.get_by_role('button',name='Продолжить к оплате',exact=True).click()
    expect(page.locator('#productNotice')).to_contain_text('Приём платежей')
    after=portal.billing.subscription_view('12')
    assert after['term_id']==before['term_id'] and after['remaining']==100
    assert not after['renewal']['enabled']
    page.goto(origin+'/admin/subscription');expect(page.locator('#billingAccount')).to_be_visible()
    page.get_by_role('tab',name='SMS',exact=True).click();expect(page.locator('#billingHistoryEmpty')).to_be_visible()
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(tmp_path/'billing-account-mobile.png'),full_page=True)
    # A cancelled tariff can retain a historical future end date. Reconnecting
    # must offer immediate access, not silently schedule after that old date.
    page.locator('#openCancellation').click()
    cancel=page.locator('#cancellationForm')
    cancel.locator('[name=when][value=now]').check()
    cancel.locator('[name=confirm_burn]').check()
    cancel.get_by_role('button',name='Подтвердить отключение',exact=True).click()
    expect(page.locator('#subscriptionStatus')).to_have_text('Отключён')
    expect(page.locator('#subscriptionSummary')).to_be_hidden()
    expect(page.locator('#billingHeroPrice')).to_be_hidden()
    expect(page.locator('#currentPlanLogo')).to_be_hidden()
    expect(page.locator('#selectTariff')).to_have_text('Подключить тариф')
    page.reload();expect(page.locator('#subscriptionSummary')).to_be_hidden()
    page.goto(origin+'/admin/tariffs')
    page.locator('.billing-catalog-card').filter(has=page.get_by_role('heading',name='Бизнес',exact=True)).get_by_role('button',name='Выбрать тариф',exact=True).click()
    expect(page.locator('#purchaseMode')).to_be_hidden()
    expect(page.locator('#purchaseForm [name=mode][value=now]')).to_be_checked()
    expect(page.locator('#purchaseConfirm')).to_be_enabled()
    assert not web.calls


def test_super_test_connection_after_cancellation_survives_reload(portal,web,browser_ui,tmp_path):
    page,origin,expect,errors=browser_ui
    sign_in(page,origin)
    assert page.request.post(origin+'/admin/api/subscriptions/test-activate',data={'branch_id':12,'plan_id':'start','request_id':'no-csrf','confirm_test':True,'confirm_burn':True}).status==403
    page.goto(origin+'/admin/branch-plans')
    page.locator('#billingBranch').select_option('12')
    expect(page.locator('#subscriptionSummary')).to_be_visible()
    page.locator('#openCancellation').click()
    cancel=page.locator('#cancellationForm')
    cancel.locator('[name=when][value=now]').check();cancel.locator('[name=confirm_burn]').check()
    cancel.get_by_role('button',name='Подтвердить отключение',exact=True).click()
    expect(page.locator('#subscriptionSummary')).to_be_hidden()
    page.reload();page.locator('#billingBranch').select_option('12')
    expect(page.locator('#currentPlanName')).to_have_text('Тариф отключён')
    expect(page.locator('#billingActiveDetails')).to_be_hidden()
    page.locator('#openTestActivation').click()
    form=page.locator('#testActivationForm')
    form.locator('[name=plan_id]').select_option('start')
    form.locator('[name=confirm_test]').check()
    form.get_by_role('button',name='Подключить без оплаты',exact=True).click()
    expect(page.locator('#subscriptionSummary')).to_be_visible()
    expect(page.locator('#currentPlanPrice')).to_have_text('Для тестирования')
    page.reload();page.locator('#billingBranch').select_option('12')
    expect(page.locator('#subscriptionSummary')).to_contain_text('100')
    assert portal.billing.subscription_view('12')['source']=='test'
    with portal.billing.store.connect() as db:
        assert db.execute('SELECT count(*) FROM billing_payments').fetchone()[0]==1
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(tmp_path/'billing-test-connected-mobile.png'),full_page=True)
    assert not web.calls


def test_super_gifts_package_and_sees_scoped_operations(portal,web,browser_ui):
    page,origin,expect,errors=browser_ui
    portal.billing.catalog.save('package',{'name':'Ещё 20','resource':'calls','quantity':20,'price_kopecks':10000,'logo':'','active':True},actor='super')
    sign_in(page,origin)
    page.goto(origin+'/admin/branch-plans')
    expect(page.locator('#billingBranches tr')).to_have_count(2)
    page.locator('#billingBranch').select_option('12')
    page.locator('#openGift').click();form=page.locator('#giftForm')
    form.locator('[name=kind]').select_option('package')
    form.locator('[name=reason]').fill('Synthetic package gift')
    form.get_by_role('button',name='Подарить',exact=True).click()
    expect(page.locator('#activePackages')).to_contain_text('Ещё 20')
    assert portal.billing.subscription_view('12')['remaining']==120
    page.get_by_role('tab',name='Операции с тарифом',exact=True).click()
    expect(page.locator('#billingHistory')).to_contain_text('Подключение пакета')
    page.locator('#billingBranch').select_option('123')
    expect(page.locator('#activePackages')).not_to_contain_text('Ещё 20')
    assert not web.calls
