"""Focused desktop/mobile landing and branding upload acceptance."""
import os

import pytest

from test_branding import SVG,jpeg
from test_palette_settings import palette_web,web
from test_ui_browser import browser_ui,sign_in

pytestmark=pytest.mark.skipif(os.environ.get('GATEBOT_BROWSER_TESTS')!='1',reason='Opt-in browser acceptance')


def test_landing_client_header_and_branding_uploads(palette_web,browser_ui,tmp_path):
    web=palette_web;page,origin,expect,errors=browser_ui
    page.goto(origin+'/')
    expect(page.locator('.header-signin')).to_have_count(0)
    expect(page.get_by_role('link',name='Зарегистрировать филиал')).to_be_visible()
    expect(page.get_by_role('link',name='Войти',exact=True)).to_be_visible()
    assert page.locator('.brand').evaluate('e=>Math.abs(e.getBoundingClientRect().x+e.getBoundingClientRect().width/2-innerWidth/2)<2')
    assert page.locator('main svg').count()==0
    page.screenshot(path=str(tmp_path/'landing-desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(tmp_path/'landing-mobile.png'),full_page=True)
    page.goto(origin+'/opn/'+web.token_a)
    assert page.locator('.brand').evaluate('e=>Math.abs(e.getBoundingClientRect().x+e.getBoundingClientRect().width/2-innerWidth/2)<2')
    expect(page.locator('.brand a,.header-signin')).to_have_count(0)
    page.set_viewport_size({'width':1440,'height':900})
    sign_in(page,origin)
    assert page.locator('.brand').evaluate('e=>e.getBoundingClientRect().left')==page.locator('.admin-container').evaluate('e=>e.getBoundingClientRect().left+parseFloat(getComputedStyle(e).paddingLeft)')
    page.goto(origin+'/admin/palette')
    logo=page.locator('[data-branding-kind=logo]')
    expect(logo.get_by_role('button',name='Сохранить логотип')).to_be_enabled()
    logo.locator('input[type=file]').set_input_files({'name':'brand.jpg','mimeType':'image/jpeg','buffer':jpeg()})
    logo.get_by_role('button',name='Сохранить логотип').click()
    expect(page.locator('[data-brand-logo]')).to_have_attribute('src',__import__('re').compile('/ui/branding/assets/[a-f0-9]{64}.png'))
    icon=page.locator('[data-branding-kind=icon]')
    icon.locator('input[type=file]').set_input_files({'name':'icon.svg','mimeType':'image/svg+xml','buffer':SVG})
    icon.get_by_role('button',name='Сохранить иконку').click()
    expect(page.locator('#brandingIconPreview')).to_have_attribute('src',__import__('re').compile('/ui/branding/assets/[a-f0-9]{64}.svg'))
    view=page.request.get(origin+'/admin/api/branding').json()
    assert page.request.get(origin+view['apple_icon_url']).headers['content-type']=='image/png'
    page.reload();expect(page.locator('[data-brand-logo]')).to_have_attribute('src',view['logo_url'])
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path=str(tmp_path/'branding-mobile.png'),full_page=True)
    assert not web.calls
