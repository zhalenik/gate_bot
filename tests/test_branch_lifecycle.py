"""Confirmed local cascade; preserves other tenants and global super identity."""
from datetime import datetime,timezone
from pathlib import Path
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from werkzeug.datastructures import MultiDict

from branch_lifecycle import BranchLifecycleService
from gate_qr_service import GateQRService
from product_notifications import ProductNotifications
from storage.sql_documents import read_document,write_document
from support_service import SupportService
from telephony_store import TelephonyStore
from test_identity_product import identities,PASSWORD
from yclients_marketplace import MarketplaceService
from storage import config


def delete(service,username='owner',branch='11'):
    preview=service.preview(username,branch)
    return service.delete(username,branch,confirmation=branch,revision=preview['revision'])


def test_super_only_and_explicit_current_confirmation(identities):
    s=identities;service=BranchLifecycleService(s.service)
    with pytest.raises(PermissionError):service.preview('north','11')
    view=service.preview('owner','11')
    for confirmation,revision in [('12',view['revision']),('11','stale')]:
        with pytest.raises(ValueError):service.delete('owner','11',confirmation=confirmation,revision=revision)
    s.branches.update_settings(11,{'name':'Changed'})
    with pytest.raises(ValueError):service.delete('owner','11',confirmation='11',revision=view['revision'])
    assert s.branches.get_branch(11) and s.auth.verify_admin('north',PASSWORD)


def test_cascade_all_branch_data_preserves_other_tenant_and_immutable_guards(identities):
    s=identities;service=BranchLifecycleService(s.service)
    notices=ProductNotifications(s.service,s.links,s.branches,s.billing)
    support=SupportService(s.service,sender=lambda *args:True)
    GateQRService(s.service,s.links,s.branches,SimpleNamespace())
    marketplace=MarketplaceService(s.service,partner_token='synthetic-partner')
    marketplace.begin(MultiDict({'salon_id':'11'}),'test-browser')
    s.service.save_integration('owner','11','chatpush',{'token':'synthetic-branch-token'})
    s.service.save_integration('owner','12','chatpush',{'token':'synthetic-other-token'})
    north=s.service.web_admin('north')['person_id']
    with s.product.connect() as db:
        links=read_document(db,'links.json')
        links['shared-other']={'branch_id':12,'person_id':north,'user_name':'Shared','phone':'+15550100888','gate_access':True}
        write_document(db,'links.json',links)
        db.execute('CREATE TABLE appointment_schedule(branch_id TEXT PRIMARY KEY,records TEXT)')
        db.execute('INSERT INTO appointment_schedule VALUES("11","[]")')
        db.execute('INSERT INTO gate_qr_links VALUES("11","gate","synthetic-qr",1,0,0)')
    other_before=s.links.load_links()['shared-other']
    at=datetime(2026,1,1,tzinfo=timezone.utc)
    for branch in ('11','12'):
        s.billing.activate_trial(branch,at=at)
        result=s.billing.admit('request-'+branch,branch,'synthetic-person','gate',at=at)
        s.billing.finalize(result['command_id'],'command_sent',at=at)
        assert s.billing.sms.reserve_sms(branch,'sms-'+branch,2,phone='+15550100999',at=at)['allowed']
        s.billing.sms.finish_sms('sms-'+branch,'accepted',at=at)
    offer=s.billing.catalog.publish_offer({'filename':'terms.pdf','url':'/admin/billing-assets/'+'a'*32+'.pdf','sha256':'a'*64},actor='owner',at=at)
    order=s.billing.create_order('11','pro',actor='north',idempotency_key='delete-fixture-order',at=at,
                                 offer_version=offer['id'],accept_offer=True)
    message=support.send('north','Synthetic question','support-request-1',branch_id='11')
    with s.product.connect() as db:
        db.execute('INSERT INTO billing_payments VALUES("synthetic-payment",?,350000,"RUB",0)',(order['id'],))
        db.execute('INSERT OR IGNORE INTO support_outbox VALUES("synthetic-support",?,"admin",?,"telegram","pending",0,0,0,NULL)',(message['message_id'],s.service.web_admin('owner')['person_id']))
    s.billing.activate_test_tariff('11','start',actor='owner',idempotency_key='delete-test-activation',confirm_burn=True,at=at)
    s.billing.cancel_subscription('11',actor='north',immediate=True,confirm_burn=True,at=at)
    result=delete(service)
    assert result['deleted'] and result['admins']==1 and result['clients']>=2
    assert s.branches.get_branch(11) is None and s.branches.get_branch(12)
    assert not s.auth.verify_admin('north',PASSWORD) and s.auth.verify_admin('owner',PASSWORD)
    assert s.links.load_links()=={'shared-other':other_before}
    assert s.service.integration_credentials('11','chatpush')=={}
    assert s.service.integration_credentials('12','chatpush')['token']=='synthetic-other-token'
    with s.product.connect() as db:
        for table in ('branch_clients','branch_memberships','identity_integrations','billing_accounts','billing_terms','billing_periods','hardware_commands','gate_requests','product_events','support_threads','appointment_schedule','gate_qr_links','client_branch_events','billing_term_details','billing_consents','billing_cancelled_terms','billing_test_activations','sms_billing_commands','sms_quota_ledger'):
            assert db.execute('SELECT count(*) FROM '+table+' WHERE branch_id="11"').fetchone()[0]==0
        assert db.execute('SELECT 1 FROM persons WHERE id=?',(north,)).fetchone()
        assert db.execute('SELECT count(*) FROM marketplace_intents').fetchone()[0]==0
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        assert db.execute('SELECT count(*) FROM quota_ledger').fetchone()[0]>0
        assert db.execute('SELECT count(*) FROM sms_quota_ledger WHERE branch_id="12"').fetchone()[0]>0
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM quota_ledger')
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM sms_quota_ledger')
        with pytest.raises(sqlite3.IntegrityError):db.execute('DELETE FROM product_events')
    s.service.import_legacy()
    assert s.branches.get_branch(11) is None and not s.auth.verify_admin('north',PASSWORD)


def test_busy_branch_cannot_be_deleted_until_command_finishes(identities):
    s=identities;service=BranchLifecycleService(s.service)
    at=datetime(2026,1,1,tzinfo=timezone.utc)
    s.billing.activate_trial('11',at=at)
    result=s.billing.admit('pending','11','person','gate',at=at)
    assert not service.preview('owner','11')['can_delete']
    with pytest.raises(ValueError):delete(service)
    assert s.branches.get_branch(11)
    s.billing.finalize(result['command_id'],'cancelled',at=at)
    assert delete(service)['deleted']


def test_deleted_branch_cannot_be_recreated_by_inflight_appointment_snapshot(identities):
    s=identities;delete(BranchLifecycleService(s.service))
    assert s.links.reconcile_appointments('11',{'987':{'name':'Old CRM response','phone':'+15550100666'}})['changed']==0
    assert not s.links.load_links()


def test_failure_rolls_back_documents_cascade_and_immutable_triggers(identities):
    s=identities;service=BranchLifecycleService(s.service)
    s.billing.activate_trial('11',at=datetime(2026,1,1,tzinfo=timezone.utc))
    before=s.links.load_links()
    with s.product.connect() as db:
        db.execute('CREATE TRIGGER synthetic_delete_failure BEFORE DELETE ON billing_accounts WHEN OLD.branch_id="11" BEGIN SELECT RAISE(ABORT,"synthetic failure"); END')
    with pytest.raises(sqlite3.IntegrityError):delete(service)
    assert s.links.load_links()==before and s.branches.get_branch(11)
    assert s.auth.verify_admin('north',PASSWORD)
    with s.product.connect() as db:
        assert {row['name'] for row in db.execute('SELECT name FROM sqlite_master WHERE type="trigger"')} >= {'quota_no_delete','payments_no_delete','product_events_no_delete'}
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('effect_state', ['sending', 'queued'])
def test_inflight_direct_message_effect_blocks_deletion(identities, effect_state):
    s=identities;service=BranchLifecycleService(s.service)
    ProductNotifications(s.service,s.links,s.branches,s.billing)
    with s.product.connect() as db:db.execute('INSERT INTO message_effects VALUES("synthetic-effect",?,NULL,0)', (effect_state,))
    assert not service.preview('owner','11')['can_delete']
    with pytest.raises(ValueError):delete(service)
    assert s.auth.verify_admin('north',PASSWORD)


def test_queued_sms_blocks_deletion_until_modem_result_is_reconciled(identities, monkeypatch):
    s=identities;service=BranchLifecycleService(s.service)
    s.billing.activate_trial('11')
    journal=TelephonyStore(Path(s.product.path).parent/'telephony.json')
    notices=ProductNotifications(s.service,s.links,s.branches,s.billing,telephony=journal)
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    queue=Mock();queue.dry_run=False;queue.journal=journal
    queue.add_manual.return_value={'status':'success','delivery':'queued','command_id':'queued-sms'}
    queue.status.return_value={'recent':[]}
    monkeypatch.setattr('sim800c._get_queue',lambda:queue)
    notices.hardware=queue
    result=notices.send('sms','+15550100555','Synthetic test code',
                        {'branch_id':'11','purpose':'otp','idempotency_key':'queued-confirmation'})
    assert result['delivery']=='queued'
    notices.reconcile_hardware()
    with pytest.raises(ValueError):delete(service)
    assert s.branches.get_branch(11)
    journal.update(result['record_id'],status='network_accepted',segments=[{'sequence':1,'status':'network_accepted'}])
    queue.status.return_value={'recent':[{'command_id':'queued-sms','status':'network_accepted'}]}
    notices.reconcile_hardware()
    assert notices.effect_status('sms','queued-confirmation')['status']=='network_accepted'
    assert delete(service)['deleted']
    assert queue.add_manual.call_count==1


@pytest.mark.parametrize('event_name', ['crm_error', 'crm_recovered'])
def test_event_snapshot_cannot_enqueue_after_branch_deletion(identities, monkeypatch, event_name):
    s=identities;service=BranchLifecycleService(s.service)
    notices=ProductNotifications(s.service,s.links,s.branches,s.billing)
    person_id=s.service.web_admin('owner')['person_id']
    contacts=[{'person_id':person_id,'username':'owner','channel':'telegram','target':'synthetic-chat'}]
    monkeypatch.setattr(s.service,'contacts_for_admin_events',lambda *_:contacts)
    notices.event(event_name,'11',{'event_id':'before-deletion'})
    with s.product.connect() as db:
        assert db.execute('SELECT count(*) FROM message_outbox WHERE branch_id="11"').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM notification_inbox WHERE branch_id="11"').fetchone()[0]==2

    def contacts_then_delete(*_):
        # The sender has already read the branch and its recipients; deletion
        # commits immediately before the event starts its write transaction.
        assert delete(service)['deleted']
        return contacts

    monkeypatch.setattr(s.service,'contacts_for_admin_events',contacts_then_delete)
    notices.event(event_name,'11',{'event_id':'racing-deletion'})
    with s.product.connect() as db:
        assert db.execute('SELECT count(*) FROM message_outbox').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM notification_inbox').fetchone()[0]==0

    # Global modem events must still reach the surviving superadministrator.
    monkeypatch.setattr(s.service,'contacts_for_admin_events',lambda *_:contacts)
    notices.event('call_received',None,{'event_id':'global-call'})
    with s.product.connect() as db:
        assert db.execute('SELECT count(*) FROM message_outbox WHERE branch_id IS NULL').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM notification_inbox WHERE person_id=? AND branch_id IS NULL',(person_id,)).fetchone()[0]==1


def test_late_branch_message_cannot_claim_effect_after_deletion(identities,monkeypatch):
    s=identities;service=BranchLifecycleService(s.service)
    notices=ProductNotifications(s.service,s.links,s.branches,s.billing)
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    smtp=Mock(return_value=True);monkeypatch.setattr('smtp_mailing.send_email',smtp)
    delete(service)
    result=notices.send('email','synthetic@example.invalid','Old branch message',{'branch_id':'11','idempotency_key':'late-message'})
    assert result['status']=='skipped' and result['code']=='branch_deleted'
    smtp.assert_not_called()
    with s.product.connect() as db:assert db.execute('SELECT count(*) FROM message_effects').fetchone()[0]==0


def test_real_registration_otp_claim_remains_allowed_before_branch_creation(identities,monkeypatch):
    s=identities;notices=ProductNotifications(s.service,s.links,s.branches,s.billing)
    monkeypatch.setattr(config,'OUTBOUND_ENABLED',True)
    queue=Mock();queue.dry_run=False;queue.journal.add.return_value='synthetic-record'
    queue.add_manual.return_value={'status':'success','delivery':'queued'}
    monkeypatch.setattr('sim800c._get_queue',lambda:queue)
    with s.product.connect() as db:
        db.execute('INSERT INTO identity_otp(id,subject,phone,code_hash,expires,state,created,ip_hash) VALUES("pending-registration","registration:synthetic","+15550100555","synthetic",9999999999,"sending",0,"synthetic")')
    result=notices.send('sms','+15550100555','Synthetic test code',{'branch_id':'99','purpose':'otp','billing_exempt':'registration','idempotency_key':'pending-registration'})
    assert result['status']=='success'
    assert queue.add_manual.call_count==1
