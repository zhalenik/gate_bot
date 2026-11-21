"""Public branding consists only of canonical pixels or self-contained SVG shapes."""
import io

from PIL import Image
import pytest

from branding import BrandingSettings
from branding_images import canonical_brand
from test_identity_product import identities
from test_palette_settings import palette_web
from test_web_security import login,web


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect width="40" height="40" rx="8" fill="#F94F06"/></svg>'


@pytest.fixture(autouse=True)
def mock_branding_diagnostics(monkeypatch):
    monkeypatch.setattr('branding.logger.event',lambda *args,**kwargs:None)


def jpeg():
    output = io.BytesIO()
    Image.new('RGB',(320,80),'#F94F06').save(output,format='JPEG',comment=b'private-upload-metadata')
    return output.getvalue()


@pytest.mark.parametrize('unsafe', [
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40" onload="alert(1)"/>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><image href="file:///private/secret.png"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><div>HTML</div></foreignObject></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect style="fill:url(https://example.invalid/x)"/></svg>',
    b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///private/secret">]><svg>&x;</svg>',
    b'<?xml-stylesheet href="https://example.invalid/x"?><svg viewBox="0 0 40 40"/>',
])
def test_svg_rejects_execution_and_resource_references(unsafe):
    with pytest.raises(ValueError): canonical_brand(unsafe)


def test_svg_shapes_local_gradients_and_pwa_rasters_have_no_metadata(identities):
    settings=BrandingSettings(identities.service)
    original=settings.public_view()
    svg=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><metadata>private-metadata</metadata><defs><linearGradient id="paint"><stop offset="0" stop-color="#F94F06"/><stop offset="1" stop-color="#FFFFFF"/></linearGradient></defs><rect width="40" height="40" style="fill:url(#paint)"/></svg>'
    saved=settings.save('owner','icon',svg,original['revision'])
    assert saved['icon_custom'] and saved['icon_url'].endswith('.svg')
    content=(settings.directory/saved['icon_url'].rsplit('/',1)[1]).read_bytes()
    assert b'private-metadata' not in content and b'style=' not in content
    for key,size in [('icon192_url',192),('icon512_url',512),('apple_icon_url',180)]:
        with Image.open(settings.directory/saved[key].rsplit('/',1)[1]) as image:
            assert image.size==(size,size) and image.mode=='RGB'
    assert BrandingSettings(identities.service).public_view()==saved
    with pytest.raises(PermissionError): settings.save('north','logo',jpeg(),saved['revision'])
    with pytest.raises(ValueError): settings.save('owner','logo',jpeg(),0)
    assert settings.public_view()==saved


def test_upload_http_permissions_csrf_reset_and_public_only_canonical_assets(palette_web):
    web=palette_web;client=web.app.test_client()
    assert client.get('/admin/api/branding').status_code==401
    default=client.get('/ui/branding/default/icon-192.png')
    assert default.status_code==200 and Image.open(io.BytesIO(default.data)).size==(192,192)
    csrf=login(client,'tenant-a')
    assert client.get('/admin/api/branding').status_code==403
    assert client.post('/admin/api/branding/logo',headers={'X-CSRF-Token':csrf},data={'revision':'0','image':(io.BytesIO(jpeg()),'logo.jpg')}).status_code==403
    csrf=login(client,'super')
    assert client.post('/admin/api/branding/logo',data={'revision':'0','image':(io.BytesIO(jpeg()),'logo.jpg')}).status_code==403
    response=client.post('/admin/api/branding/logo',headers={'X-CSRF-Token':csrf},data={'revision':'0','image':(io.BytesIO(jpeg()),'logo.jpg')})
    assert response.status_code==200
    saved=response.json;public=web.app.test_client()
    asset=public.get(saved['logo_url'])
    assert asset.status_code==200 and asset.mimetype=='image/png'
    assert b'private-upload-metadata' not in asset.data
    assert asset.headers['X-Content-Type-Options']=='nosniff'
    asset.close()
    assert public.get('/ui/branding/assets/'+'a'*64+'.png').status_code==404
    assert public.get('/ui/branding/assets/../../application.sqlite3').status_code==404
    assert saved['logo_url'].encode() in public.get('/').data
    reset=client.post('/admin/api/branding/logo/reset',headers={'X-CSRF-Token':csrf},json={'revision':saved['revision']})
    assert reset.status_code==200 and reset.json['logo_url']=='/static/images/logo.svg'
    assert not web.calls


def test_icon_can_be_reframed_without_uploading_its_original_again(palette_web):
    client=palette_web.app.test_client()
    headers={'X-CSRF-Token':login(client,'super')}
    result=client.post('/admin/api/branding/icon',headers=headers,data={
        'revision':'0','image':(io.BytesIO(jpeg()),'wide.jpg')})
    assert result.status_code==200
    before=result.json
    image=Image.open(io.BytesIO(client.get(before['icon192_url']).data))
    assert image.getpixel((0,0))[0]>200 and image.getpixel((0,0))[1]<130
    result=client.post('/admin/api/branding/icon',headers=headers,data={
        'revision':str(before['revision']),'icon_fit':'contain','icon_padding':'15','icon_transparent':'on'})
    after=result.json
    assert result.status_code==200 and before['icon_url']==after['icon_url']
    assert after['icon_frame']=={'fit':'contain','padding':15,'background':'transparent'}
    image=Image.open(io.BytesIO(client.get(after['icon192_url']).data))
    assert image.getpixel((0,0))[-1]==0 and image.getpixel((96,96))[-1]==255
    assert client.get('/admin/api/branding').json['icon192_url']==after['icon192_url']
