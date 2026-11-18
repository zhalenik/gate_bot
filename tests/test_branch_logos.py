"""Offline uploads, tenant permissions and public image sanitization."""
import io
import re
from pathlib import Path

from PIL import Image, PngImagePlugin
import pytest

from branch_logos import MAX_UPLOAD_BYTES
from test_web_security import web, login


def image_bytes(size=(40, 30)):
    image = Image.new('RGB', size, '#007aff')
    output = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text('Comment', 'private-upload-metadata')
    image.save(output, format='PNG', pnginfo=metadata)
    return output.getvalue()


def upload(client, csrf, branch, data, filename='logo.png'):
    return client.post('/admin/api/branches/'+str(branch)+'/logo',
        data={'image': (io.BytesIO(data), filename)}, headers={'X-CSRF-Token':csrf})


def test_logo_upload_sanitizes_pixels_scopes_mutation_and_deletes_old_file(web):
    client=web.app.test_client();csrf=login(client)
    assert upload(client,csrf,123,image_bytes()).status_code==403
    assert upload(client,'invalid-csrf',12,image_bytes()).status_code==403
    response=upload(client,csrf,12,image_bytes()+b'<script>test trailing payload</script>','../../data.json')
    assert response.status_code==200
    logo=response.json['logo']
    assert re.fullmatch(r'/media/branch-logos/[a-f0-9]{32}\.png',logo)
    assert web.branches.get_branch(12).logo==logo and web.branches.get_branch(123).logo==''
    public=web.app.test_client().get(logo)
    assert public.status_code==200 and public.mimetype=='image/png'
    assert public.headers['X-Content-Type-Options']=='nosniff'
    assert b'private-upload-metadata' not in public.data and b'<script>' not in public.data
    with Image.open(io.BytesIO(public.data)) as pixels:
        assert pixels.size==(40,30) and pixels.getpixel((0,0))==(0,122,255,255)
    public.close()  # Windows keeps a send_file handle until the response closes.
    assert web.app.test_client().get('/media/branch-logos/session.key').status_code==404
    replacement=upload(client,csrf,12,image_bytes((50,50)))
    assert replacement.status_code==200 and replacement.json['logo']!=logo
    assert client.get(logo).status_code==404
    assert client.delete('/admin/api/branches/123/logo',headers={'X-CSRF-Token':csrf}).status_code==403
    assert client.delete('/admin/api/branches/12/logo',headers={'X-CSRF-Token':csrf}).status_code==200
    assert web.branches.get_branch(12).logo==''
    assert client.get(replacement.json['logo']).status_code==404
    assert list((Path(web.branches.branches_file).parent/'branch-logos').iterdir())==[]


@pytest.mark.parametrize('bad_image',[b'<svg xmlns="http://www.w3.org/2000/svg"><script>test</script></svg>',
                                    b'not an image',image_bytes((4097,1))])
def test_invalid_images_never_change_branch_or_create_files(web,bad_image):
    client=web.app.test_client();csrf=login(client)
    response=upload(client,csrf,12,bad_image)
    assert response.status_code==400
    assert web.branches.get_branch(12).logo==''
    assert not (Path(web.branches.branches_file).parent/'branch-logos').exists()


def test_oversized_upload_is_bounded_before_image_decode(web):
    client=web.app.test_client();csrf=login(client)
    response=upload(client,csrf,12,b'x'*(MAX_UPLOAD_BYTES+1))
    assert response.status_code in {400,413}
    assert web.branches.get_branch(12).logo==''


def test_navigation_follows_role_and_keeps_client_pass_actions(web):
    client=web.app.test_client();login(client)
    page=client.get('/admin/users').text
    nav=re.search(r'<nav class="app-navigation".*?</nav>',page,re.S).group()
    assert '/admin/temp-link' not in nav and '/admin/telephony' not in nav
    assert '/admin/subscription' in nav
    assert 'href="/admin/logs"' not in page and 'href="/admin/smtp"' not in page
    assert 'href="/admin/system-settings"' not in page
    login(client,'super');page=client.get('/admin/users').text
    nav=re.search(r'<nav class="app-navigation".*?</nav>',page,re.S).group()
    assert '/admin/temp-link' not in nav and '/admin/subscription' not in nav
    assert '/admin/telephony' in nav
    assert 'href="/admin/branch-plans"' in page and 'href="/admin/logs"' in page and 'href="/admin/system-settings"' in page
    assert 'href="/admin/smtp"' not in page  # SMTP is now embedded in system settings.
    assert client.get('/admin/temp-link').status_code==200
