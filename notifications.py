"""Durable, opt-in automatic access notifications with honest delivery states."""
from pathlib import Path
from datetime import datetime, timezone
import logging
import threading
from storage import config
from runtime_state import RuntimeState
from link_manager import access_is_active
from pass_notifications import timely_grant
import logger


class NotificationDispatcher:
    def __init__(self, callbacks=None, state=None, ready=None, links=None):
        self.state = state or RuntimeState(Path(config.DATA_DIR) / 'runtime.sqlite3')
        self.callbacks = callbacks or {}
        self.ready = ready or {}
        from link_manager import LinkManager
        self.links = links or LinkManager()
        self._stop = threading.Event()
        self._thread = None
        self._health_lock = threading.Lock()
        self._last_success_at = None
        self._last_error_type = None
        self._consecutive_failures = 0

    def enqueue(self, payload):
        # Unsubscribing controls notifications; it never changes gate access.
        if not payload.get('mailing_enabled') or not timely_grant(payload.get('event'), payload):
            return
        for channel, field in [('telegram', 'tg_id'), ('max', 'max_id')]:
            if payload.get(field):
                self.state.enqueue(channel, payload)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self.state.recover_notifications()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='gatebot-notifications', daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            delay = 2
            try:
                self.process_once()
                with self._health_lock:
                    if config.OUTBOUND_ENABLED:
                        self._last_success_at = datetime.now(timezone.utc).isoformat()
                    self._last_error_type = None
                    self._consecutive_failures = 0
            except Exception as error:
                with self._health_lock:
                    self._last_error_type = type(error).__name__
                    self._consecutive_failures += 1
                    failures = self._consecutive_failures
                delay = min(2 ** min(failures, 5), 30)
                try:
                    logger.event('NOTIFICATION_WORKER_ERROR', level='ERROR', details={
                        'error_type': type(error).__name__, 'retry_in_seconds': delay})
                except Exception:
                    logging.getLogger('gatebot.notifications').error('notification_audit_unavailable')
            self._stop.wait(delay)

    def process_once(self):
        if not config.OUTBOUND_ENABLED:
            return
        for channel, callback in self.callbacks.items():
            if self._stop.is_set():
                break
            if channel in self.ready and not self.ready[channel]().get('ready'):
                continue
            # Preparation failures are safely retryable. Read local permissions
            # BEFORE claiming, so a bad/unavailable JSON file does not strand an
            # unsent notification in the ambiguous "sending" state.
            snapshot = self.links.load_links()
            notification = self.state.next_notification(channel)
            if notification is None:
                continue
            payload = notification['payload']
            current = snapshot.get(payload.get('token'))
            recipient_field = {'telegram': 'tg_id', 'max': 'max_id'}[channel]
            if not current or not current.get('mailing_enabled') or current.get(recipient_field) != payload.get(recipient_field) or current.get('access_revision', 0) != payload.get('access_revision', 0):
                self.state.finish_notification(notification['id'], False, 'superseded_or_unsubscribed')
                continue
            event = payload.get('event')
            active = access_is_active(current)
            if not timely_grant(event, current) or not active:
                self.state.finish_notification(notification['id'], False, 'superseded_access_state')
                continue
            error_type = None
            try:
                sent = bool(callback(notification['payload']))
            except Exception as error:
                sent = False
                error_type = type(error).__name__
            self.state.finish_notification(notification['id'], sent, error_type or (None if sent else 'not_delivered'))
            logger.event('NOTIFICATION_SENT' if sent else 'NOTIFICATION_FAILED', branch_id=notification['payload'].get('branch_id'), details={'channel': channel, 'notification_id': notification['id'], 'error_type': error_type})

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=20)

    def status(self):
        """Worker health only; does not promise external or physical delivery."""
        with self._health_lock:
            return {'worker_alive': bool(self._thread and self._thread.is_alive()),
                    'outbound_enabled': config.OUTBOUND_ENABLED,
                    'last_success_at': self._last_success_at,
                    'last_error_type': self._last_error_type,
                    'consecutive_failures': self._consecutive_failures}
