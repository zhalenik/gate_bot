"""Unknown client links stay in the client-facing layout."""
import pytest

from test_web_security import web


@pytest.mark.parametrize('path,status', [('/opn/unknown-example-token', 403), ('/opn/invalid!token', 403), ('/unsubscribe/unknown-example-token', 404)])
def test_unknown_client_link_has_no_admin_or_home_navigation(web, path, status):
    response = web.app.test_client().get(path)
    assert response.status_code == status
    assert '<a class="brand"' not in response.text
    assert 'header-signin' not in response.text
    assert 'href="/admin/login"' not in response.text
    assert 'href="/"' not in response.text
