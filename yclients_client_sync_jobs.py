"""Durable YC identity reads; only run.py starts this worker.

The document hook writes local queue rows in the client's transaction. All CRM
calls happen after claiming a job and releasing the database transaction.
"""
import hashlib
import json
import re
import threading
import time
import uuid

from storage.sql_documents import read_document


OUTCOMES = {'updated', 'unchanged', 'not_found', 'ambiguous', 'conflict', 'unavailable', 'stale', 'error'}


def setup_sync_tables(db):
    """Shared by runtime composition and writers such as the console admin CLI."""
    db.execute('CREATE TABLE IF NOT EXISTS yclients_client_sync_batches('
               'id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,actor TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS yclients_client_sync_jobs('
               'id TEXT PRIMARY KEY,branch_id TEXT NOT NULL,client_token TEXT NOT NULL,phone TEXT NOT NULL,expected_id TEXT,'
               'actor TEXT,batch_id TEXT REFERENCES yclients_client_sync_batches(id) ON DELETE CASCADE,'
               'state TEXT NOT NULL,outcome TEXT,attempts INTEGER NOT NULL DEFAULT 0,available_at REAL NOT NULL,'
               'generation INTEGER NOT NULL DEFAULT 1,claim TEXT,created REAL NOT NULL,updated REAL NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS yclients_client_sync_due ON yclients_client_sync_jobs(state,available_at)')
    db.execute('CREATE INDEX IF NOT EXISTS yclients_client_sync_batch ON yclients_client_sync_jobs(batch_id,state)')


def enqueue_client_changes(db, links, previous):
    """Pure SQL hook: no credentials, requests, thread creation or callbacks."""
    setup_sync_tables(db)
    from link_manager import normalize_phone
    branches = read_document(db, 'branches.json')
    stamp = time.time()
    for token in set(previous) - set(links):
        db.execute('DELETE FROM yclients_client_sync_jobs WHERE client_token=? AND batch_id IS NULL', (token,))
        # Keep a terminal batch item so a deleted client cannot stall progress.
        db.execute('UPDATE yclients_client_sync_jobs SET state="done",outcome="stale",claim=NULL,updated=? '
                   'WHERE client_token=? AND batch_id IS NOT NULL AND state!="done"', (stamp, token))
    for token, client in links.items():
        if not isinstance(client, dict):
            continue
        branch_id = str(client.get('branch_id'))
        branch = branches.get(branch_id)
        if not branch or branch.get('yclients_enabled', True) is False or client.get('id_ycl') not in (None, ''):
            continue
        phone = normalize_phone(client.get('phone'))
        old = previous.get(token)
        changed = old is None or normalize_phone(old.get('phone')) != phone or str(old.get('branch_id')) != branch_id
        if (not isinstance(client.get('phone'), str) or
                not re.fullmatch(r'[+0-9\s().-]{7,64}', client['phone']) or
                not re.fullmatch(r'\+[1-9][0-9]{6,14}', phone) or not changed):
            continue
        key = 'auto:' + hashlib.sha256(token.encode()).hexdigest()
        db.execute('INSERT INTO yclients_client_sync_jobs '
                   '(id,branch_id,client_token,phone,expected_id,actor,batch_id,state,outcome,attempts,available_at,generation,claim,created,updated) '
                   'VALUES(?,?,?,?,?,NULL,NULL,"queued",NULL,0,?,1,NULL,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET branch_id=excluded.branch_id,phone=excluded.phone,expected_id=excluded.expected_id,'
                   'state="queued",outcome=NULL,attempts=0,available_at=excluded.available_at,generation=generation+1,claim=NULL,updated=excluded.updated',
                   (key, branch_id, token, phone, json.dumps(client.get('id_ycl')), stamp, stamp, stamp))


class YClientsClientSyncJobs:
    def __init__(self, identity, sync, *, clock=None):
        self.identity, self.sync = identity, sync
        self.store = identity.product
        self.clock = clock or time.time
        self._stop = threading.Event()
        self._process_lock = threading.Lock()
        self._thread = None
        with self.store.connect() as db:
            setup_sync_tables(db)

    def _scope(self, db, username, branch_id):
        self.identity._scope(db, username, branch_id)
        branch = read_document(db, 'branches.json')[str(branch_id)]
        if branch.get('yclients_enabled', True) is False:
            raise PermissionError('Этот филиал работает без YCLIENTS')

    def start_batch(self, username, branch_id):
        from link_manager import normalize_phone
        branch_id = str(branch_id)
        with self.store.connect() as db:
            self._scope(db, username, branch_id)
            ready = self.sync.readiness(username, branch_id)
            if not ready['ready']:
                raise ValueError(ready['message'])
            existing = db.execute('SELECT id FROM yclients_client_sync_batches WHERE branch_id=? '
                                  'AND state IN ("queued","running") ORDER BY created DESC LIMIT 1', (branch_id,)).fetchone()
            if existing:
                status = self._status(db, existing['id'], ready)
                if status['state'] in {'queued', 'running'}:
                    return status
            clients = {token: client for token, client in read_document(db, 'links.json').items()
                       if str(client.get('branch_id')) == branch_id}
            identifier, stamp = uuid.uuid4().hex, self.clock()
            db.execute('INSERT INTO yclients_client_sync_batches VALUES(?,?,?,?,?,?)',
                       (identifier, branch_id, username, 'queued' if clients else 'complete', stamp, stamp))
            for token, client in clients.items():
                # The explicit batch owns pending work for these same snapshots.
                # A claimed automatic request is reconciled after it finishes.
                db.execute('DELETE FROM yclients_client_sync_jobs WHERE client_token=? AND batch_id IS NULL '
                           'AND state IN ("queued","retry")', (token,))
                db.execute('INSERT INTO yclients_client_sync_jobs VALUES(?,?,?,?,?,?,?,"queued",NULL,0,?,1,NULL,?,?)',
                           (uuid.uuid4().hex, branch_id, token, normalize_phone(client.get('phone')),
                            json.dumps(client.get('id_ycl')), username, identifier, stamp, stamp, stamp))
            return self._status(db, identifier, ready)

    def batch_status(self, username, branch_id):
        with self.store.connect() as db:
            self._scope(db, username, branch_id)
            ready = self.sync.readiness(username, str(branch_id))
            latest = db.execute('SELECT id FROM yclients_client_sync_batches WHERE branch_id=? ORDER BY created DESC,id DESC LIMIT 1', (str(branch_id),)).fetchone()
            return self._status(db, latest['id'] if latest else None, ready)

    def _status(self, db, identifier, ready):
        result = dict(id=identifier, state='idle', total=0, processed=0, updated=0, unchanged=0,
                      not_found=0, ambiguous=0, conflicts=0, errors=0, can_start=bool(ready['ready']))
        if identifier:
            batch = db.execute('SELECT state FROM yclients_client_sync_batches WHERE id=?', (identifier,)).fetchone()
            if batch:
                result['state'] = batch['state']
                rows = db.execute('SELECT state,outcome,count(*) AS count FROM yclients_client_sync_jobs WHERE batch_id=? GROUP BY state,outcome', (identifier,)).fetchall()
                for row in rows:
                    result['total'] += row['count']
                    if row['state'] != 'done':
                        continue
                    result['processed'] += row['count']
                    key = 'conflicts' if row['outcome'] == 'conflict' else row['outcome']
                    if key not in {'updated', 'unchanged', 'not_found', 'ambiguous', 'conflicts'}:
                        key = 'errors'
                    result[key] += row['count']
                if result['processed'] == result['total'] and result['state'] != 'failed':
                    result['state'] = 'complete'
                    db.execute('UPDATE yclients_client_sync_batches SET state="complete",updated=? WHERE id=?', (self.clock(), identifier))
                elif result['processed'] or any(row['state'] == 'running' for row in rows):
                    result['state'] = 'running' if result['state'] != 'failed' else 'failed'
        messages = {'idle': 'Обновление пока не запускалось.', 'queued': 'Проверка поставлена в очередь.',
                    'running': 'Обновляем ID клиентов по данным YCLIENTS.', 'complete': 'Обновление завершено.',
                    'failed': 'Обновление остановлено: права администратора или филиал изменились.'}
        result['message'] = ready['message'] if not ready['ready'] else messages[result['state']]
        if result['state'] == 'complete' and result['errors'] and ready['ready']:
            result['message'] = 'Проверка завершена с ошибками. После устранения причины можно запустить её снова.'
        return result

    def _claim(self):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM yclients_client_sync_jobs WHERE state IN ("queued","retry") AND available_at<=? '
                             'ORDER BY CASE WHEN batch_id IS NULL THEN 0 ELSE 1 END,created,id LIMIT 1', (self.clock(),)).fetchone()
            if not row:
                return None
            item = dict(row); item['claim'] = uuid.uuid4().hex; item['attempts'] += 1
            db.execute('UPDATE yclients_client_sync_jobs SET state="running",claim=?,attempts=?,updated=? WHERE id=?',
                       (item['claim'], item['attempts'], self.clock(), item['id']))
            if item['batch_id']:
                db.execute('UPDATE yclients_client_sync_batches SET state="running",updated=? WHERE id=? AND state!="failed"', (self.clock(), item['batch_id']))
            return item

    def process_once(self, limit=5):
        if self.store.current_connection() is not None:
            raise RuntimeError('YC identity work must run outside a database transaction')
        if not self._process_lock.acquire(blocking=False):
            return 0
        processed = 0
        try:
            for _ in range(max(0, min(int(limit), 100))):
                if self._stop.is_set():
                    break
                item = self._claim()
                if not item:
                    break
                invalid_actor = False
                try:
                    if item['actor']:
                        with self.store.connect() as db:
                            self._scope(db, item['actor'], item['branch_id'])
                    result = self.sync.refresh_snapshot(item['branch_id'], item['client_token'], item['phone'],
                                                        json.loads(item['expected_id']), actor=item['actor'])
                    outcome = result.get('status')
                    if outcome not in OUTCOMES:
                        outcome = 'error'
                except (PermissionError, ValueError):
                    outcome = 'error'; invalid_actor = bool(item['actor'])
                except Exception:
                    outcome = 'error'
                with self.store.connect() as db:
                    current = db.execute('SELECT claim,generation FROM yclients_client_sync_jobs WHERE id=?', (item['id'],)).fetchone()
                    if not current or current['claim'] != item['claim'] or current['generation'] != item['generation']:
                        continue
                    retry = item['batch_id'] is None and outcome in {'unavailable', 'error'}
                    delay = min(300, 60 * (2 ** min(item['attempts'] - 1, 3))) if retry else 0
                    db.execute('UPDATE yclients_client_sync_jobs SET state=?,outcome=?,available_at=?,claim=NULL,updated=? WHERE id=?',
                               ('retry' if retry else 'done', outcome, self.clock() + delay, self.clock(), item['id']))
                    if item['batch_id'] is None and outcome in {'updated', 'unchanged'}:
                        self._advance_waiting_snapshots(db, item, result)
                    if item['batch_id']:
                        if invalid_actor:
                            db.execute('UPDATE yclients_client_sync_jobs SET state="done",outcome="error",claim=NULL,updated=? WHERE batch_id=? AND state!="done"', (self.clock(), item['batch_id']))
                            db.execute('UPDATE yclients_client_sync_batches SET state="failed",updated=? WHERE id=?', (self.clock(), item['batch_id']))
                        else:
                            self._status(db, item['batch_id'], {'ready': True, 'message': ''})
                processed += 1
            return processed
        finally:
            self._process_lock.release()

    def _advance_waiting_snapshots(self, db, item, result):
        """Only this successful claim may advance an overlapping batch's CAS."""
        from link_manager import normalize_phone
        client = read_document(db, 'links.json').get(item['client_token'])
        identifier = result.get('id_ycl')
        if (not client or str(client.get('branch_id')) != item['branch_id'] or
                normalize_phone(client.get('phone')) != item['phone'] or
                str(client.get('id_ycl')) != str(identifier) or
                not re.fullmatch(r'[1-9][0-9]{0,15}', str(identifier))):
            return
        rows = db.execute('SELECT id,expected_id FROM yclients_client_sync_jobs WHERE client_token=? AND branch_id=? '
                          'AND phone=? AND batch_id IS NOT NULL AND state="queued"',
                          (item['client_token'], item['branch_id'], item['phone'])).fetchall()
        old_id = json.loads(item['expected_id'])
        for row in rows:
            expected_id = json.loads(row['expected_id'])
            if str(expected_id or '') == str(old_id or ''):
                db.execute('UPDATE yclients_client_sync_jobs SET expected_id=?,updated=? WHERE id=?',
                           (json.dumps(identifier), self.clock(), row['id']))

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # Only the sole runtime owner may recover claims from its predecessor.
        with self.store.connect() as db:
            db.execute('UPDATE yclients_client_sync_jobs SET state="queued",claim=NULL,available_at=?,updated=? WHERE state="running"', (self.clock(), self.clock()))
        self._thread = threading.Thread(target=self._run, name='gatebot-yc-client-ids', daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception as error:
                import logger
                logger.event('YCLIENTS_CLIENT_SYNC_WORKER_ERROR', level='ERROR', details={'error_type': type(error).__name__})
            self._stop.wait(1)

    def stop(self, timeout=35):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)
        return not self._thread or not self._thread.is_alive()
