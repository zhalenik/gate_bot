"""Keep durable people/client identities in the same transaction as access records."""
from datetime import datetime
import json
import uuid
import hashlib


def sync_client_index(db, links, previous=None):
    # Migration imports losslessly before IdentityService bootstraps its schema.
    if not db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="branch_clients"').fetchone():
        return
    from link_manager import normalize_phone
    from storage.client_roles import directory_role
    event_fields = {r['name'] for r in db.execute('PRAGMA table_info(client_branch_events)')}
    if event_fields and 'client_role' not in event_fields:
        db.execute("ALTER TABLE client_branch_events ADD COLUMN client_role TEXT NOT NULL DEFAULT 'unknown'")
    previous = previous or {}
    rows = {r['legacy_token']:r['person_id'] for r in db.execute('SELECT legacy_token,person_id FROM branch_clients')}
    # This is an index of current clients, not a second archive. Keep persons,
    # memberships and historical analytics independent, but remove dead links.
    removed = set(rows)-set(links)
    tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")} if removed else set()
    for token in removed:
        db.execute('DELETE FROM branch_clients WHERE legacy_token=?',(token,))
        if 'guest_issues' in tables:
            db.execute('DELETE FROM guest_issues WHERE guest=? OR owner=?',(token,token))
        if previous.get(token, {}).get('is_temporary'):
            db.execute('CREATE TABLE IF NOT EXISTS removed_guest_links(digest TEXT PRIMARY KEY)')
            db.execute('INSERT OR IGNORE INTO removed_guest_links VALUES(?)',(hashlib.sha256(token.encode()).hexdigest(),))
            if 'branch_join_proofs' in tables:
                db.execute('DELETE FROM identity_otp WHERE subject IN (SELECT subject FROM branch_join_proofs WHERE client_token=?)',(token,))
                db.execute('DELETE FROM branch_join_proofs WHERE client_token=?',(token,))
            if 'gate_qr_challenges' in tables:
                db.execute('DELETE FROM identity_otp WHERE id IN (SELECT challenge_id FROM gate_qr_challenges WHERE client_token=?)',(token,))
                db.execute('DELETE FROM gate_qr_challenges WHERE client_token=?',(token,))
                db.execute('DELETE FROM gate_qr_devices WHERE client_token=?',(token,))
            person = rows[token]
            if not any(db.execute('SELECT 1 FROM '+table+' WHERE person_id=?',(person,)).fetchone()
                       for table in ('branch_clients','web_accounts','branch_memberships','system_roles')):
                for table in ('notification_preferences','identity_tickets','bot_bindings'):
                    db.execute('DELETE FROM '+table+' WHERE person_id=?',(person,))
                db.execute('DELETE FROM persons WHERE id=?',(person,))
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
        if not old or old.get('id_ycl') != client.get('id_ycl'):
            from notification_policy import seed_client_channels
            seed_client_channels(db, person_id, client, old)
        if old and normalize_phone(old.get('phone')) != (phone or ''):
            changed_phones[person_id] = phone
        if created is not None and event_fields:
            db.execute('INSERT OR IGNORE INTO client_branch_events VALUES(?,?,?,?,?,?)',
                (token,str(client['branch_id']),person_id,created,'legacy' if token in previous else 'local', directory_role(client)))
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
    has_push = db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="pwa_subscriptions"').fetchone()
    has_desk = db.execute('SELECT 1 FROM sqlite_master WHERE type="table" AND name="desk_threads"').fetchone()
    if has_push or has_desk:
        for token, old in previous.items():
            current = links.get(token)
            if current and str(current.get('branch_id')) == str(old.get('branch_id')) and normalize_phone(current.get('phone')) == normalize_phone(old.get('phone')):
                continue
            # An address or number restored later must not revive previous authority.
            if has_push:
                db.execute('DELETE FROM pwa_subscriptions WHERE client_token=?', (token,))
            if has_desk:
                db.execute('DELETE FROM desk_shares WHERE token=?',(token,))
                db.execute('DELETE FROM desk_threads WHERE token=?',(token,))
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
