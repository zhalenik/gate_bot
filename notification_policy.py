"""Branch notification policy shared by creation, delivery and preference screens."""
import json

from storage import config
from storage.sql_documents import read_document


DISABLED_MESSAGE = 'Уведомления отключены администратором'


def delivery_audience(context):
    if context.get('purpose') in {'otp','email_verification','api_otp'}:
        return 'verification'
    if context.get('audience') in {'admin','office','welcome'} or context.get('event') in {'admin_invitation','admin_welcome'}:
        return 'admin'
    return 'client'


def transport_enabled(audience):
    if audience == 'verification':
        # Split settings govern notifications. An installation still using
        # the legacy all-transport stop retains that existing behavior.
        return (config.CLIENT_OUTBOUND_ENABLED is not None or config.ADMIN_OUTBOUND_ENABLED is not None
                or config.OUTBOUND_ENABLED)
    return config.outbound_enabled(audience)


def client_delivery_enabled(db, branch_id):
    if not config.outbound_enabled('client'):
        return False
    if branch_id is None:
        return True
    branch = read_document(db, 'branches.json').get(str(branch_id))
    return bool(branch and branch.get('client_notifications_enabled', True))


def seed_client_channels(db, person_id, client, previous):
    """Seed ordinary clients once, revisiting only an untouched default upon CRM matching."""
    if client.get('is_temporary') or client.get('generated_admin_client') or client.get('mailing_opted_out'):
        return
    if previous and previous.get('id_ycl') == client.get('id_ycl'):
        return
    branch_id = str(client['branch_id'])
    row = db.execute('SELECT payload FROM notification_preferences WHERE person_id=? AND audience="client" AND branch_id=?',
                     (person_id, branch_id)).fetchone()
    if ((row and not json.loads(row['payload']).get('auto_route_defaults')) or
            (previous and not row)):
        return
    branch = read_document(db, 'branches.json').get(branch_id, {})
    crm = branch.get('yclients_enabled', True)
    chatpush = crm and db.execute('SELECT 1 FROM identity_integrations WHERE branch_id=? AND kind="chatpush"', (branch_id,)).fetchone()
    messenger = bool(crm and (client.get('id_ycl') or chatpush))
    sms = not messenger and branch.get('sms_notifications_enabled') is True
    prefs = {'channels': {'messengers': messenger, 'sms': sms,
                          'telegram': bool(client.get('tg_id') and client.get('mailing_enabled')),
                          'max': bool(client.get('max_id') and client.get('mailing_enabled'))},
             'sms_consent_version': 1, 'auto_route_defaults': True}
    if sms or messenger:
        client['mailing_enabled'] = True
    db.execute('INSERT OR REPLACE INTO notification_preferences VALUES(?,"client",?,?)',
               (person_id, branch_id, json.dumps(prefs)))


def clear_disabled_sms(db, previous, current):
    branches = [key for key, branch in current.items() if previous.get(key, {}).get('sms_notifications_enabled')
                and branch.get('sms_notifications_enabled') is not True]
    if not branches or not db.execute('SELECT 1 FROM sqlite_master WHERE name="notification_preferences"').fetchone():
        return
    for branch in branches:
        rows = db.execute('SELECT person_id,payload FROM notification_preferences WHERE audience="client" AND branch_id=?',
                          (branch,)).fetchall()
        for row in rows:
            prefs = json.loads(row['payload'])
            if prefs.get('channels', {}).get('sms'):
                prefs['channels']['sms'] = False
                db.execute('UPDATE notification_preferences SET payload=? WHERE person_id=? AND audience="client" AND branch_id=?',
                           (json.dumps(prefs), row['person_id'], branch))
