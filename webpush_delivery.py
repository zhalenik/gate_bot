"""Bounded Web Push transport. Subscription endpoints are never diagnostics."""
import ipaddress
import json
import re
import socket
from urllib.parse import urlsplit

import requests


PUSH_HOSTS = frozenset({'fcm.googleapis.com', 'updates.push.services.mozilla.com', 'web.push.apple.com'})


def validate_endpoint(endpoint):
    if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 4096 or any(ord(c) <= 32 or ord(c) == 127 for c in endpoint) or '\\' in endpoint:
        raise ValueError('Некорректная подписка push-уведомлений')
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ''
        valid_host = host in PUSH_HOSTS or bool(re.fullmatch(r'[a-z0-9-]+\.notify\.windows\.com', host))
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment or not parsed.path or not valid_host:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError('Служба push-уведомлений этого браузера не поддерживается') from None
    return endpoint


class PushSession(requests.Session):
    def __init__(self):
        super().__init__()
        self.trust_env = False

    def request(self, method, url, **kwargs):
        validate_endpoint(url)
        if method.upper() != 'POST':
            raise ValueError('Unsupported push request')
        addresses = socket.getaddrinfo(urlsplit(url).hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
            raise ValueError('Unsupported push address')
        kwargs.update(allow_redirects=False, timeout=10)
        return super().request(method, url, **kwargs)


def send_webpush(subscription, payload, private_key, subject):
    """Return safe delivery states; no exception text or response body escapes."""
    from pywebpush import WebPushException, webpush
    validate_endpoint(subscription.get('endpoint'))
    try:
        with PushSession() as session:
            response = webpush(subscription_info=subscription, data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=private_key, vapid_claims={'sub':subject}, ttl=300, timeout=10,
                requests_session=session, content_encoding='aes128gcm', verbose=False)
        if response.status_code in (200, 201, 202):
            return {'status':'accepted', 'confirmed':False}
        return {'status':'failed', 'code':'push_rejected', 'confirmed':False}
    except WebPushException as error:
        code = error.status_code
        if code in (404, 410):
            return {'status':'expired', 'code':'push_subscription_expired', 'confirmed':False}
        return {'status':'failed', 'code':'push_rejected', 'confirmed':False}
    except (requests.RequestException, OSError):
        return {'status':'unknown', 'code':'push_result_unknown', 'confirmed':False}
    except (ValueError, TypeError):
        return {'status':'failed', 'code':'push_invalid', 'confirmed':False}
