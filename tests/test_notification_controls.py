"""Branch controls, delivery priorities and administrative events with offline transports."""
import pytest

from messenger_service import MessengerService
from product_notifications import ProductNotifications
from storage import config
from test_identity_product import identities
from test_client_notification_web import preferences_web
from test_web_security import web, login, post


def test_new_channels_follow_recipient_and_disabled_sms_stays_off(identities,monkeypatch):
    s=identities
    monkeypatch.setattr(config,'YC_PARTNER_TOKEN','offline-partner')
    s.branches.update_settings('11',{'sms_notifications_enabled':True})
    sms=s.links.create_link({'branch_id':11,'phone':'+15550120101'})
    assert s.service.prefs_for_token(sms)['effective_channels']['sms']
    s.service.save_integration('north','11','chatpush',{'token':'offline-chatpush'})
    cp=s.links.create_link({'branch_id':11,'phone':'+15550120102'})
    s.service.save_integration('owner','11','yclients',{'user_token':'offline-yc'})
    yc=s.links.create_link({'branch_id':11,'phone':'+15550120103','id_ycl':19})
    assert all(s.service.prefs_for_token(t)['effective_channels']['messengers'] for t in (cp,yc))
    with s.product.connect() as db:
        service=MessengerService(s.service)
        assert service.plan(db,11,cp)['provider']=='chatpush'
        assert service.plan(db,11,yc)['provider']=='yclients'
    s.branches.update_settings('11',{'sms_notifications_enabled':False})
    s.branches.update_settings('11',{'sms_notifications_enabled':True})
    assert not s.service.prefs_for_token(sms)['channels']['sms']
    assert s.service.prefs_for_token(cp)['channels']['messengers']

    service.save('north','11','sms')
    fresh=s.links.create_link({'branch_id':11,'phone':'+15550120104','id_ycl':20})
    assert s.service.prefs_for_token(fresh)['channels']['messengers']
    assert not s.service.prefs_for_token(fresh)['channels']['sms']
    s.service.save_prefs_for_token(fresh,{'enabled':True,'channels':{'sms':True}})
    notices=ProductNotifications(s.service,s.links,s.branches,s.billing)
    for token in (cp,fresh):
        notices.enqueue({**s.links.load_links()[token],'token':token,'event':'granted'})
    with s.product.connect() as db:
        rows=db.execute('SELECT channel,payload FROM message_outbox WHERE audience="client"').fetchall()
    assert [(row['channel'],notices._decrypt(row['payload'])['token']) for row in rows]==[('sms',fresh)]


def test_events_control_web_and_queued_external_separately(identities,monkeypatch):
    s=identities;sent=[]
    n=ProductNotifications(s.service,s.links,s.branches,s.billing,sender=lambda *args:sent.append(args) or {'status':'accepted'})
    ticket=s.service.issue_bind_ticket('north','telegram')
    s.service.bind_ticket('telegram','71009',ticket)
    s.service.save_admin_preferences('north',{'events':{'gate_requested':{'telegram':True}}})
    n.event('gate_requested','11',{'event_id':'first'})
    s.service.select_admin_events('north',[])
    n.event('gate_requested','11',{'event_id':'second'})
    n.process_once()
    assert not sent
    with s.product.connect() as db:
        person=s.service.web_admin('north')['person_id']
        assert db.execute('SELECT count(*) FROM notification_inbox WHERE person_id=? AND event="gate_requested"',(person,)).fetchone()[0]==1
    s.service.select_admin_events('north',['gate_requested'])
    assert s.service.admin_preferences('north')['events']['gate_requested']['telegram']
    monkeypatch.setattr(config,'CLIENT_OUTBOUND_ENABLED',False)
    monkeypatch.setattr(config,'ADMIN_OUTBOUND_ENABLED',True)
    n.event('gate_requested','11',{'event_id':'third'})
    n.process_once()
    assert [args[0] for args in sent]==['telegram']


def test_branch_journal_scope_content_pause_and_resume(preferences_web,monkeypatch):
    web,identity=preferences_web
    n=web.module.product_services().notifications
    browser=web.app.test_client();csrf=login(browser,'tenant-a')
    page='/admin/client-notices'
    with n.store.connect() as db:
        for branch,message,token in [('12','Branch example',web.token_a),('123','Other example',web.token_b)]:
            n._enqueue('snapshot:'+branch,'client','access_granted',branch,'email','sample@example.invalid',
                       {'token':token,'text':message+' /opn/'+token},db)
    view=browser.get(page)
    assert view.status_code==200 and 'Branch example' in view.text and 'Other example' not in view.text
    assert web.token_a in view.text and web.token_b not in view.text
    assert browser.get(page+'?branch_id=123').status_code==403
    def toggle(branch,enabled):
        return browser.post(page,data={'csrf_token':csrf,'branch_id':branch,'enabled':enabled})
    assert toggle('123','0').status_code==403
    before=identity.prefs_for_token(web.token_a)
    assert toggle('12','0').status_code==302
    after=identity.prefs_for_token(web.token_a)
    assert not after['delivery_enabled'] and after['channels']==before['channels']
    with pytest.raises(PermissionError):
        identity.save_prefs_for_token(web.token_a,{'enabled':False,'channels':{}})
    assert n.send('email','example@example.invalid','test',{'event':'manual_pass','branch_id':'12'})['code']=='outbound_disabled'
    assert toggle('12','1').status_code==302
    assert identity.prefs_for_token(web.token_a)['delivery_enabled']
    monkeypatch.setattr(config,'CLIENT_OUTBOUND_ENABLED',False)
    assert not identity.prefs_for_token(web.token_a)['delivery_enabled']
    assert identity.prefs_for_token(web.token_a)['channels']==before['channels']


def test_read_all_limited_to_current_account(preferences_web):
    web,identity=preferences_web
    n=web.module.product_services().notifications
    browser=web.app.test_client()
    with n.store.connect() as db:
        for name,branch in [('tenant-a','12'),('tenant-b','123')]:
            person=identity.web_admin(name)['person_id']
            if not person:
                identity.attach_console_account(name,'+15550120'+('101' if branch=='12' else '102'))
                person=identity.web_admin(name)['person_id']
            db.execute('INSERT INTO notification_inbox VALUES(?,?,?,?,?,1,NULL)',(name,person,branch,'gate_requested',n._encrypt({'text':'Offline notice'})))
    csrf=login(browser,'tenant-a')
    assert post(browser,csrf,'/admin/api/notification-inbox/read-all',{}).status_code==200
    with n.store.connect() as db:
        assert db.execute('SELECT read_at FROM notification_inbox WHERE id="tenant-a"').fetchone()[0]
        assert not db.execute('SELECT read_at FROM notification_inbox WHERE id="tenant-b"').fetchone()[0]
