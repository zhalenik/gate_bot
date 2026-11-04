"""SIM800 serial transport, bounded call queue and non-destructive SMS reading.

Importing this module never opens a port or starts a worker. See docs/SIM800C.md.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import csv
import logging
import math
import os
import re
import threading
import time
import uuid

from sms_pdu import PDUDecodeError, UnsupportedPDU, assemble_messages, decode_deliver

log = logging.getLogger("gatebot.sim800")
_PHONE = re.compile(r"\+?[0-9]{3,15}\Z")
_CALL_END = {"NO CARRIER", "BUSY", "NO ANSWER", "NO DIALTONE"}


def _audit(name, *, level="INFO", branch_id=None, details=None):
    try:
        import logger
        logger.event(name, level=level, branch_id=branch_id, details=details)
    except Exception:
        # A full/unwritable log disk must not strand the COM worker mid-call.
        log.error("sim800_audit_unavailable")


class ModemError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False):
        super().__init__(code)
        self.code, self.uncertain = code, uncertain


def _setting(name, default, legacy=None):
    if name in os.environ:
        return os.environ[name]
    try:
        from storage import config
        return getattr(config, name, getattr(config, legacy, default) if legacy else default)
    except ImportError:
        return default


def _boolean(value) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text not in {"0", "1", "true", "false", "yes", "no", "on", "off"}:
        raise ValueError("Invalid boolean modem setting")
    return text in {"1", "true", "yes", "on"}


def _phone(value: str) -> str:
    if not isinstance(value, str) or not _PHONE.fullmatch(value.strip()):
        raise ValueError("Invalid dial number")
    return value.strip()


def _duration(value) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or not 0.1 <= seconds <= 30:
        raise ValueError("Call duration must be between 0.1 and 30 seconds")
    return seconds


class Sim800Modem:
    """One port owner; the same lock covers complete calls and SMS transactions."""

    def __init__(self, port: str, baudrate: int = 115200, command_timeout: float = 5,
                 serial_factory=None):
        if not math.isfinite(command_timeout) or not 0 < command_timeout <= 60:
            raise ValueError("Invalid command timeout")
        self.port, self.baudrate, self.command_timeout = port, baudrate, command_timeout
        self._factory = serial_factory
        self._serial = None
        self._rx = bytearray()
        self._ready = False
        self._lock = threading.RLock()
        self._stopping = threading.Event()

    def _open_locked(self):
        if self._stopping.is_set():
            raise ModemError("stopping")
        if self._serial is not None:
            return
        if not self.port:
            raise ModemError("port_not_configured")
        factory = self._factory
        if factory is None:
            try:
                import serial
            except ImportError as exc:
                raise ModemError("pyserial_missing") from exc
            factory = serial.Serial
        try:
            kwargs = dict(port=self.port, baudrate=self.baudrate, timeout=0.1,
                          write_timeout=self.command_timeout, rtscts=False, dsrdtr=False)
            if os.name == "posix":
                kwargs["exclusive"] = True
            self._serial = factory(**kwargs)
            self._rx.clear()
        except Exception as exc:
            raise ModemError("port_unavailable") from exc

    def _line_locked(self, deadline, allow_stopping=False):
        while time.monotonic() < deadline:
            if self._stopping.is_set() and not allow_stopping:
                raise ModemError("stopping")
            if b"\n" in self._rx:
                line, _, rest = self._rx.partition(b"\n")
                self._rx = bytearray(rest)
                return line.rstrip(b"\r").decode("ascii", errors="replace").strip()
            try:
                waiting = min(max(getattr(self._serial, "in_waiting", 0), 1), 4096)
                self._rx.extend(self._serial.read(waiting))
            except Exception as exc:
                raise ModemError("serial_read_failed") from exc
            if len(self._rx) > 8192:
                raise ModemError("serial_line_too_long")
        return None

    def _command_locked(self, command, timeout=None, allow_stopping=False):
        """Only exact terminal lines terminate a reply, never fixed-size reads."""
        if self._stopping.is_set() and not allow_stopping:
            raise ModemError("stopping")
        payload = (command + "\r").encode("ascii")
        try:
            if self._serial.write(payload) != len(payload):
                raise ModemError("serial_partial_write")
        except ModemError:
            raise
        except Exception as exc:
            raise ModemError("serial_write_failed") from exc
        deadline = time.monotonic() + (timeout or self.command_timeout)
        lines, size = [], 0
        while True:
            line = self._line_locked(deadline, allow_stopping=allow_stopping)
            if line is None:
                raise ModemError("command_timeout")
            if not line or line == command:
                continue
            if line == "OK":
                return lines
            if line == "ERROR" or line.startswith(("+CME ERROR:", "+CMS ERROR:")):
                raise ModemError("command_rejected")
            if line in _CALL_END:
                if command == "ATH":
                    continue
                if command.startswith("ATD"):
                    raise ModemError("call_ended", uncertain=True)
                continue
            lines.append(line)
            size += len(line)
            if size > 1024 * 1024:
                raise ModemError("serial_response_too_large")

    def _prepare_locked(self):
        self._open_locked()
        if not self._ready:
            try:
                for command in ("AT", "ATE0", "AT+CMEE=2"):
                    self._command_locked(command)
                self._ready = True
            except Exception:
                self._close_locked()
                raise

    def call(self, phone: str, duration: float = 3, *, not_after=None):
        phone, duration = _phone(phone), _duration(duration)
        with self._lock:
            if not_after is not None and time.monotonic() >= not_after:
                raise ModemError("queue_expired")
            self._prepare_locked()
            if not_after is not None and time.monotonic() >= not_after:
                raise ModemError("queue_expired")
            sent, error, ended = False, None, False
            try:
                if self._stopping.is_set():
                    raise ModemError("stopping")
                sent = True
                self._command_locked(f"ATD{phone};")
                deadline = time.monotonic() + duration
                while not self._stopping.is_set() and time.monotonic() < deadline:
                    line = self._line_locked(min(deadline, time.monotonic() + 0.15),
                                             allow_stopping=True)
                    if line in _CALL_END:
                        ended = True
                        break
            except ModemError as exc:
                error = ModemError(exc.code, uncertain=sent)
            finally:
                if sent and self._serial is not None:
                    try:
                        self._command_locked("ATH", allow_stopping=True)
                    except ModemError:
                        error = error or ModemError("hangup_unconfirmed", uncertain=True)
                if error:
                    self._close_locked()
            if error:
                raise error
            return {"delivery": "command_sent", "confirmed": False,
                    "call_ended_by_network": ended}

    def read_sms(self, storage="SM") -> dict:
        """List received messages without deleting them or marking them as read."""
        if storage not in {"SM", "ME", "MT"}:
            raise ValueError("Unsupported SMS storage selector")
        with self._lock:
            self._prepare_locked()
            try:
                self._command_locked(f'AT+CPMS="{storage}"')
                self._command_locked("AT+CMGF=0")
                lines = self._command_locked("AT+CMGL=4,1", timeout=max(30, self.command_timeout))
            except ModemError:
                self._close_locked()
                raise
        parts, errors = _parse_cmgl(lines)
        return {"status": "success", "partial": bool(errors), "storage": storage, "segments_count": len(parts),
                "messages": [m.as_dict() for m in assemble_messages(parts)], "errors": errors}

    def request_stop(self):
        self._stopping.set()

    def _close_locked(self):
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                _audit("SIM800_CLOSE_FAILED", level="WARNING")
        self._serial, self._ready = None, False
        self._rx.clear()

    def close(self):
        with self._lock:
            self._close_locked()


def _parse_cmgl(lines):
    parts, errors, pending = [], [], None
    for line in lines:
        if line.startswith("+CMGL:"):
            if pending:
                errors.append({"storage_index": pending[0], "code": "missing_pdu"})
            try:
                fields = next(csv.reader([line.split(":", 1)[1].strip()]))
                pending = (int(fields[0]), int(fields[1]), int(fields[-1]))
            except (ValueError, IndexError, csv.Error):
                pending = None
                errors.append({"storage_index": None, "code": "invalid_cmgl_header"})
        elif pending and re.fullmatch(r"[\da-fA-F]+", line):
            index, status, length = pending
            pending = None
            if status not in (0, 1):
                continue
            try:
                raw = bytes.fromhex(line)
                if not raw or len(raw) - 1 - raw[0] != length:
                    raise PDUDecodeError("PDU length does not match CMGL")
                parts.append(decode_deliver(line, storage_index=index))
            except UnsupportedPDU:
                errors.append({"storage_index": index, "code": "unsupported_pdu"})
            except ValueError:
                errors.append({"storage_index": index, "code": "invalid_pdu"})
    if pending:
        errors.append({"storage_index": pending[0], "code": "missing_pdu"})
    return parts, errors


class CallQueue:
    def __init__(self, modem=None, *, enabled=None, dry_run=None, max_calls=None, ttl=None):
        self.enabled = _boolean(_setting("SIM800_ENABLED", False) if enabled is None else enabled)
        self.dry_run = _boolean(_setting("GATE_DRY_RUN", True) if dry_run is None else dry_run)
        self.call_queue_timeout = float(ttl if ttl is not None else
                                        _setting("SIM800_QUEUE_TIMEOUT", 40, "call_queue_timeout"))
        self.max_calls_in_queue = int(max_calls if max_calls is not None else
                                      _setting("SIM800_MAX_QUEUE", 4, "max_calls_in_queue"))
        if not math.isfinite(self.call_queue_timeout) or self.call_queue_timeout <= 0 or self.max_calls_in_queue < 1:
            raise ValueError("Invalid call queue limits")
        self.modem = modem or Sim800Modem(str(_setting("SIM800_PORT", "")),
                                          int(_setting("SIM800_BAUDRATE", 115200)),
                                          float(_setting("SIM800_COMMAND_TIMEOUT", 5)))
        self.queue, self.recent = deque(), deque(maxlen=100)
        self.lock = threading.Lock()
        self.processing, self._closed, self.active, self._thread = False, False, None, None

    @staticmethod
    def _public(call):
        return {k: v for k, v in call.items() if k not in {"phone", "deadline"}} | {
            "phone_masked": "***" + call["phone"][-4:]}

    def _cleanup_expired(self):
        now = time.monotonic()
        retained = deque()
        for call in self.queue:
            if call["deadline"] <= now:
                call.update(status="expired", code="queue_expired")
                self.recent.append(call)
                self._event(call)
            else:
                retained.append(call)
        self.queue = retained

    @staticmethod
    def _event(call):
        _audit("SIM800_COMMAND", branch_id=call["branch_id"],
               details={"command_id": call["command_id"], "command_status": call["status"],
                        "error_code": call.get("code")})

    def add_call(self, phone, duration=None, branch_id=None):
        try:
            phone = _phone(phone)
            seconds = _duration(duration if duration is not None else _setting("SIM800_CALL_DURATION", 3))
        except (ValueError, TypeError):
            return {"status": "error", "code": "invalid_call", "message": "Неверные параметры команды"}
        with self.lock:
            if self._closed:
                return {"status": "error", "code": "stopping", "message": "Сервис завершает работу"}
            if not self.dry_run and not self.enabled:
                return {"status": "error", "code": "modem_disabled", "message": "Модуль связи выключен"}
            if not self.dry_run and not self.modem.port:
                return {"status": "error", "code": "port_not_configured", "message": "Порт модуля связи не настроен"}
            self._cleanup_expired()
            for call in list(self.queue) + ([self.active] if self.active else []):
                if call["phone"] == phone:
                    return {"status": "success", "delivery": "queued", "coalesced": True,
                            "command_id": call["command_id"], "confirmed": False,
                            "message": "Запрос уже в обработке"}
            if len(self.queue) + bool(self.active) >= self.max_calls_in_queue:
                return {"status": "error", "code": "queue_full", "message": "Очередь команд переполнена"}
            call = {"phone": phone, "duration": seconds, "branch_id": branch_id,
                    "command_id": uuid.uuid4().hex, "timestamp": datetime.now(timezone.utc).isoformat(),
                    "deadline": time.monotonic() + self.call_queue_timeout,
                    "status": "simulated" if self.dry_run else "queued", "confirmed": False}
            self._event(call)
            if self.dry_run:
                self.recent.append(call)
            else:
                self.queue.append(call)
                if not self.processing:
                    self.processing = True
                    self._thread = threading.Thread(target=self._process_queue, name="sim800-call-queue", daemon=True)
                    self._thread.start()
            return {"status": "success", "delivery": call["status"], "accepted": True,
                    "command_id": call["command_id"], "confirmed": False,
                    "message": "Тестовая команда выполнена" if self.dry_run else "Запрос добавлен в очередь"}

    def _process_queue(self):
        while True:
            with self.lock:
                self._cleanup_expired()
                if self._closed or not self.queue:
                    self.processing = False
                    return
                call = self.queue.popleft()
                call["status"], self.active = "processing", call
            try:
                result = self.modem.call(call["phone"], call["duration"], not_after=call["deadline"])
                update = {"status": "command_sent", **result}
            except ModemError as exc:
                update = {"status": "unknown" if exc.uncertain else
                          ("expired" if exc.code == "queue_expired" else "failed"), "code": exc.code}
            except Exception:
                update = {"status": "unknown", "code": "unexpected_modem_error"}
            with self.lock:
                call.update(update)
                self.recent.append(call)
                self.active = None
                self._event(call)

    def status(self):
        with self.lock:
            return {"enabled": self.enabled, "dry_run": self.dry_run,
                    "stopping": self._closed,
                    "queue_length": len(self.queue), "max_queue": self.max_calls_in_queue,
                    "active": self._public(self.active) if self.active else None,
                    "queue": [self._public(c) for c in self.queue],
                    "recent": [self._public(c) for c in self.recent]}

    def shutdown(self, timeout=10):
        with self.lock:
            self._closed = True
            for call in self.queue:
                call.update(status="cancelled", code="shutdown")
                self.recent.append(call)
                self._event(call)
            self.queue.clear()
            worker = self._thread
        self.modem.request_stop()
        if worker and worker is not threading.current_thread():
            worker.join(timeout=max(0, timeout))
        finished = not worker or not worker.is_alive()
        if finished:
            self.modem.close()
        else:
            _audit("SIM800_SHUTDOWN_TIMEOUT", level="WARNING")
        return finished


_queue = None
_queue_lock = threading.Lock()
_shutdown_requested = False


def _get_queue():
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = CallQueue()
            if _shutdown_requested:
                # Shutdown is terminal even when no command ever initialized the
                # singleton. A late web/bot request must not create a new owner.
                _queue.shutdown(timeout=0)
        return _queue


def open_gate(phone: str, duration=None, branch_id=None) -> dict:
    """Enqueue a command; success means acceptance, never physical confirmation."""
    return _get_queue().add_call(phone, duration=duration, branch_id=branch_id)


def get_call_queue_status() -> dict:
    return _get_queue().status()


def read_sms(storage=None) -> dict:
    queue = _get_queue()
    if not queue.enabled:
        return {"status": "error", "code": "modem_disabled", "messages": []}
    try:
        return queue.modem.read_sms(storage or str(_setting("SIM800_SMS_STORAGE", "SM")))
    except (ModemError, ValueError) as exc:
        return {"status": "error", "code": getattr(exc, "code", "invalid_storage"), "messages": []}


def shutdown(timeout=10):
    global _shutdown_requested
    with _queue_lock:
        _shutdown_requested = True
        queue = _queue
    return queue.shutdown(timeout) if queue else True
