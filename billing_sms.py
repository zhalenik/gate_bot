"""Branch SMS quota: reserve parts before sending and settle a durable outcome."""
from contextlib import nullcontext
from datetime import datetime, timezone
import math
import re
import sqlite3


SCHEMA = '''
CREATE TABLE IF NOT EXISTS sms_billing_commands(
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, period_id TEXT NOT NULL REFERENCES billing_periods(id),
 phone TEXT NOT NULL, client_token TEXT, client_name TEXT NOT NULL DEFAULT '', purpose TEXT NOT NULL,
 parts INTEGER NOT NULL CHECK(parts>0), status TEXT NOT NULL, charged INTEGER NOT NULL DEFAULT 0,
 created REAL NOT NULL, finished REAL);
CREATE INDEX IF NOT EXISTS sms_billing_branch_time ON sms_billing_commands(branch_id,created);
CREATE TABLE IF NOT EXISTS sms_quota_ledger(
 id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, period_id TEXT NOT NULL REFERENCES billing_periods(id),
 message_id TEXT, entry_type TEXT NOT NULL, reserved_delta INTEGER NOT NULL DEFAULT 0,
 spent_delta INTEGER NOT NULL DEFAULT 0, credit_delta INTEGER NOT NULL DEFAULT 0,
 happened_at REAL NOT NULL, actor TEXT, reason TEXT, UNIQUE(message_id,entry_type));
CREATE TRIGGER IF NOT EXISTS sms_quota_no_update BEFORE UPDATE ON sms_quota_ledger BEGIN SELECT RAISE(ABORT,'immutable SMS quota ledger'); END;
CREATE TRIGGER IF NOT EXISTS sms_quota_no_delete BEFORE DELETE ON sms_quota_ledger BEGIN SELECT RAISE(ABORT,'immutable SMS quota ledger'); END;
'''


def ensure_schema(db):
    statement = ''
    for line in SCHEMA.splitlines():
        statement += line + '\n'
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ''


def _epoch(value=None):
    if value is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('Укажите часовой пояс')
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Некорректное время')
    return float(value)


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def _identifier(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('Некорректный идентификатор')
    value = str(value)
    if not value or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError('Некорректный идентификатор')
    return value


class SmsBilling:
    def __init__(self, billing):
        self.billing, self.store = billing, billing.store

    @staticmethod
    def usage(db, period_id):
        row = db.execute('SELECT COALESCE(SUM(reserved_delta),0) reserved, '
            'COALESCE(SUM(spent_delta),0) spent, COALESCE(SUM(credit_delta),0) credits '
            'FROM sms_quota_ledger WHERE period_id=?', (period_id,)).fetchone()
        return dict(row)

    @staticmethod
    def add_credit(db, branch_id, period_id, amount, *, actor, reason, at, credit_id):
        if type(amount) is not int or not 1 <= amount <= 10000000:
            raise ValueError('Количество SMS должно быть положительным целым числом')
        period=db.execute('SELECT branch_id FROM billing_periods WHERE id=?',(period_id,)).fetchone()
        if not period or str(period['branch_id'])!=str(branch_id):
            raise ValueError('Период не принадлежит филиалу')
        credit_id = _identifier(credit_id)
        existing = db.execute('SELECT branch_id,period_id,credit_delta FROM sms_quota_ledger WHERE id=?', (credit_id,)).fetchone()
        if existing:
            if tuple(existing) != (str(branch_id), period_id, amount):
                raise ValueError('Операция уже использована для другого пакета')
            return False
        db.execute('INSERT INTO sms_quota_ledger VALUES(?,?,?,?,?,0,0,?,?,?,?)',
            (credit_id, str(branch_id), period_id, None, 'credit', amount, _epoch(at), actor, reason))
        return True

    def reserve_sms(self, branch_id, message_id, parts, *, phone, client_token=None, purpose=None, at=None, db=None):
        branch, identifier, current = _identifier(branch_id), _identifier(message_id), _epoch(at)
        if type(parts) is not int or not 1 <= parts <= 255 or not isinstance(phone, str) or not re.fullmatch(r'\+[1-9][0-9]{6,14}', phone):
            raise ValueError('Некорректные параметры SMS')
        with nullcontext(db) if db is not None else self.store.connect() as tx:
            previous = tx.execute('SELECT * FROM sms_billing_commands WHERE id=?', (identifier,)).fetchone()
            if previous:
                if ((previous['branch_id'], previous['phone'], previous['parts']) != (branch, phone, parts) or
                        (client_token is not None and previous['client_token']!=client_token)):
                    raise ValueError('Идентификатор отправки уже использован')
                return {'allowed': previous['finished'] is None, 'duplicate': True, 'reason': 'sms_already_reserved',
                        'period_id': previous['period_id'], 'message_id': identifier}
            view = self.billing.subscription_view(branch, at=current, db=tx)
            if view['status'] not in {'active', 'trial', 'legacy'} or not view.get('period_id'):
                return {'allowed': False, 'reason': 'subscription_' + view['status']}
            sms = view['sms']
            if sms['remaining'] is not None and sms['remaining'] < parts:
                return {'allowed': False, 'reason': 'sms_quota_exhausted', 'required': parts, 'remaining': sms['remaining']}
            from storage.sql_documents import read_document
            from link_manager import normalize_phone
            links = read_document(tx, 'links.json') if tx.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'").fetchone() else {}
            client = links.get(client_token) if client_token else None
            if client_token and (not client or str(client.get('branch_id')) != branch or normalize_phone(client.get('phone')) != phone):
                return {'allowed': False, 'reason': 'sms_recipient_changed'}
            if client is None:
                matches = [(token, row) for token, row in links.items()
                    if str(row.get('branch_id')) == branch and normalize_phone(row.get('phone')) == phone]
                if len(matches) == 1:
                    client_token, client = matches[0]
            tx.execute('INSERT INTO sms_billing_commands VALUES(?,?,?,?,?,?,?,?,?,0,?,NULL)',
                (identifier, branch, view['period_id'], phone, client_token,
                 str((client or {}).get('user_name') or '')[:200], str(purpose or 'notification')[:100], parts, 'reserved', current))
            tx.execute('INSERT INTO sms_quota_ledger VALUES(?,?,?,?,?,?,0,0,?,?,?)',
                (identifier + ':reserve', branch, view['period_id'], identifier, 'reserve', parts, current, None, None))
            return {'allowed': True, 'duplicate': False, 'period_id': view['period_id'], 'message_id': identifier, 'parts': parts}

    def authorize_sms(self, message_id, *, at=None):
        """Recheck a queued reservation immediately before its first modem part."""
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM sms_billing_commands WHERE id=?',(str(message_id),)).fetchone()
            if not row or row['finished'] is not None:
                return False
            view=self.billing.subscription_view(row['branch_id'],at=at,db=db)
            if view['status'] not in {'active','trial','legacy'} or view['period_id']!=row['period_id']:
                return False
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'").fetchone():
                from storage.sql_documents import read_document
                from link_manager import normalize_phone
                if row['branch_id'] not in read_document(db,'branches.json'):
                    return False
                if row['client_token']:
                    client=read_document(db,'links.json').get(row['client_token'])
                    if (not client or str(client.get('branch_id'))!=row['branch_id'] or
                            normalize_phone(client.get('phone'))!=row['phone']):
                        return False
            return True

    def finish_sms(self, message_id, outcome, *, segments=None, at=None, db=None):
        if outcome in {'queued', 'processing', 'reserved', 'sending'}:
            return False
        if outcome not in {'network_accepted', 'accepted', 'partial', 'unknown', 'failed', 'error', 'expired', 'cancelled', 'simulated'}:
            outcome = 'unknown'
        with nullcontext(db) if db is not None else self.store.connect() as tx:
            row = tx.execute('SELECT * FROM sms_billing_commands WHERE id=?', (str(message_id),)).fetchone()
            if not row or row['finished'] is not None:
                return False
            # A part durably marked submitting may have crossed CTRL-Z before
            # interruption. It is charged conservatively and never resubmitted.
            valid = isinstance(segments, list) and len(segments)==row['parts']
            statuses = {}
            if valid:
                for part in segments:
                    sequence = part.get('sequence') if isinstance(part, dict) else None
                    if type(sequence) is not int or not 1 <= sequence <= row['parts'] or sequence in statuses:
                        valid = False
                        break
                    statuses[sequence] = part.get('status')
            if outcome in {'accepted', 'network_accepted'}:
                charged = row['parts']
            elif valid:
                charged = sum(status in {'network_accepted', 'accepted', 'submitting', 'unknown'} for status in statuses.values())
                if any(status not in {'network_accepted', 'accepted', 'submitting', 'unknown', 'failed', 'pending', 'cancelled', 'expired'} for status in statuses.values()):
                    charged = row['parts'] if outcome in {'partial', 'unknown'} else charged
            else:
                charged = row['parts'] if outcome in {'partial', 'unknown'} else 0
            current = _epoch(at)
            tx.execute('INSERT INTO sms_quota_ledger VALUES(?,?,?,?,?,?,?,0,?,?,?)',
                (row['id'] + ':settle', row['branch_id'], row['period_id'], row['id'], 'settle', -row['parts'], charged,
                 current, None, outcome))
            tx.execute('UPDATE sms_billing_commands SET status=?,charged=?,finished=? WHERE id=?',
                (outcome, charged, current, row['id']))
            return True

    def pending(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute('SELECT id,branch_id,parts FROM sms_billing_commands WHERE finished IS NULL')]

    def recover(self, records=None):
        records = records or {}
        for row in self.pending():
            record = records.get(row['id'], {})
            status = record.get('status', 'unknown')
            self.finish_sms(row['id'], 'unknown' if status in {'queued', 'processing', 'reserved'} else status,
                            segments=record.get('segments'))

    def details(self, branch_ids, from_at=None, to_at=None, page=1, page_size=50):
        if not isinstance(branch_ids, (list, tuple, set)):
            raise ValueError('Выберите филиалы')
        branches = sorted({_identifier(branch) for branch in branch_ids})
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 200:
            raise ValueError('Некорректная страница')
        if not branches:
            return {'items': [], 'total': 0, 'page': page, 'page_size': page_size}
        filters = ['branch_id IN (' + ','.join('?' for _ in branches) + ')']
        values = list(branches)
        if from_at is not None:
            filters.append('created>=?'); values.append(_epoch(from_at))
        if to_at is not None:
            filters.append('created<?'); values.append(_epoch(to_at))
        if from_at is not None and to_at is not None and _epoch(from_at) >= _epoch(to_at):
            raise ValueError('Начало периода должно быть раньше окончания')
        where = ' AND '.join(filters)
        with self.store.connect() as db:
            total = db.execute('SELECT count(*) FROM sms_billing_commands WHERE ' + where, values).fetchone()[0]
            rows = db.execute('SELECT * FROM sms_billing_commands WHERE ' + where + ' ORDER BY created DESC,id DESC LIMIT ? OFFSET ?',
                              values + [page_size, (page - 1) * page_size]).fetchall()
            items = [{**dict(row), 'created': _iso(row['created']), 'finished': _iso(row['finished'])} for row in rows]
            return {'items': items, 'total': total, 'page': page, 'page_size': page_size}
