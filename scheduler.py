"""Single-owner scheduled read-only reconciliation with per-branch isolation."""
from pathlib import Path
from datetime import timedelta
import hashlib
import json
import threading
import time
from storage import config
from storage.product_store import ProductStore
from branch_manager import BranchManager
from link_manager import LinkManager, access_is_active
from timeutil import now
import logger
from yclients_api import YClientsClient, eligible_clients


class _SchedulerStopping(Exception):
    pass


def _can_sync(branch, *, expected_revision=None):
    return (branch is not None and branch.enabled and branch.yclients_enabled
            and branch.yclients_marketplace_state not in {'frozen', 'uninstalled'}
            and (expected_revision is None or branch.yclients_sync_revision == expected_revision))


class Scheduler:
    def __init__(self, link_manager=None, branch_manager=None, crm=None, event_sink=None):
        self.link_manager = link_manager or LinkManager()
        self.branch_manager = branch_manager or BranchManager()
        self.crm = crm
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._activation_thread = None
        self.last_runs = {}
        self.event_sink = event_sink
        self.store = ProductStore(Path(self.link_manager.links_file).parent / 'application.sqlite3')
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS appointment_schedule(branch_id TEXT PRIMARY KEY, records TEXT NOT NULL, valid_until REAL NOT NULL, fingerprint TEXT, active INTEGER NOT NULL)')

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        if not config.SCHEDULER_ENABLED:
            return
        # The first runtime poll must confirm persisted appointments before the
        # local timer can issue new grants. Existing passes keep their expiry.
        with self.store.connect() as db:
            db.execute('UPDATE appointment_schedule SET active=0')
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='gatebot-scheduler', daemon=True)
        self._activation_thread = threading.Thread(target=self._activation_loop, name='gatebot-access-timer', daemon=True)
        self._thread.start()
        self._activation_thread.start()

    def stop(self):
        self._stop.set()
        for worker in (self._thread, self._activation_thread):
            if worker and worker is not threading.current_thread():
                worker.join(timeout=30)
        return not any(worker and worker.is_alive() for worker in (self._thread, self._activation_thread))

    def _activation_loop(self):
        while not self._stop.is_set():
            try:
                self.activate_due()
            except Exception as error:
                logger.event('ACCESS_TIMER_ERROR', level='ERROR', details={'error_type': type(error).__name__})
            self._stop.wait(1)

    @staticmethod
    def _minimal_records(records):
        """Persist only fields needed for access, never raw CRM response payloads."""
        result = {}
        for record in records:
            item = {k: record[k] for k in ('id', 'company_id', 'datetime', 'date', 'deleted', 'attendance', 'seance_length', 'length') if k in record}
            client = record.get('client')
            item['client'] = {k: client[k] for k in ('id', 'name', 'phone', 'email') if k in client} if isinstance(client, dict) else None
            identity = str(item.get('id'))
            if identity in result and result[identity] != item:
                raise ValueError('Conflicting appointment snapshots')
            result[identity] = item
        return list(result.values())

    def _apply(self, branch, records, at, db, *, force=False):
        eligible = eligible_clients(records, branch.branch_id, at=at, branch_timezone=branch.timezone, lead_seconds=30)
        def fingerprint_now():
            # Time can expire a shorter manual grant even when the CRM snapshot
            # and stored fields are unchanged. Also notice local block/edits.
            local = {token: {**data, '_active_now': access_is_active(data, at)}
                     for token, data in self.link_manager.load_links().items()
                     if str(data.get('branch_id')) == str(branch.branch_id) and not data.get('is_temporary')}
            return hashlib.sha256(json.dumps([eligible, local], sort_keys=True).encode()).hexdigest()
        fingerprint = fingerprint_now()
        previous = db.execute('SELECT fingerprint FROM appointment_schedule WHERE branch_id=?', (str(branch.branch_id),)).fetchone()
        if not force and previous and previous['fingerprint'] == fingerprint:
            return {'eligible': len(eligible), 'changed': 0}
        applied = self.link_manager.reconcile_appointments(branch.branch_id, eligible, at=at, branch_timezone=branch.timezone)
        db.execute('UPDATE appointment_schedule SET fingerprint=? WHERE branch_id=?', (fingerprint_now(), str(branch.branch_id)))
        if applied['changed']:
            logger.event('ACCESS_TIMER_APPLIED', branch_id=branch.branch_id, details=applied)
        return applied

    def activate_due(self):
        """No CRM calls: known deadlines are independent of polling/network latency."""
        if not config.YCLIENTS_ENABLED and self.crm is None:
            return {}
        result = {}
        for branch in self.branch_manager.get_all_branches():
            if self._stop.is_set():
                break
            if not _can_sync(branch):
                continue
            at = now(branch.timezone)
            # Match DocumentStore's lock order; links + schedule + outbox commit
            # in one SQL transaction in the production SQL storage mode.
            try:
                with self.link_manager.store.lock, self.store.connect() as db:
                    if self._stop.is_set():
                        break
                    current=self.branch_manager.get_branch(branch.branch_id)
                    if not _can_sync(current):
                        continue
                    branch=current
                    row = db.execute('SELECT * FROM appointment_schedule WHERE branch_id=? AND active=1', (str(branch.branch_id),)).fetchone()
                    if not row or row['valid_until'] < at.timestamp():
                        continue
                    result[str(branch.branch_id)] = self._apply(branch, json.loads(row['records']), at, db)
                    if self._stop.is_set():
                        raise _SchedulerStopping()
            except _SchedulerStopping:
                break
            except Exception as error:
                result[str(branch.branch_id)] = {'status': 'error', 'error_type': type(error).__name__}
                logger.event('ACCESS_TIMER_BRANCH_ERROR', branch_id=branch.branch_id, level='ERROR',
                             details={'error_type': type(error).__name__})
        return result

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.run_once()
            except Exception as error:
                logger.event('SCHEDULER_ERROR', level='ERROR', details={'error_type': type(error).__name__})
            self._stop.wait(max(1, config.ACCESS_UPD_PERIOD * 60 - (time.monotonic() - started)))

    def _cleanup_job(self):
        revoked = self.link_manager.cleanup_expired_accesses()
        removed = self.link_manager.cleanup_guest_users()
        return {'expired': revoked, 'removed_guests': removed}

    def _access_update_job(self):
        return self.run_once()

    def run_once(self):
        if self._stop.is_set():
            return {'skipped': 'stopping'}
        if not self._lock.acquire(blocking=False):
            return {'skipped': 'already_running'}
        result = {}
        try:
            self._cleanup_job()
            if not config.YCLIENTS_ENABLED and self.crm is None:
                return {'skipped': 'crm_disabled'}
            crm = self.crm or YClientsClient(branches=self.branch_manager)
            for branch in self.branch_manager.get_all_branches():
                if self._stop.is_set():
                    break
                if not branch.enabled:
                    continue
                if not getattr(branch,'yclients_enabled',True):
                    self.last_runs[str(branch.branch_id)] = result[str(branch.branch_id)] = {'status':'skipped','reason':'standalone'}
                    continue
                if branch.yclients_marketplace_state in {'frozen', 'uninstalled'}:
                    self.last_runs[str(branch.branch_id)] = result[str(branch.branch_id)] = {'status':'skipped','reason':'marketplace_disconnected'}
                    continue
                correlation = logger.set_correlation_id()
                try:
                    at = now(branch.timezone)
                    records = self._minimal_records(crm.records_for_day(branch.branch_id, at.date()) +
                                                   crm.records_for_day(branch.branch_id, at.date() + timedelta(days=1)))
                    if self._stop.is_set():
                        break
                    # Validate the entire snapshot, including future records,
                    # before replacing a previously usable schedule.
                    eligible_clients(records, branch.branch_id, at=at, branch_timezone=branch.timezone)
                    with self.link_manager.store.lock, self.store.connect() as db:
                        if self._stop.is_set():
                            break
                        # A branch can be deleted/disabled while the GET is in
                        # flight. Never recreate its schedule or clients from
                        # the stale object captured before that request.
                        current=self.branch_manager.get_branch(branch.branch_id)
                        if not _can_sync(current, expected_revision=branch.yclients_sync_revision):
                            continue
                        branch=current
                        completed = now(branch.timezone)
                        db.execute('INSERT INTO appointment_schedule VALUES(?,?,?,NULL,1) ON CONFLICT(branch_id) DO UPDATE SET records=excluded.records,valid_until=excluded.valid_until,active=1',
                                   (str(branch.branch_id), json.dumps(records, ensure_ascii=False),
                                    completed.timestamp() + max(120, config.ACCESS_UPD_PERIOD * 120)))
                        applied = self._apply(branch, records, completed, db, force=True)
                        if self._stop.is_set():
                            raise _SchedulerStopping()
                    status = {'status': 'success', 'at': at.isoformat(), **applied}
                    logger.event('SCHEDULER_SYNC', branch_id=branch.branch_id, details=applied, correlation=correlation)
                    if self.event_sink and self.last_runs.get(str(branch.branch_id), {}).get('status') == 'error':
                        self.event_sink('crm_recovered', branch.branch_id, {})
                except _SchedulerStopping:
                    break
                except Exception as error:
                    with self.store.connect() as db:
                        current = self.branch_manager.get_branch(branch.branch_id)
                        if not _can_sync(current, expected_revision=branch.yclients_sync_revision):
                            continue
                        db.execute('UPDATE appointment_schedule SET active=0 WHERE branch_id=?', (str(branch.branch_id),))
                    status = {'status': 'error', 'at': now().isoformat(), 'error_type': type(error).__name__}
                    logger.event('SCHEDULER_BRANCH_ERROR', branch_id=branch.branch_id, level='ERROR', details={'error_type': type(error).__name__, 'correlation_id': correlation})
                    if self.event_sink:
                        self.event_sink('crm_error', branch.branch_id, {'message': 'Не удалось обновить записи; действующие пропуска сохранены до своего срока.'})
                self.last_runs[str(branch.branch_id)] = result[str(branch.branch_id)] = status
            return result
        finally:
            self._lock.release()
