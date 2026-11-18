"""Shared URL boundary for the CRM reader and its settings editor."""
from urllib.parse import urlsplit


def normalize_yclients_base_url(value):
    message = 'Используйте официальный HTTPS API YCLIENTS: api.yclients.ru или api.yclients.com, путь /api/v1'
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(message)
    try:
        url = urlsplit(value.strip().rstrip('/'))
        valid = (url.scheme == 'https' and url.hostname in {'api.yclients.ru', 'api.yclients.com'}
                 and url.path == '/api/v1' and not url.query and not url.fragment
                 and url.username is None and url.password is None and url.port in {None, 443})
    except ValueError:
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)
    return f'https://{url.hostname}/api/v1'
