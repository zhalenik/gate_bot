"""Single-owner scheduled read-only reconciliation with per-branch isolation."""
from pathlib import Path
import threading
from storage import config
from branch_manager import BranchManager
from link_manager import LinkManager
from timeutil import now
import logger
from yclients_api import YClientsClient, eligible_clients


class Scheduler:
    def __init__(self, link_manager=None, branch_manager=None, crm=None):
        self.link_manager = link_manager or LinkManager()
        self.branch_manager = branch_manager or BranchManager()
        self.crm = crm
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self.last_runs = {}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        if not config.SCHEDULER_ENABLED:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='gatebot-scheduler', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=30)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:
                logger.event('SCHEDULER_ERROR', level='ERROR', details={'error_type': type(error).__name__})
            self._stop.wait(config.ACCESS_UPD_PERIOD * 60)

    def _cleanup_job(self):
        revoked = self.link_manager.cleanup_expired_accesses()
        removed = self.link_manager.cleanup_guest_users()
        return {'expired': revoked, 'removed_guests': removed}

    def _access_update_job(self):
        return self.run_once()

    def run_once(self):
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
                correlation = logger.set_correlation_id()
                try:
                    at = now(branch.timezone)
                    records = crm.records_for_day(branch.branch_id, at.date())
                    eligible = eligible_clients(records, branch.branch_id, at=at, branch_timezone=branch.timezone)
                    applied = self.link_manager.reconcile_appointments(branch.branch_id, eligible, at=at, branch_timezone=branch.timezone)
                    status = {'status': 'success', 'at': at.isoformat(), **applied}
                    logger.event('SCHEDULER_SYNC', branch_id=branch.branch_id, details=applied, correlation=correlation)
                except Exception as error:
                    status = {'status': 'error', 'at': now().isoformat(), 'error_type': type(error).__name__}
                    logger.event('SCHEDULER_BRANCH_ERROR', branch_id=branch.branch_id, level='ERROR', details={'error_type': type(error).__name__, 'correlation_id': correlation})
                self.last_runs[str(branch.branch_id)] = result[str(branch.branch_id)] = status
            return result
        finally:
            self._lock.release()
