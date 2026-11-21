"""Real outbox/HTML composition with mocked SMTP; no real messages or sockets."""
import re
from types import SimpleNamespace
from html.parser import HTMLParser
import pytest
from test_identity_product import identities,accept,PASSWORD,PHONE
from product_notifications import ProductNotifications
from storage import config
from storage.sql_documents import read_document
from welcome_email import render_welcome


@pytest.fixture
def welcome(identities,monkeypatch):
    state=identities
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    monkeypatch.setattr(config,'BASE_URL','https://gatebot.example.invalid')
    ready=[True];messages=[]
    monkeypatch.setattr('smtp_settings.SmtpSettings',lambda:SimpleNamespace(effective=lambda:{'enabled':ready[0]}))
    monkeypatch.setattr('smtp_mailing.send_email',lambda *args,**kwargs:messages.append(args) or True)
    notifications=ProductNotifications(state.service,state.links,state.branches,state.billing)
    state.service.welcome_sink=notifications.welcome
    return SimpleNamespace(state=state,notices=notifications,ready=ready,messages=messages)

def rows(welcome):
    with welcome.state.product.connect() as db:
        return [dict(row) for row in db.execute('SELECT * FROM message_outbox WHERE audience="welcome"')]

@pytest.mark.parametrize('flow',['invite','yclients','standalone'])
def test_each_registration_commits_one_encrypted_welcome_and_sends_once(welcome,flow):
    w=welcome;s=w.state
    if flow=='invite':
        result,raw,otp,code=accept(s)
    else:
        raw=s.service.register_branch('99' if flow=='yclients' else None,'Welcome branch','New owner',PHONE,yclients_enabled=flow=='yclients')['registration_token']
        otp=s.service.request_otp(raw,registration=True);code=re.search(r'\b[0-9]{6}\b',s.sent[-1][2]).group()
        result=s.service.complete_registration(raw,otp['challenge_id'],code,'welcome@example.invalid',PASSWORD,billing=s.billing)
    row=rows(w)[0]
    assert len(rows(w))==1 and row['state']=='pending'
    assert result['username'] not in row['payload'] and PASSWORD not in row['payload'] and code not in row['payload']
    w.notices.welcome(result['person_id'],result['branch_id'],result['username'])
    assert len(rows(w))==1
    # A different worker instance consumes the same durable row after restart.
    restarted=ProductNotifications(s.service,s.links,s.branches,s.billing)
    restarted.process_once();restarted.process_once()
    assert len(w.messages)==1
    recipient,subject,html=w.messages[0]
    assert recipient==result['username'] and '<html' in html and 'GateBot' in subject
    assert PASSWORD not in html and code not in html and '/opn/' not in html and '/invite/' not in html
    assert 'https://gatebot.example.invalid/admin/login' in html
    assert rows(w)[0]['state']=='accepted'
    with s.product.connect() as db:
        assert not db.execute('SELECT email_verified FROM persons WHERE id=?',(result['person_id'],)).fetchone()[0]

@pytest.mark.parametrize('disabled',['smtp','outbound'])
def test_disabled_delivery_keeps_welcome_pending(welcome,monkeypatch,disabled):
    w=welcome;accept(w.state)
    if disabled=='smtp':w.ready[0]=False
    else:monkeypatch.setattr(config,'OUTBOUND_ENABLED',False)
    w.notices.process_once()
    assert not w.messages and rows(w)[0]['state']=='pending'
    if disabled=='smtp':w.ready[0]=True
    else:monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    w.notices.process_once()
    assert len(w.messages)==1

@pytest.mark.parametrize('change',['email','revoked','disabled','different_primary'])
def test_recipient_and_role_are_rechecked_before_smtp(welcome,change):
    w=welcome;s=w.state;result,*_=accept(s)
    if change=='email':s.service.admin_email(result['username'],'changed@example.invalid')
    elif change=='revoked':s.service.revoke_admin('owner',result['person_id'],'11')
    else:
        with s.auth.store.transaction() as admins:
            if change=='disabled':admins[result['username']]['disabled']=True
            else:admins[result['username']]['branch_id']=12
    w.notices.process_once()
    assert rows(w)[0]['state']=='skipped' and w.messages==[]

def test_welcome_write_failure_rolls_back_account_and_outbox_together(welcome,monkeypatch):
    w=welcome;s=w.state
    invitation=s.service.invite('owner','11',client_token='example-client');raw=invitation['link'].rsplit('/',1)[1]
    otp=s.service.request_otp(raw);code=re.search(r'\b[0-9]{6}\b',s.sent[-1][2]).group()
    original=w.notices._enqueue
    def interrupted(*args,**kwargs):
        original(*args,**kwargs)
        raise OSError('synthetic transaction interruption')
    monkeypatch.setattr(w.notices,'_enqueue',interrupted)
    with pytest.raises(OSError):s.service.accept_invitation(raw,otp['challenge_id'],code,'Name','atomic@example.invalid',PASSWORD)
    assert rows(w)==[] and 'atomic@example.invalid' not in s.auth.load_admins()
    with s.product.connect() as db:
        assert db.execute('SELECT state FROM identity_otp WHERE id=?',(otp['challenge_id'],)).fetchone()[0]=='ready'
        assert not read_document(db,'links.json')['example-client'].get('admin_role_active')
    monkeypatch.setattr(w.notices,'_enqueue',original)
    s.service.accept_invitation(raw,otp['challenge_id'],code,'Name','atomic@example.invalid',PASSWORD)
    assert len(rows(w))==1

def test_html_escapes_names_and_has_only_nonsecret_first_party_links():
    html=render_welcome({'name':'<img src=x onerror=alert(1)>','branch_name':'Branch & "quotes"','yclients_enabled':True},'https://gatebot.example.invalid')
    assert '<img' not in html and '&lt;img' in html and 'Branch &amp; &quot;quotes&quot;' in html
    class Links(HTMLParser):
        def __init__(self):super().__init__();self.links=[]
        def handle_starttag(self,tag,attrs):
            if tag=='a':self.links.append(dict(attrs)['href'])
    parsed=Links();parsed.feed(html)
    assert set(parsed.links)=={'https://gatebot.example.invalid/admin/login','https://gatebot.example.invalid/admin/help'}
