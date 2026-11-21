"""On-site client enrolment, scoped to a printed branch QR and an SMS-verified browser."""
import json
import secrets
from datetime import datetime, timezone

from gate_qr_service import GateQRUnavailable
from identity_service import valid_email
from link_manager import access_is_active, normalize_phone
from storage import config
from storage.sql_documents import read_document, write_document
from timeutil import pass_expiry


class BranchJoin:
    def __init__(self, identity, links, directory):
        self.identity, self.links, self.directory = identity, links, directory
        self.product, self.clock = identity.product, identity.clock
        with self.product.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS branch_join_codes(branch_id TEXT PRIMARY KEY,token TEXT UNIQUE NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS branch_join_proofs(subject TEXT PRIMARY KEY,branch_id TEXT NOT NULL,'
                       'phone TEXT NOT NULL,expires REAL NOT NULL,profile TEXT,client_token TEXT)')

    def admin_view(self, username, branch_id, action=None):
        with self.product.connect() as db:
            self.identity._scope(db, username, branch_id)
            key = str(branch_id)
            row = db.execute('SELECT token FROM branch_join_codes WHERE branch_id=?', (key,)).fetchone()
            if action not in {None, 'revoke', 'create', 'rotate'}:
                raise ValueError('Неизвестное действие')
            if action == 'revoke' or action == 'rotate' or (action == 'create' and not row):
                db.execute('DELETE FROM branch_join_proofs WHERE branch_id=?', (key,))
                db.execute('DELETE FROM branch_join_codes WHERE branch_id=?', (key,))
                if action != 'revoke':
                    db.execute('INSERT INTO branch_join_codes VALUES(?,?)', (key, secrets.token_urlsafe(32)))
                row = db.execute('SELECT token FROM branch_join_codes WHERE branch_id=?', (key,)).fetchone()
            return {'enabled': bool(row), 'branch_id': key, 'gate_label': 'QR-код регистрации в филиале',
                    'url': config.BASE_URL.rstrip('/') + '/join/' + row['token'] if row else None}

    def _branch(self, db, raw):
        row = db.execute('SELECT branch_id FROM branch_join_codes WHERE token=?', (raw,)).fetchone()
        branch = read_document(db, 'branches.json').get(row['branch_id']) if row else None
        if not branch or not branch.get('enabled', True):
            raise GateQRUnavailable('Регистрация по этой ссылке недоступна.')
        return row['branch_id'], branch

    def name(self, raw):
        with self.product.connect() as db:
            return self._branch(db, raw)[1]['name']

    def _subject(self, branch_id, raw, browser):
        return 'branch_join:' + branch_id + ':' + self.identity._hash(raw + ':' + browser)

    def code(self, raw, browser, phone, ip):
        def resolve(db):
            branch_id, _ = self._branch(db, raw)
            subject = self._subject(branch_id, raw, browser)
            db.execute('DELETE FROM branch_join_proofs WHERE subject=? OR expires<=?', (subject, self.clock()))
            return subject, phone, branch_id
        return self.identity.request_phone_otp(resolve, ip, flow='branch_qr')

    def verify(self, raw, browser, challenge, code):
        with self.product.connect() as db:
            branch_id, _ = self._branch(db, raw)
            subject = self._subject(branch_id, raw, browser)
            otp = self.identity._check_otp(db, subject, challenge, code)
            if otp:
                db.execute('UPDATE identity_otp SET state="used" WHERE id=?', (challenge,))
                db.execute('INSERT OR REPLACE INTO branch_join_proofs(subject,branch_id,phone,expires) VALUES(?,?,?,?)',
                           (subject, branch_id, otp['phone'], self.clock() + 600))
        if not otp:  # Commit failed-attempt accounting.
            raise ValueError('Код неверен или срок его действия истёк.')

    def _proof(self, db, raw, browser):
        branch_id, branch = self._branch(db, raw)
        proof = db.execute('SELECT * FROM branch_join_proofs WHERE subject=? AND branch_id=? AND expires>?',
                           (self._subject(branch_id, raw, browser), branch_id, self.clock())).fetchone()
        if not proof:
            raise ValueError('Подтвердите номер телефона кодом из сообщения.')
        return proof, branch

    @staticmethod
    def _local(db, branch_id, phone, crm_id=None):
        matches = []
        for token, card in read_document(db, 'links.json').items():
            if str(card['branch_id']) != branch_id or card.get('system_phone'):
                continue
            same_phone = normalize_phone(card.get('phone')) == phone
            same_crm = bool(crm_id and str(card.get('id_ycl')) == str(crm_id))
            if same_crm and not same_phone or (same_phone and crm_id and card.get('id_ycl') and not same_crm):
                raise ValueError('Данные клиента изменились. Обратитесь к администратору филиала.')
            if same_phone:
                matches.append((token, card))
        if len(matches) > 1:
            raise ValueError('Найдено несколько карточек. Обратитесь к администратору филиала.')
        return matches[0] if matches else (None, {})

    def profile(self, raw, browser):
        with self.product.connect() as db:
            proof, branch = self._proof(db, raw, browser)
            branch_id, phone = proof['branch_id'], proof['phone']
            uses_crm = branch.get('yclients_enabled', True)
            if uses_crm and not self.directory._availability(db, branch_id)['ready']:
                raise ValueError('Сейчас не удалось проверить клиентскую базу филиала. Повторите позже.')
        crm = {}
        if uses_crm:
            try:
                matches = self.directory.crm.find_clients_by_phone(branch_id, phone)
            except Exception:
                raise ValueError('Сейчас не удалось проверить клиентскую базу филиала. Повторите позже.') from None
            if len(matches) > 1:
                raise ValueError('Найдено несколько карточек. Обратитесь к администратору филиала.')
            crm = matches[0] if matches else {}
        with self.product.connect() as db:
            current, branch = self._proof(db, raw, browser)
            if current['phone'] != phone or bool(branch.get('yclients_enabled', True)) != bool(uses_crm):
                raise ValueError('Данные регистрации изменились. Начните заново.')
            token, local = self._local(db, branch_id, phone, crm.get('id'))
            result = {'name': local.get('user_name') or crm.get('name') or '',
                      'email': local.get('email') or crm.get('email') or '', 'existing': bool(token)}
            snapshot = {'id_ycl': crm.get('id'), 'uses_crm': bool(uses_crm), 'local_token':token}
            db.execute('UPDATE branch_join_proofs SET profile=? WHERE subject=?', (json.dumps(snapshot), current['subject']))
            return result

    def finish(self, raw, browser, name, email):
        with self.links.store.lock, self.product.connect() as db:
            proof, branch = self._proof(db, raw, browser)
            if not proof['profile']:
                raise ValueError('Сначала проверьте данные клиента.')
            snapshot = json.loads(proof['profile'])
            if snapshot['uses_crm'] != bool(branch.get('yclients_enabled', True)):
                raise ValueError('Филиал изменил способ подключения. Проверьте данные ещё раз.')
            crm_id, phone, branch_id = snapshot['id_ycl'], proof['phone'], proof['branch_id']
            token, local = self._local(db, branch_id, phone, crm_id)
            original = proof['client_token'] or snapshot['local_token']
            if original and token != original:
                raise ValueError('Карточка клиента изменилась. Начните регистрацию заново.')
            at = datetime.fromtimestamp(self.clock(), timezone.utc)
            expiry = pass_expiry(branch, at)
            if token:
                if crm_id and not local.get('id_ycl'):
                    cards = read_document(db, 'links.json')
                    cards[token]['id_ycl'] = crm_id
                    write_document(db, 'links.json', cards)
                if not local.get('admin_blocked') and not access_is_active(local, at):
                    self.links.set_expiry_at(token, expiry)
            else:
                if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
                    raise ValueError('Укажите имя (до 200 символов).')
                email = valid_email(email)
                if crm_id:
                    token = self.links.create_link({'branch_id': branch_id, 'user_name': name.strip(), 'email': email,
                            'phone': phone, 'id_ycl': crm_id, 'gate_access': True, 'access_source': 'manual'}, expires_at=expiry)
                else:
                    token = self.links.create_guest(branch, phone, name=name.strip(), email=email, at=at)
            db.execute('UPDATE branch_join_proofs SET client_token=? WHERE subject=?', (token, proof['subject']))
            return {'url': '/opn/' + token}
