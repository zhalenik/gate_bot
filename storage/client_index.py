"""Keep durable people/client identities in the same transaction as access records."""
from datetime import datetime
import json
import uuid


def sync_client_index(db, links, previous=None):
    # Migration imports losslessly before IdentityService bootstraps its schema.
    if not db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="branch_clients"').fetchone():
        return
    from link_manager import normalize_phone
    previous = previous or {}
    rows = {r['legacy_token']:r['person_id'] for r in db.execute('SELECT legacy_token,person_id FROM branch_clients')}
    # This is an index of current clients, not a second archive. Keep persons,
    # memberships and historical analytics independent, but remove dead links.
    for token in set(rows)-set(links):
        db.execute('DELETE FROM branch_clients WHERE legacy_token=?',(token,))
    changed_phones = {}
    for token, client in links.items():
        if not isinstance(client,dict) or not client.get('branch_id'):
            continue
        person_id = rows.get(token)
        phone = normalize_phone(client.get('phone')) or None
        created = None
        try:
            created = datetime.fromisoformat(str(client.get('created','')).replace('Z','+00:00')).timestamp()
        except (ValueError,TypeError,OverflowError):
            pass
        if not person_id:
            person_id = client.get('person_id')
            if not person_id or not db.execute('SELECT 1 FROM persons WHERE id=?',(person_id,)).fetchone():
                person_id = uuid.uuid4().hex
                db.execute('INSERT INTO persons(id,name,phone,email,created) VALUES(?,?,?,?,?)',
                    (person_id,str(client.get('user_name') or '')[:200],phone,str(client.get('email') or ''),created or 0))
            db.execute('INSERT INTO branch_clients(id,person_id,branch_id,legacy_token,created) VALUES(?,?,?,?,?)',
                (token,person_id,str(client['branch_id']),token,created or 0))
        client['person_id'] = person_id
        old = previous.get(token)
        if old and normalize_phone(old.get('phone')) != (phone or ''):
            changed_phones[person_id] = phone
        if created is not None and db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="client_branch_events"').fetchone():
            db.execute('INSERT OR IGNORE INTO client_branch_events VALUES(?,?,?,?,?)',
                (token,str(client['branch_id']),person_id,created,'legacy' if token in previous else 'local'))
    for person_id,phone in changed_phones.items():
        db.execute('UPDATE persons SET phone=?,phone_verified=0 WHERE id=?',(phone,person_id))
        db.execute('DELETE FROM bot_bindings WHERE person_id=?',(person_id,))
        db.execute('UPDATE identity_tickets SET used=1 WHERE person_id=? AND purpose IN ("admin_binding","email")',(person_id,))
        for client in links.values():
            if client.get('person_id') == person_id:
                if normalize_phone(client.get('phone')) != (phone or ''):
                    client['id_ycl'] = None
                client.update(phone=phone or '',tg_id=None,max_id=None)
    # Device proof belongs to the exact branch/client/phone identity. Revoke in
    # the same transaction as every writer (manual edit, CRM sync or removal),
    # even if the phone is later changed back before that browser visits again.
    if db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="gate_qr_devices"').fetchone():
        for token, old in previous.items():
            current = links.get(token)
            if not current or str(current.get('branch_id')) != str(old.get('branch_id')) or normalize_phone(current.get('phone')) != normalize_phone(old.get('phone')):
                db.execute('UPDATE gate_qr_devices SET revoked=1 WHERE client_token=?', (token,))
                db.execute('UPDATE identity_otp SET state="superseded" WHERE id IN (SELECT challenge_id FROM gate_qr_challenges WHERE client_token=?) AND state IN ("sending","ready")', (token,))
    from yclients_client_sync_jobs import enqueue_client_changes
    enqueue_client_changes(db, links, previous)


def sync_admin_roles(db, admins):
    if not db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="branch_memberships"').fetchone():
        return
    from storage.sql_documents import read_document, write_document
    links=read_document(db,'links.json')
    active={}
    for username,admin in admins.items():
        if not isinstance(admin,dict) or admin.get('disabled') or not admin.get('person_id'):
            continue
        person=admin['person_id']
        roles=active.setdefault(person,set())
        if admin.get('is_super'):roles.add('*')
        else:roles.update(r['branch_id'] for r in db.execute('SELECT branch_id FROM branch_memberships WHERE person_id=? AND branch_id=? AND status="active"',(person,str(admin.get('branch_id')))))
    changed=False
    for client in links.values():
        person=client.get('admin_person_id')
        if person:
            roles=active.get(person,set())
            enabled='*' in roles or str(client.get('branch_id')) in roles
            if client.get('admin_role_active') is not enabled:
                client['admin_role_active']=enabled;changed=True
    if changed:write_document(db,'links.json',links)
