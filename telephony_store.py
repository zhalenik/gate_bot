"""Private call journal and SMS conversations; no payloads in the public audit."""
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from link_manager import normalize_phone
from sms_pdu import SMSPart, assemble_messages
from storage.json_store import JsonStore


def stamp():
    return datetime.now(timezone.utc).isoformat()


def instant(value):
    try:
        value = datetime.fromisoformat(value)
        return value.replace(tzinfo=timezone.utc).timestamp() if value.tzinfo is None else value.timestamp()
    except (ValueError, TypeError):
        return 0.0


def chat_key(phone):
    phone = str(phone or '')
    canonical = normalize_phone(phone) if phone and all(c in '+0123456789 ()-' for c in phone) else ''
    return hashlib.sha256(('sms:' + (canonical or phone.casefold())).encode()).hexdigest()


def part_key(part):
    data = asdict(part)
    data.pop('storage_index')
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def upgrade(state):
    records = state.setdefault('records', {})
    requests = state.setdefault('requests', {})
    maintenance = state.setdefault('maintenance', {})
    for key, record in list(records.items()):
        if record.get('direction') == 'outgoing':
            requests.setdefault(key, {'status': record.get('status', 'unknown')})
        if record.get('action') == 'sms_delete':
            maintenance[key] = records.pop(key)
    state['version'] = 3
    # v2 incorrectly discarded unseen delayed deliveries using these cutoffs.
    state.pop('sms_cleared_at', None)
    state.pop('cleared_chats', None)
    return state


class TelephonyStore:
    def __init__(self, path=None):
        if path is None:
            from storage import config
            path = Path(config.DATA_DIR) / 'telephony.json'
        self.store = JsonStore(path)

    def snapshot(self):
        with self.store.transaction() as state:
            upgrade(state)
            if state.get('assembly_version') != 2:
                self._assemble(state)
            return json.loads(json.dumps(state))

    def add(self, kind, direction, **fields):
        record = {'id': uuid.uuid4().hex, 'kind': kind, 'direction': direction,
                  'timestamp': stamp(), 'read': False, **fields}
        with self.store.transaction() as state:
            upgrade(state)['records'][record['id']] = record
        return record['id']

    def update(self, record_id, **fields):
        with self.store.transaction() as state:
            upgrade(state)
            if record_id in state['requests'] and 'status' in fields:
                state['requests'][record_id]['status'] = fields['status']
            record = state['records'].get(record_id) or state['maintenance'].get(record_id)
            if record is None:
                return False
            record.update(fields)
        return True

    def _assemble(self, state):
        saved = state.setdefault('sms_parts', {})
        locations = state.setdefault('sms_locations', {})
        decoded = [SMSPart(**{**p, 'timestamp': datetime.fromisoformat(p['timestamp']),
                             'payload': bytes.fromhex(p['payload']),
                             'extra_headers': bytes.fromhex(p['extra_headers'])}) for p in saved.values()]
        records = state['records']
        claimed = set()
        for message in assemble_messages(decoded):
            keys = {part_key(p) for p in message.parts}
            matches = [r for r in records.values() if r['kind'] == 'sms' and
                r['direction'] == 'incoming' and r['id'] not in claimed and keys.intersection(r.get('part_keys', []))]
            existing = matches[0] if matches else None
            record_id = existing['id'] if existing else uuid.uuid4().hex
            claimed.add(record_id)
            for duplicate in matches[1:]:
                records.pop(duplicate['id'], None)
            records[record_id] = {**(existing or {}), 'id': record_id, 'kind': 'sms',
                'direction': 'incoming', 'read': existing.get('read', False) if existing and keys == set(existing.get('part_keys', [])) else False,
                'received_at': existing.get('received_at', stamp()) if existing else stamp(),
                'phone': message.sender, 'status': 'received' if message.complete else 'incomplete',
                **message.as_dict(), 'part_keys': sorted(keys),
                'sim_locations': [location for key in sorted(keys) for location in locations.get(key, [])]}
            for row, part in zip(records[record_id]['parts'], message.parts):
                observation = state.get('sms_observations', {}).get(part_key(part), {})
                row.update(smsc_at=part.timestamp.isoformat(), **observation)
        state['assembly_version'] = 2

    def ingest(self, parts, storage='SM', *, observed_at=None):
        receipts = []
        with self.store.transaction() as state:
            upgrade(state)
            saved = state.setdefault('sms_parts', {})
            locations = state.setdefault('sms_locations', {})
            deleted = state.get('deleted_parts', {})
            changed = False
            for part in parts:
                key = part_key(part)
                location = {'storage': storage, 'index': part.storage_index, 'part_key': key}
                if part.storage_index is not None:
                    receipts.append(location)
                if key in deleted:
                    continue  # Deduplicate only known, explicitly deleted parts.
                # SMSC time is not local arrival time. A delayed, unseen message
                # must survive clearing a chat, even when it was sent days ago.
                if part.storage_index is not None and location not in locations.get(key, []):
                    locations.setdefault(key, []).append(location)
                    changed = True
                if key not in saved:
                    data = asdict(part)
                    data.update(timestamp=part.timestamp.isoformat(), payload=part.payload.hex(),
                                extra_headers=part.extra_headers.hex())
                    saved[key] = data
                    state.setdefault('sms_observations', {})[key] = {
                        'received_at': (observed_at or {}).get(part.storage_index) or stamp(),
                        'archived_at': stamp()}
                    changed = True
            if changed:
                self._assemble(state)
        # Transaction fsync+replace completed before authorizing any SIM deletion.
        return receipts

    def acknowledge_sim(self, location):
        with self.store.transaction() as state:
            upgrade(state)
            for key, entries in state.get('sms_locations', {}).items():
                state['sms_locations'][key] = [p for p in entries if not
                    (p['storage'] == location['storage'] and p['index'] == location['index'])]
            for record in state['records'].values():
                if record['kind'] == 'sms' and record['direction'] == 'incoming':
                    record['sim_locations'] = [p for p in record.get('sim_locations', []) if not
                        (p['storage'] == location['storage'] and p['index'] == location['index'])]
            # Text and sms_parts remain unchanged, even if a late part arrives tomorrow.

    def list_records(self, page=1, kind='all', direction='all', search='', contacts=None):
        rows = self.snapshot()['records'].values()
        rows = [r for r in rows if (kind == 'all' or r['kind'] == kind) and
                (direction == 'all' or r['direction'] == direction) and
                (not search or search.casefold() in (str(r.get('phone', '')) + ' ' + str(r.get('text', '')) + ' ' + (contacts or {}).get(chat_key(r.get('phone')), '')).casefold())]
        rows.sort(key=lambda r: instant(r['timestamp']), reverse=True)
        rows = [{**r, 'contact_name': (contacts or {}).get(chat_key(r.get('phone')), '')} for r in rows]
        return {'records': rows[(page-1)*50:page*50], 'total': len(rows), 'page': page}

    def chats(self, search='', page=1, contacts=None):
        grouped = {}
        for record in self.snapshot()['records'].values():
            if record['kind'] != 'sms':
                continue
            key = chat_key(record.get('phone'))
            grouped.setdefault(key, []).append(record)
        chats = []
        for key, messages in grouped.items():
            messages.sort(key=lambda r: instant(r['timestamp']), reverse=True)
            name = (contacts or {}).get(key, '')
            if search and not any(search.casefold() in (name + ' ' + str(r.get('phone', '')) + ' ' + str(r.get('text') or '')).casefold() for r in messages):
                continue
            latest = messages[0]
            chats.append({'id': key, 'phone': latest.get('phone'), 'contact_name': name, 'timestamp': latest['timestamp'],
                'preview': latest.get('text') or 'Сообщение получено не полностью',
                'status': latest.get('status'), 'direction': latest['direction'],
                'count': len(messages), 'unread': sum(not r.get('read') and r['direction'] == 'incoming' for r in messages)})
        chats.sort(key=lambda r: instant(r['timestamp']), reverse=True)
        return {'chats': chats[(page-1)*50:page*50], 'total': len(chats), 'page': page}

    def messages(self, key, page=1, contacts=None):
        rows = [r for r in self.snapshot()['records'].values() if r['kind'] == 'sms' and chat_key(r.get('phone')) == key]
        rows.sort(key=lambda r: instant(r['timestamp']), reverse=True)
        return {'messages': rows[(page-1)*50:page*50], 'total': len(rows), 'page': page,
                'contact_name': (contacts or {}).get(key, '')}

    @staticmethod
    def _remove_sms(state, record):
        for part in record.get('part_keys', []):
            state.setdefault('deleted_parts', {})[part] = True
            for field in ('sms_parts', 'sms_locations', 'sms_observations'):
                state.get(field, {}).pop(part, None)
        state['records'].pop(record['id'], None)

    def delete_message(self, record_id):
        with self.store.transaction() as state:
            upgrade(state)
            if state.get('assembly_version') != 2:
                self._assemble(state)
            record = state['records'].get(record_id)
            if not record or record['kind'] != 'sms':
                return False
            if record.get('status') in {'queued', 'processing'}:
                raise ValueError('Дождитесь завершения отправки')
            self._remove_sms(state, record)
        return True

    def read_chat(self, key):
        with self.store.transaction() as state:
            upgrade(state)
            for record in state['records'].values():
                if record['kind'] == 'sms' and chat_key(record.get('phone')) == key:
                    record['read'] = True

    def clear_sms(self, key=None):
        with self.store.transaction() as state:
            upgrade(state)
            if state.get('assembly_version') != 2:
                self._assemble(state)
            records = state['records']
            selected = [r for r in records.values() if r['kind'] == 'sms' and (key is None or chat_key(r.get('phone')) == key)]
            if any(r.get('status') in {'queued', 'processing'} for r in selected):
                raise ValueError('Дождитесь завершения отправки перед очисткой переписки')
            for record in selected:
                self._remove_sms(state, record)
            # Also remove orphaned decoded parts from legacy snapshots. Otherwise
            # _assemble() could resurrect a cleared message on the next poll.
            for part, value in list(state.get('sms_parts', {}).items()):
                if key is None or chat_key(value.get('sender')) == key:
                    state.setdefault('deleted_parts', {})[part] = True
                    state['sms_parts'].pop(part, None)
                    state.get('sms_locations', {}).pop(part, None)
                    state.get('sms_observations', {}).pop(part, None)
            for record_id, record in list(state['maintenance'].items()):
                if key is None or chat_key(record.get('phone')) == key:
                    state['maintenance'].pop(record_id, None)
        return len(selected)

    def import_snapshot(self, path):
        source = JsonStore(path).read()
        with self.store.transaction() as state:
            upgrade(state)
            for name in ('records', 'sms_parts', 'sms_locations'):
                for key, value in source.get(name, {}).items():
                    state.setdefault(name, {}).setdefault(key, value)
            upgrade(state)
            self._assemble(state)

    def recover_outgoing(self):
        with self.store.transaction() as state:
            upgrade(state)
            for record in list(state['records'].values()) + list(state['maintenance'].values()):
                if record['direction'] == 'outgoing' and record.get('status') in {'queued', 'processing'}:
                    record.update(status='unknown', code='runtime_interrupted')
                    state['requests'].setdefault(record['id'], {})['status'] = 'unknown'
                if record['direction'] == 'incoming' and record['kind'] == 'call' and record.get('status') == 'ringing':
                    record.update(status='unknown', code='runtime_interrupted')
