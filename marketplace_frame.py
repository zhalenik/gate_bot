"""An isolated registration session inside the YCLIENTS marketplace."""
import secrets

from flask import request
from flask.sessions import SecureCookieSessionInterface


ENTRY = '/yclients/marketplace'
FRAME_ANCESTORS = 'frame-ancestors https://yclients.ru https://*.yclients.ru'
UNCLAIMED_BROWSER = 'awaiting-first-party-browser'


def in_marketplace_frame(req):
    if req.path == ENTRY + '/resume' or not (req.path == ENTRY or req.path.startswith(ENTRY + '/')):
        return False
    return req.args.get('frame') in {'1', 'start'} or req.headers.get('Sec-Fetch-Dest') == 'iframe'


class FrameCookie(SecureCookieSessionInterface):
    # Frame cookies never authenticate requests to the ordinary GateBot cabinet.
    salt = 'gatebot-marketplace-frame'

    def get_cookie_name(self, app):
        return '__Secure-gatebot-marketplace'

    def get_cookie_path(self, app):
        return ENTRY

    def get_cookie_samesite(self, app):
        return 'None'

    def get_cookie_secure(self, app):
        return True

    def get_cookie_partitioned(self, app):
        return True


class MarketplaceSessionInterface(SecureCookieSessionInterface):
    frame = FrameCookie()

    def open_session(self, app, request):
        interface = self.frame if in_marketplace_frame(request) else super()
        return interface.open_session(app, request)

    def save_session(self, app, session, response):
        interface = self.frame if in_marketplace_frame(request) else super()
        interface.save_session(app, session, response)


def issue_first_party_link(service, raw, browser):
    """Copy signed registration hints only. OTP, consent and admin grants stay behind."""
    ticket = secrets.token_urlsafe(32)
    with service.store.connect() as db:
        source = db.execute('SELECT expires,payload FROM marketplace_intents WHERE id_hash=? AND browser_hash=?',
                            (service._hash(raw), service._hash(browser))).fetchone()
        db.execute('INSERT INTO marketplace_intents(id_hash,browser_hash,created,expires,payload) VALUES(?,?,?,?,?)',
                   (service._hash(ticket), UNCLAIMED_BROWSER, service.clock(), source['expires'], source['payload']))
    return ticket


def claim_first_party_link(service, ticket, browser):
    if not isinstance(ticket, str) or len(ticket) != 43:
        return False
    with service.store.connect() as db:
        claimed = db.execute('UPDATE marketplace_intents SET browser_hash=? '
                             'WHERE id_hash=? AND browser_hash=? AND expires>?',
                             (service._hash(browser), service._hash(ticket), UNCLAIMED_BROWSER, service.clock()))
        return claimed.rowcount == 1
