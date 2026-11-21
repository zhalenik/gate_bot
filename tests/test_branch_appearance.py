"""The appearance editor and public assets use the branch's active entitlement."""
import io
import json
import os
import pytest

from branch_appearance import DEFAULTS
from test_branch_hours_web import hours_web
from test_web_security import web, login
from test_ui_browser import browser_ui, sign_in


@pytest.fixture
def appearance(hours_web):
    services=hours_web.module.product_services()
    services.billing.activate_trial('12')
    plan=services.billing.catalog.get('plan','trial')
    services.billing.catalog.save('plan',{**plan,'personalization':True},actor='super')
    return hours_web,services


def test_appearance_for_each_entry_scope_assets_and_revocation(appearance):
    web,services=appearance
    c=web.app.test_client();csrf=login(c)
    endpoint='/admin/api/branches/12/appearance'
    assert c.get('/admin/api/branches/123/appearance').status_code==403
    assert c.get('/admin/branches/123/appearance-preview').status_code==403
    view=c.get(endpoint).get_json()
    assert view['revision']==0 and view['palette']
    svg=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"><circle cx="4" cy="4" r="3" fill="red"/></svg>'
    surface=next(entry['id'] for entry in view['palette'] if entry['id'].startswith('theme-surface-'))
    settings={**DEFAULTS,'banner':'Welcome, <visitors>','hide_logo':True,'radius':12,
              'colors':{surface:'#994455'}}
    def save(revision,uploads):
        data={'appearance':json.dumps({'settings':settings,'revision':revision,'remove':[]})}
        for kind,img in uploads.items():data[kind]=(io.BytesIO(img),kind+'.svg')
        return c.post(endpoint,data=data,headers={'X-CSRF-Token':csrf},content_type='multipart/form-data')
    assert save(0,{'favicon':b'invalid'}).status_code==400
    saved=save(0,dict.fromkeys(('favicon','pwa','background','barrier'),svg))
    assert saved.status_code==200,saved.text
    assert save(0,{}).status_code==400
    assets=saved.json['assets']
    client=web.app.test_client()
    assert client.get(assets['pwa192']).mimetype=='image/png'
    assert client.get(assets['favicon']).mimetype=='image/svg+xml'
    manifest=client.get('/pwa/'+web.token_a+'/manifest.webmanifest').json
    assert manifest['icons'][0]['src']==assets['pwa192'] and manifest['name']=='Branch 12'
    assert manifest['theme_color']=='#994455'
    qr=web.module.product_provider()['gate_qr']
    gate=next(b for b in web.branches.get_branch(12).buttons if b.type=='gate')
    gate_url=qr.admin_view('tenant-a',12,gate.stable_id,'create')['url']
    join_url=services.branch_join.admin_view('tenant-a',12,'create')['url']
    for path in ['/opn/'+web.token_a, '/preferences/'+web.token_a, '/pwa/'+web.token_a+'/gate',
                 '/unsubscribe?token='+web.token_a,gate_url,join_url]:
        reply=client.get(path)
        assert reply.status_code==200 and 'data-hide-brand' in reply.text and assets['favicon'] in reply.text
    for path in ['/admin/users','/admin/settings','/opn/'+web.token_b]:
        assert assets['favicon'] not in c.get(path).text
    trial=services.billing.catalog.get('plan','trial')
    services.billing.catalog.save('plan',{**trial,'personalization':False},actor='super')
    assert c.get(endpoint).status_code==403 and assets['favicon'] not in client.get('/opn/'+web.token_a).text
    updated=services.billing.catalog.get('plan','trial')
    services.billing.catalog.save('plan',{**updated,'personalization':True},actor='super')
    assert c.get(endpoint).json['settings']['banner']==settings['banner']
    services.billing.cancel_subscription(12,actor='super',immediate=True,confirm_burn=True)
    assert c.get(endpoint).status_code==403 and client.get(assets['favicon']).status_code==404
    assert 'data-hide-brand' not in client.get('/opn/'+web.token_a).text
    assert save(1,{}).status_code==403


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1', reason='Opt-in Chromium')
def test_live_preview_and_saved_page_in_chrome(appearance,browser_ui):
    web,_=appearance;page,origin,expect,errors=browser_ui
    sign_in(page,origin,'tenant-a');page.goto(origin+'/admin/settings')
    page.get_by_role('tab',name='Персонализация').click()
    page.get_by_text('Баннер над кнопками',exact=True).click()
    page.get_by_label('Текст баннера',exact=True).fill('Гостевая парковка справа')
    page.get_by_label('Скрыть логотип GateBot').check()
    page.get_by_label('Скругление кнопок, px').fill('8')
    preview=page.frame_locator('#appearancePreview')
    expect(preview.locator('[data-branch-banner]')).to_have_text('Гостевая парковка справа')
    expect(preview.locator('.app-bar')).to_be_hidden()
    page.get_by_role('button',name='Сохранить оформление').click()
    expect(page.locator('#appearanceStatus')).to_have_text('Оформление сохранено')
    page.goto(origin+'/opn/'+web.token_a)
    expect(page.locator('[data-branch-banner]')).to_be_visible()
    assert page.locator('.gate-action').first.evaluate("b=>getComputedStyle(b).borderTopLeftRadius")=='8px'
    assert not web.calls and not errors
