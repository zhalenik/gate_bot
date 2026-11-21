"""The appearance editor and public assets use the branch's active entitlement."""
import io
import hashlib
import json
import os
import pytest
from PIL import Image

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
    page.get_by_label('Расстояние между кнопками, px').fill('28')
    page.get_by_label('Скругление блока, px').fill('0')
    page.get_by_text('Размеры текста',exact=True).click()
    page.get_by_label('Название филиала, px',exact=True).fill('29')
    page.get_by_label('Текст кнопок, px').fill('18')
    page.get_by_text('Текст внизу страницы пропуска',exact=True).click()
    page.get_by_label('Заголовок блока мессенджеров').fill('Мой пропуск')
    page.get_by_label('Подпись внизу страницы',exact=True).fill('')
    page.get_by_label('Заголовок блока установки приложения').fill('Установите приложение парковки')
    page.get_by_label('Иконка шлагбаума на кнопках ворот').uncheck()
    page.get_by_text('Название и логотип филиала',exact=True).click()
    page.get_by_label('Размер логотипа, px').fill('63')
    page.get_by_label('Расположение логотипа').select_option('below')
    page.get_by_label('Выравнивание логотипа').select_option('right')
    preview=page.frame_locator('#appearancePreview')
    expect(preview.locator('[data-branch-banner]')).to_have_text('Гостевая парковка справа')
    expect(preview.locator('.app-bar')).to_be_hidden()
    expect(preview.locator('.button-gate svg').first).to_be_hidden()
    assert preview.locator('.gate-actions').evaluate("e=>getComputedStyle(e).gap")=='28px'
    assert preview.locator('.branch-logo').evaluate("e=>getComputedStyle(e).alignSelf")=='flex-end'
    assert preview.locator('.pass-brand').evaluate("e=>getComputedStyle(e).flexDirection")=='column-reverse'
    assert preview.locator('.branch-logo').evaluate("e=>getComputedStyle(e).width")=='63px'
    assert preview.locator('.desk-tools').evaluate("e=>getComputedStyle(e).gap")=='28px'
    expect(preview.locator('#clientInstallCard h2')).to_have_text('Установите приложение парковки')
    page.get_by_label('Скрыть логотип филиала').check()
    page.get_by_role('button',name='Сохранить оформление').click()
    expect(page.locator('#appearanceStatus')).to_have_text('Оформление сохранено')
    page.goto(origin+'/opn/'+web.token_a)
    expect(page.locator('[data-branch-banner]')).to_be_visible()
    assert page.locator('.gate-action').first.evaluate("b=>getComputedStyle(b).borderTopLeftRadius")=='8px'
    assert page.locator('.pass-card').evaluate("e=>getComputedStyle(e).borderTopLeftRadius")=='0px'
    assert page.locator('.pass-brand h1').evaluate("e=>getComputedStyle(e).fontSize")=='29px'
    assert page.locator('.gate-action').first.evaluate("e=>getComputedStyle(e).fontSize")=='18px'
    expect(page.locator('.branch-logo')).to_be_hidden()
    expect(page.locator('.pass-tools h2')).to_have_text('Мой пропуск')
    expect(page.locator('.access-footer')).to_be_hidden()
    expect(page.locator('.gate-actions .gate-button-icon').first).to_be_hidden()
    assert not web.calls and not errors


@pytest.mark.parametrize('endpoint,actor',[('/admin/api/palette/client-appearance','super'),('/admin/api/branches/12/appearance','tenant-a')])
def test_edit_keeps_new_inherited_icon_and_rejects_only_stale_crop(appearance,endpoint,actor):
    from branding import BrandingSettings
    web,services=appearance
    branding=BrandingSettings(services.identity)
    client=web.app.test_client()
    csrf=login(client,actor)
    previous=client.get(endpoint).json
    def save(view,settings,edit):
        payload={'revision':view['revision'],'base_revision':view['base_revision'],'settings':settings,
                 'remove':[],'icon_edit':edit,'branding_icon_revision':view['branding_icon_revision']}
        return client.post(endpoint,headers={'X-CSRF-Token':csrf},content_type='multipart/form-data',
                           data={'appearance':json.dumps(payload)})
    new_frame={**previous['settings']['icon_frame'],'zoom':145,'x':33}
    branding.save('super','icon',None,branding.read('super')['revision'],frame=new_frame)
    draft={**previous['settings'],'icon_frame':{**previous['settings']['icon_frame'],'zoom':185}}
    stale=save(previous,draft,'frame')
    assert stale.status_code==400 and 'Исходная иконка изменилась' in stale.json['message']
    saved=save(previous,{**previous['settings'],'font':'rubik'},'keep')
    assert saved.status_code==200 and saved.json['settings']['icon_frame']==new_frame
    assert 'pwa_source' not in saved.json['own_assets']
    fresh=saved.json
    updated=save(fresh,{**fresh['settings'],'icon_frame':draft['icon_frame']},'frame')
    assert updated.status_code==200 and 'pwa_source' in updated.json['own_assets']
    own=updated.json
    branding.save('super','icon',None,branding.read('super')['revision'])
    updated=save(own,{**own['settings'],'icon_frame':new_frame},'frame')
    assert updated.status_code==200


def test_default_theme_inheritance_and_reframing(appearance):
    web,services=appearance
    client=web.app.test_client()
    owner=web.app.test_client()
    csrf=login(owner,'super')
    tenant=web.app.test_client()
    branch_csrf=login(tenant)
    global_api='/admin/api/palette/client-appearance'
    branch_api='/admin/api/branches/12/appearance'
    assert tenant.get(global_api).status_code==403
    assert tenant.get('/admin/palette/client-appearance-preview').status_code==403
    def save(session,token,endpoint,current,updates,file=None):
        values={**current['settings'],**updates}
        body={'appearance':json.dumps({'settings':values,'revision':current['revision'],'base_revision':current['base_revision'],'remove':[]})}
        if file:
            body[file[0]]=(io.BytesIO(file[1]),'sample.png')
        return session.post(endpoint,headers={'X-CSRF-Token':token},data=body,content_type='multipart/form-data')
    image=io.BytesIO()
    Image.new('RGB',(72,36),'red').save(image,'PNG')
    initial=owner.get(global_api).json
    global_view=save(owner,csrf,global_api,initial,{'button_gap':16,'title_align':'center','banner':'Общий въезд'},('card_background',image.getvalue())).json
    assert global_view['settings']['button_gap']==16
    assert 'Общий въезд' in client.get('/opn/'+web.token_b).text
    assert global_view['assets']['card_background'] in client.get('/opn/'+web.token_b).text
    assert global_view['assets']['card_background'] not in owner.get('/admin/users').text
    local=tenant.get(branch_api).json
    saved=save(tenant,branch_csrf,branch_api,local,{'button_align':'right'},('pwa',image.getvalue())).json
    def pixel(view):
        src=view['assets']['pwa192']
        return Image.open(io.BytesIO(client.get(src).data)).convert('RGB').getpixel((0,0))
    assert pixel(saved)==(255,0,0)
    frame={'fit':'contain','padding':15,'background':'#0000FF'}
    reframed=save(tenant,branch_csrf,branch_api,saved,{'icon_frame':frame}).json
    assert pixel(reframed)==(0,0,255)
    assert reframed['assets']['pwa192']!=saved['assets']['pwa192']
    assert save(owner,csrf,global_api,global_view,{'banner':'Новый баннер','title_align':'right'}).status_code==200
    assert save(tenant,branch_csrf,branch_api,reframed,{}).status_code==400
    effective=tenant.get(branch_api).json
    assert effective['settings']['title_align']=='right' and effective['settings']['button_align']=='right'
    assert 'Новый баннер' in client.get('/opn/'+web.token_a).text
    assert client.get('/pwa/'+web.token_a+'/manifest.webmanifest').json['icons'][0]['src']==reframed['assets']['pwa192']
    assert tenant.get(branch_api).json['revision']==reframed['revision']


def test_scoped_typography_and_footer_preserve_legacy_theme(appearance):
    web,services=appearance
    appearance_service=services.appearance
    with services.store.connect() as db:
        db.execute('INSERT INTO branch_appearance VALUES(?,?,?)',('default',1,json.dumps({'font_size':19})))
    defaults=appearance_service.editor('super','default')
    appearance_service.save('super','default',1,{**defaults['settings'],'footer_text':'Общая подпись','button_size':17},{},[])
    branch=appearance_service.editor('tenant-a',12)
    draft={**branch['settings'],'tools_title':'<Гостевой въезд>','footer_text':'','branch_size':31,'logo_position':'after'}
    with pytest.raises(ValueError):
        appearance_service.save('tenant-a',12,0,{**draft,'logo_size':999},{},[])
    appearance_service.save('tenant-a',12,0,draft,{},[])
    client=web.app.test_client()
    for path in ['/opn/'+web.token_a, '/pwa/'+web.token_a+'/gate']:
        html=client.get(path).text
        assert 'data-logo-position="after"' in html and '--client-unit:19px;' in html
        assert '--client-button-size:17px;' in html and '--client-branch-size:31px;' in html
        assert '&lt;Гостевой въезд&gt;' in html and 'Общая подпись' not in html
    assert 'Общая подпись' in client.get('/opn/'+web.token_b).text


def test_previous_padded_icon_can_be_reframed_without_upload(appearance):
    web,services=appearance
    # This is exactly the square composed by alpha.24 for an opaque square source.
    image=Image.new('RGB',(512,512),'white')
    image.paste('red',(51,51,461,461))
    data=io.BytesIO();image.save(data,'PNG');png=data.getvalue()
    with services.store.connect() as db:
        db.execute('INSERT INTO branch_appearance VALUES("12",1,"{}")')
        db.execute('INSERT INTO branch_appearance_assets VALUES("12","pwa512",?,?)',(hashlib.sha256(png).hexdigest()+'.png',png))
    service=services.appearance
    old=service.editor('tenant-a',12)
    assert old['settings']['icon_frame']['padding']==10
    assert web.app.test_client().get(old['assets']['pwa_source']).status_code==200
    new=service.save('tenant-a',12,1,{**old['settings'],'icon_frame':DEFAULTS['icon_frame']},{},[])
    response=web.app.test_client().get(new['assets']['pwa192'])
    assert Image.open(io.BytesIO(response.data)).getpixel((0,0))==(255,0,0)


@pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1', reason='Opt-in Chromium')
def test_default_editor_preview(appearance,browser_ui):
    web,_=appearance;page,origin,expect,errors=browser_ui
    sign_in(page,origin,'super');page.goto(origin+'/admin/palette')
    form=page.locator('#appearanceForm')
    page.get_by_text('Клиентские страницы по умолчанию',exact=True).click()
    form.locator('select[name=font]').select_option('rubik')
    form.get_by_label('Расстояние между кнопками, px').fill('22')
    form.get_by_label('Выравнивание текста кнопки',exact=True).select_option('center')
    form.get_by_text('Изображения',exact=True).click()
    branding_icon=page.locator('[data-branding-kind=icon]')
    branding_icon.locator('[name=icon_padding]').fill('7')
    branding_icon.get_by_role('button',name='Сохранить иконку').click()
    expect(form.locator('[name=icon_padding]')).to_have_value('7')
    expect(form.locator('[name=font]')).to_have_value('rubik')
    img=io.BytesIO();Image.new('RGB',(16,32),'blue').save(img,'PNG')
    form.locator('input[name=pwa]').set_input_files({'name':'square.png','mimeType':'image/png','buffer':img.getvalue()})
    form.get_by_label('Поля с каждой стороны, %').fill('0')
    expect(form.get_by_alt_text('Предпросмотр иконки приложения')).to_be_visible()
    pre=page.frame_locator('#appearancePreview')
    assert pre.locator('.gate-actions').evaluate("e=>getComputedStyle(e).gap")=='22px'
    assert pre.locator('.gate-actions .button-gate>span').first.evaluate("e=>getComputedStyle(e).textAlign")=='center'
    form.get_by_role('button',name='Сохранить оформление').click()
    expect(form.locator('#appearanceStatus')).to_have_text('Оформление сохранено')
    page.goto(origin+'/opn/'+web.token_b)
    assert page.locator('.gate-actions').evaluate("e=>getComputedStyle(e).gap")=='22px'
    assert page.locator('.button-gate').first.evaluate("""async e=>{
        const loaded=await document.fonts.load('600 16px Rubik','Ворота');
        return loaded.length>0 && getComputedStyle(e).fontFamily.includes('Rubik');
    }""")
    assert not web.calls and not errors
