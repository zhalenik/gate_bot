"""Durable eligibility across deletion and registration of a branch."""
import hashlib
import hmac
import secrets

from storage.sql_documents import read_document


def _table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def setup(db):
    created = not _table(db, 'trial_redemptions')
    db.execute('CREATE TABLE IF NOT EXISTS trial_redemptions(kind TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(kind,digest))')
    db.execute('CREATE TABLE IF NOT EXISTS trial_identity_key(id INTEGER PRIMARY KEY CHECK(id=1),value BLOB NOT NULL)')
    if not db.execute('SELECT 1 FROM trial_identity_key').fetchone():
        db.execute('INSERT INTO trial_identity_key VALUES(1,?)', (secrets.token_bytes(32),))
    if created and _table(db, 'documents'):
        branches = read_document(db, 'branches.json')
        for row in db.execute('SELECT branch_id FROM billing_accounts WHERE trial_used=1'):
            claim(db, row['branch_id'], branches=branches)


def claim(db, branch_id, *, phone=None, branches=None):
    """True for first registration, including trusted non-registration billing operations."""
    if branches is None:
        branches = read_document(db, 'branches.json') if _table(db, 'documents') else {}
    branch = branches.get(str(branch_id))
    if branch is None:
        return True
    if branch.get('id_origin') == 'yclients' or (
            branch.get('id_origin') != 'internal' and branch.get('yclients_enabled', True)):
        kind, value = 'yclients', str(branch_id)
    else:
        if not phone and any(r['name']=='yclients_enabled' for r in db.execute('PRAGMA table_info(branch_registrations)')):
            registration = db.execute('SELECT phone FROM branch_registrations WHERE branch_id=? AND '
                                      'yclients_enabled=0 AND state="completed" ORDER BY created LIMIT 1',
                                      (str(branch_id),)).fetchone()
            phone = registration['phone'] if registration else None
        if not phone:
            # A workspace created directly by the service operator has no public registration.
            return True
        from identity_service import valid_phone
        kind, value = 'standalone', valid_phone(phone)
    key = db.execute('SELECT value FROM trial_identity_key WHERE id=1').fetchone()[0]
    digest = hmac.new(key, (kind + ':' + value).encode(), hashlib.sha256).hexdigest()
    return db.execute('INSERT OR IGNORE INTO trial_redemptions VALUES(?,?)', (kind, digest)).rowcount == 1
