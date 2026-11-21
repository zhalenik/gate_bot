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
    if name in {"SIM800_COMMAND", "SIM800_AT_ERROR", "SIM800_STATUS", "SIM800_URC", "SIM800_INBOX_ERROR"}:
        fields = details or {}
        print("SIM800: " + name.removeprefix("SIM800_") + " " +
              " ".join(f"{key}={value}" for key, value in fields.items()), flush=True)
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
                 serial_factory=None, startup_delay: float = 3.0, inbox=None):
        if not math.isfinite(command_timeout) or not 0 < command_timeout <= 300:
            raise ValueError("Invalid command timeout")
        if not math.isfinite(startup_delay) or not 0 <= startup_delay <= 10:
            raise ValueError("Invalid startup delay")
        self.inbox = inbox
        self._incoming_id = None
        self._incoming_last = 0.0
        self.sms_pending = False
        self.pending_sms_storages = set()
        self.sms_notifications = {}
        self.sms_storages = {str(_setting("SIM800_SMS_STORAGE", "SM"))}
        self.sms_setup_error = None
        self.last_status = {}
        self.last_sms_status = {}
        self.startup_delay = startup_delay
        self.port, self.baudrate, self.command_timeout = port, baudrate, command_timeout
        self._factory = serial_factory
        self._serial = None
        self._rx = bytearray()
        self._received_bytes = 0
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
                          write_timeout=self.command_timeout, bytesize=8, parity="N", stopbits=1,
                          xonxoff=False, rtscts=False, dsrdtr=False)
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
                text = line.rstrip(b"\r").decode("ascii", errors="replace").strip()
                self._capture_notice(text)
                self._report_notice(text)
                return text
            try:
                waiting = min(max(getattr(self._serial, "in_waiting", 0), 1), 4096)
                data = self._serial.read(waiting)
                self._received_bytes += len(data)
                self._rx.extend(data)
            except Exception as exc:
                raise ModemError("serial_read_failed") from exc
            if len(self._rx) > 8192:
                raise ModemError("serial_line_too_long")
        return None

    def _capture_notice(self, line):
        if line.startswith("+CMTI:"):
            self.sms_pending = True
            match = re.fullmatch(r'\+CMTI: *"(SM|ME|MT)", *([0-9]+)', line)
            if match:
                self.pending_sms_storages.add(match[1])
                self.sms_notifications[(match[1], int(match[2]))] = datetime.now(timezone.utc).isoformat()
        if self.inbox is None:
            return
        fields = None
        try:
            if line.startswith(("+CLIP:", "+CLCC:")):
                fields = next(csv.reader([line.partition(":")[2].strip()], skipinitialspace=True))
            incoming = fields and line.startswith("+CLCC:") and fields[1] == "1"
            ringing = line in {"RING", "+CRING: VOICE"} or line.startswith("+CLIP:") or incoming
            if ringing:
                now = time.monotonic()
                if self._incoming_id is None or now - self._incoming_last > 20:
                    self._incoming_id = self.inbox.add('call', 'incoming', phone=None,
                                                       status='ringing')
                self._incoming_last = now
                update = {'last_seen': datetime.now(timezone.utc).isoformat()}
                if fields:
                    raw = fields[5] if incoming and len(fields) > 5 else (fields[0] if not incoming else '')
                    toa = fields[6] if incoming and len(fields) > 6 else (fields[1] if not incoming and len(fields) > 1 else '')
                    if re.fullmatch(r'\+?[0-9]{3,15}', raw):
                        update['phone'] = ('+' if toa == '145' and not raw.startswith('+') else '') + raw
                self.inbox.update(self._incoming_id, **update)
            elif line in _CALL_END and self._incoming_id:
                self.inbox.update(self._incoming_id, status='ended', ended_at=datetime.now(timezone.utc).isoformat())
                self._incoming_id = None
        except Exception:
            _audit("SIM800_INBOX_ERROR", level="ERROR", details={"error_code": "inbox_write_failed"})

    @staticmethod
    def _report_notice(line):
        # Never print raw URCs: CLCC/CLIP and SMS can contain private data.
        if line in _CALL_END | {"RING"}:
            _audit("SIM800_URC", details={"notice": line})
        elif line == "+CRING: VOICE":
            _audit("SIM800_URC", details={"notice": "RING"})
        elif line.startswith("+CLCC:"):
            match = re.match(r'\+CLCC: *(\d+),([01]),([0-5]),([0-9]),([01])(?:,|$)', line)
            if match:
                _audit("SIM800_URC", details={
                    "notice": "call_state", "direction": "incoming" if match[2] == "1" else "outgoing",
                    "call_state": {"0": "active", "1": "held", "2": "dialing", "3": "alerting",
                                   "4": "incoming", "5": "waiting"}[match[3]]})

    def poll_events(self, timeout=0.1):
        """Read unsolicited notices while idle, without another port owner."""
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._serial is not None and not self._stopping.is_set():
                deadline = time.monotonic() + timeout
                # Drain a burst, including leading blank lines, in this turn.
                while time.monotonic() < deadline:
                    if self._line_locked(deadline) is None:
                        break
        except ModemError as error:
            _audit("SIM800_STATUS", details={"state": "error", "error_code": error.code})
            self._close_locked()
        finally:
            self._lock.release()

    def _command_locked(self, command, timeout=None, allow_stopping=False):
        """Only exact terminal lines terminate a reply, never fixed-size reads."""
        if self._stopping.is_set() and not allow_stopping:
            raise ModemError("stopping")
        self._received_bytes = 0
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
                stage = command if command in {"AT", "ATE0", "AT+CMEE=2", "AT+CPIN?", "AT+CREG?", "AT+CSQ", "ATH", "ATQ0V1"} else ("dial" if command.startswith("ATD") else "other")
                _audit("SIM800_AT_ERROR", details={"stage": stage, "error_code": "command_timeout",
                                                  "received_bytes": self._received_bytes})
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
                # SIMCom recommends a startup grace period before autobaud.
                # Only initialization is retried; ATD must never be redialled.
                if self._stopping.wait(self.startup_delay):
                    raise ModemError("stopping")
                for command in ("AT", "AT", "AT", "ATQ0V1"):
                    try:
                        self._command_locked(command)
                        break
                    except ModemError as error:
                        if error.code not in {"command_timeout", "command_rejected"} or command == "ATQ0V1":
                            raise
                        if self._stopping.wait(0.1):
                            raise ModemError("stopping") from None
                for command in ("ATE0", "AT+CMEE=2"):
                    self._command_locked(command)
                # Automatic call-state URCs; optional on older firmware.
                try:
                    self._command_locked("AT+CLCC=1")
                except ModemError as error:
                    if error.code != "command_rejected":
                        raise
                storage = str(_setting('SIM800_SMS_STORAGE', 'SM'))
                if storage not in {'SM', 'ME', 'MT'}:
                    raise ModemError('invalid_storage')
                for command in ("AT+CLIP=1", "AT+CMGF=0", f'AT+CPMS="{storage}","{storage}","{storage}"', "AT+CNMI=2,1,0,0,0"):
                    try:
                        self._command_locked(command)
                    except ModemError as error:
                        self.sms_setup_error = error.code
                        if error.code != "command_rejected":
                            raise
                # Poll physical stores even when an unsolicited CMTI was lost.
                try:
                    for line in self._command_locked('AT+CPMS=?'):
                        if line.startswith('+CPMS:'):
                            supported = set(re.findall(r'"(SM|ME)"', line))
                            self.sms_storages.update(supported)
                except ModemError as error:
                    if error.code != 'command_rejected':
                        raise
                self._ready = True
            except Exception:
                self._close_locked()
                raise

    def call(self, phone: str, duration: float = 3, *, not_after=None, before_dial=None):
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
                if before_dial is not None:
                    before_dial()
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
                        hangup_timeout = min(5, self.command_timeout) if self._stopping.is_set() else self.command_timeout
                        self._command_locked("ATH", timeout=hangup_timeout, allow_stopping=True)
                    except ModemError:
                        error = error or ModemError("hangup_unconfirmed", uncertain=True)
                if error:
                    self._close_locked()
            if error:
                raise error
            return {"delivery": "command_sent", "confirmed": False,
                    "call_ended_by_network": ended}

    def diagnostics(self):
        """Query only non-personal status under the same lock as calls and SMS."""
        if not self._lock.acquire(blocking=False):
            return {"state": "busy"}
        try:
            self._prepare_locked()
            result = {"state": "ok", "sim": "unknown", "registration": "unknown",
                      "rssi": None, "signal_dbm": None}
            for command in ("AT+CPIN?", "AT+CREG?", "AT+CSQ"):
                for line in self._command_locked(command):
                    if command == "AT+CPIN?" and line.startswith("+CPIN:"):
                        value = line.partition(":")[2].strip()
                        result["sim"] = value if value in {"READY", "SIM PIN", "SIM PUK", "NOT INSERTED"} else "unknown"
                    elif command == "AT+CREG?":
                        match = re.fullmatch(r'\+CREG: *[0-2], *([0-5])(?:,.*)?', line)
                        if match:
                            result["registration"] = {"0": "not_registered", "1": "home", "2": "searching", "3": "denied", "4": "unknown", "5": "roaming"}[match[1]]
                    elif command == "AT+CSQ":
                        match = re.fullmatch(r'\+CSQ: *(\d{1,2}), *(\d{1,2})', line)
                        if match and 0 <= int(match[1]) <= 31:
                            result["rssi"] = int(match[1])
                            result["signal_dbm"] = -113 + 2 * int(match[1])
            operator = self._operator_locked()
            if operator:
                result['operator'] = operator
            self.last_status = {**result, "checked_at": datetime.now(timezone.utc).isoformat()}
            return result
        except ModemError as error:
            self._close_locked()
            self.last_status = {"state": "error", "error_code": error.code,
                                "checked_at": datetime.now(timezone.utc).isoformat()}
            return {"state": "error", "error_code": error.code}
        finally:
            self._lock.release()

    def available_now(self):
        if self._lock.acquire(blocking=False):
            self._lock.release()
            return True
        return False

    def _operator_locked(self):
        # Read current network/SIM names; never scan or select a new network.
        for command, prefix in (('AT+COPS?', '+COPS:'), ('AT+CSPN?', '+CSPN:')):
            try:
                lines = self._command_locked(command)
            except ModemError as error:
                if error.code == 'command_rejected':
                    continue
                raise
            for line in lines:
                if not line.startswith(prefix):
                    continue
                try:
                    fields = next(csv.reader([line.partition(':')[2].strip()], skipinitialspace=True))
                    name = fields[2] if command == 'AT+COPS?' and fields[1] in {'0', '1'} else fields[0] if command == 'AT+CSPN?' else ''
                except (IndexError, csv.Error):
                    continue
                if re.fullmatch(r'(?:[0-9A-Fa-f]{4}){2,32}', name):
                    try:name = bytes.fromhex(name).decode('utf-16-be')
                    except (ValueError, UnicodeError):continue
                name = name.strip()
                if 1 <= len(name) <= 64 and re.fullmatch(r"[\w .&'()+/-]+", name, re.UNICODE) and not name.isdigit():
                    return name
        return None

    @staticmethod
    def _sms_failure(line):
        value = line.partition(":")[2].strip().lower()
        return {"requested facility not subscribed": "sms_service_not_subscribed",
                "memory full": "sms_memory_full", "322": "sms_memory_full",
                "network timeout": "sms_network_timeout", "50": "sms_service_not_subscribed",
                "55": "sms_service_not_subscribed", "331": "sms_network_unavailable",
                "332": "sms_network_timeout", "330": "sms_service_center_unknown"}.get(value, "sms_rejected")

    def _sms_prompt_locked(self):
        deadline = time.monotonic() + self.command_timeout
        while time.monotonic() < deadline:
            if self._stopping.is_set():
                raise ModemError("stopping")
            if self._rx.lstrip(b"\r\n ").startswith(b">"):
                self._rx.clear()
                return
            if b"\n" in self._rx:
                line, _, rest = self._rx.partition(b"\n")
                self._rx = bytearray(rest)
                text = line.decode('ascii', errors='replace').strip()
                self._capture_notice(text)
                self._report_notice(text)
                if text == 'ERROR' or text.startswith(('+CMS ERROR:', '+CME ERROR:')):
                    raise ModemError(self._sms_failure(text))
            else:
                try:
                    self._rx.extend(self._serial.read(1))
                except Exception:
                    raise ModemError("serial_read_failed") from None
            if len(self._rx) > 8192:
                raise ModemError("serial_line_too_long")
        raise ModemError("sms_prompt_timeout")

    def send_sms(self, phone, text, *, progress, not_after=None, before_submit=None):
        from sms_submit import encode_submit
        pdus = encode_submit(phone, text)
        segments = [{"sequence": p['sequence'], "status": "pending"} for p in pdus]
        with self._lock:
            if not_after is not None and time.monotonic() >= not_after:
                raise ModemError("queue_expired")
            self._prepare_locked()
            self._command_locked('AT+CMGF=0')
            if not_after is not None and time.monotonic() >= not_after:
                raise ModemError("queue_expired")
        for pdu, segment in zip(pdus, segments):
            # Give incoming storage notices priority between complete CMGS
            # transactions. No second serial reader and no interruption of PDU.
            if self.sms_pending and self.inbox is not None:
                for storage in sorted(self.pending_sms_storages or self.sms_storages):
                    self.read_sms(storage)
            with self._lock:
                self._prepare_locked()
                submitted, acknowledged = False, False
                try:
                    if self._stopping.is_set():
                        raise ModemError('stopping')
                    command = f"AT+CMGS={pdu['tpdu_length']}\r".encode('ascii')
                    if self._serial.write(command) != len(command):
                        raise ModemError('serial_partial_write')
                    self._sms_prompt_locked()
                    payload = pdu['pdu'].encode('ascii') + b'\x1a'
                    if segment['sequence']==1 and before_submit is not None:
                        before_submit()
                    segment['status'] = 'submitting'
                    progress([dict(p) for p in segments])  # durable claim before CTRL-Z
                    if self._stopping.is_set():
                        raise ModemError('stopping')
                    if segment['sequence'] == 1 and not_after is not None and time.monotonic() >= not_after:
                        raise ModemError('queue_expired')
                    submitted = True
                    if self._serial.write(payload) != len(payload):
                        raise ModemError('serial_partial_write', uncertain=True)
                    deadline = time.monotonic() + 60
                    while True:
                        line = self._line_locked(deadline)
                        if line is None:
                            raise ModemError('sms_result_timeout', uncertain=True)
                        match = re.fullmatch(r'\+CMGS: *(\d+)', line)
                        if match:
                            acknowledged = True
                            segment['modem_reference'] = int(match[1])
                        elif line == 'OK' and acknowledged:
                            segment['status'] = 'network_accepted'
                            break
                        elif line == 'ERROR' or line.startswith(('+CMS ERROR:', '+CME ERROR:')):
                            submitted = False  # explicit rejection, not a transport timeout
                            raise ModemError(self._sms_failure(line))
                    progress([dict(p) for p in segments])
                except Exception as error:
                    uncertain = submitted or acknowledged
                    segment.update(status='unknown' if uncertain else 'failed',
                                   code=error.code if isinstance(error, ModemError) else 'sms_transport_error')
                    try:
                        if self._serial is not None and not submitted:
                            self._serial.write(b'\x1b')  # exit unfinished editor, never CTRL-Z
                    except Exception:
                        pass
                    self._close_locked()
                    progress([dict(p) for p in segments])
                    accepted = sum(p['status'] == 'network_accepted' for p in segments)
                    outcome = 'partial' if accepted else 'unknown' if uncertain else 'expired' if segment['code'] == 'queue_expired' else 'failed'
                    return {"status": outcome, "delivery": outcome,
                            "code": segment['code'], "segments": segments, "confirmed": False}
        return {"status": "network_accepted", "delivery": "network_accepted",
                "segments": segments, "confirmed": False}

    def read_sms(self, storage="SM", *, cleanup=True) -> dict:
        """Archive durably, then remove only those received parts from this storage."""
        from telephony_store import part_key
        if storage not in {"SM", "ME", "MT"}:
            raise ValueError("Unsupported SMS storage selector")
        with self._lock:
            self._prepare_locked()
            try:
                usage = self._command_locked(f'AT+CPMS="{storage}"')
                self._command_locked("AT+CMGF=0")
                lines = self._command_locked("AT+CMGL=4,1", timeout=max(30, self.command_timeout))
                parts, errors = _parse_cmgl(lines)
                deleted, cleanup_errors = 0, 0
                if self.inbox is not None:
                    observations = {p.storage_index: self.sms_notifications.get((storage, p.storage_index)) for p in parts}
                    receipts = self.inbox.ingest(parts, storage=storage, observed_at=observations)
                    # Still own the same COM lock and selected storage. An occupied
                    # SIM slot cannot be reused before we delete it. Never bulk-clear.
                    current = {p.storage_index: part_key(p, getattr(self.inbox, 'modem_id', 'legacy')) for p in parts}
                    for location in receipts if cleanup else []:
                        index = location['index']
                        if current.get(index) != location['part_key']:
                            continue
                        try:
                            self._command_locked(f'AT+CMGD={index},0')
                        except ModemError:
                            cleanup_errors += 1
                            self._close_locked()
                            break  # Retry by rescanning next time, not by a stale slot number.
                        self.inbox.acknowledge_sim(location)
                        self.sms_notifications.pop((storage, index), None)
                        current.pop(index, None)
                        deleted += 1
                self.last_sms_status = {"state": "partial" if errors or cleanup_errors else "ok",
                    "storage": storage, "parts_read": len(parts), "parse_errors": len(errors),
                    "deleted_parts": deleted, "cleanup_errors": cleanup_errors,
                    "checked_at": datetime.now(timezone.utc).isoformat()}
                for line in usage:
                    match = re.match(r'\+CPMS: *(\d+), *(\d+)', line)
                    if match:
                        self.last_sms_status.update(used=int(match[1]), capacity=int(match[2]))
                if parts or errors or cleanup_errors:
                    _audit('SIM800_SMS_RECEIVED', details={'parts': len(parts), 'deleted': deleted,
                        'parse_errors': len(errors), 'cleanup_errors': cleanup_errors})
                return {"status": "success", "partial": bool(errors or cleanup_errors),
                    "storage": storage, "segments_count": len(parts), "deleted_parts": deleted,
                    "messages": [m.as_dict() for m in assemble_messages(parts)], "errors": errors}
            except ModemError:
                self._close_locked()
                raise

    def delete_archived_sms(self, locations, *, not_after=None):
        # Explicit superadmin action only. Never clear SIM automatically.
        from telephony_store import part_key
        deleted, skipped = 0, 0
        with self._lock:
            if not_after is not None and time.monotonic() >= not_after:
                raise ModemError('queue_expired')
            self._prepare_locked()
            for storage in sorted({p['storage'] for p in locations}):
                if storage not in {'SM', 'ME', 'MT'}:
                    raise ModemError('invalid_storage')
                self._command_locked(f'AT+CPMS="{storage}"')
                self._command_locked('AT+CMGF=0')
                parts, errors = _parse_cmgl(self._command_locked('AT+CMGL=4,1', timeout=30))
                current = {p.storage_index: part_key(p, getattr(self.inbox, 'modem_id', 'legacy')) for p in parts}
                for location in (p for p in locations if p['storage'] == storage):
                    index = location['index']
                    if type(index) is not int or current.get(index) != location['part_key']:
                        skipped += 1  # Slot may now belong to an entirely different SMS.
                        continue
                    if not_after is not None and time.monotonic() >= not_after:
                        raise ModemError('queue_expired')
                    try:
                        self._command_locked(f'AT+CMGD={index},0')
                    except ModemError as error:
                        raise ModemError(error.code, uncertain=True) from None
                    if self.inbox is not None:
                        self.inbox.acknowledge_sim(location)
                    deleted += 1
                    current.pop(index, None)
        return {'status': 'sim_cleanup_complete', 'delivery': 'sim_cleanup_complete',
                'deleted_parts': deleted, 'skipped_parts': skipped, 'confirmed': False}

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

    def close_within(self, timeout):
        if not self._lock.acquire(timeout=max(0, timeout)):
            return False
        try:
            self._close_locked()
            return True
        finally:
            self._lock.release()


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
    def __init__(self, modem=None, *, devices=None, enabled=None, dry_run=None, max_calls=None, ttl=None):
        self.enabled = _boolean(_setting("SIM800_ENABLED", False) if enabled is None else enabled)
        self.dry_run = _boolean(_setting("GATE_DRY_RUN", True) if dry_run is None else dry_run)
        self.call_queue_timeout = float(ttl if ttl is not None else
                                        _setting("SIM800_QUEUE_TIMEOUT", 40, "call_queue_timeout"))
        self.max_calls_in_queue = int(max_calls if max_calls is not None else
                                      _setting("SIM800_MAX_QUEUE", 4, "max_calls_in_queue"))
        if not math.isfinite(self.call_queue_timeout) or self.call_queue_timeout <= 0 or self.max_calls_in_queue < 1:
            raise ValueError("Invalid call queue limits")
        from telephony_store import TelephonyStore
        from modem_settings import default_modem, read_modems
        self.journal = TelephonyStore()
        if devices is None:
            if modem is not None:
                devices = [({**default_modem(), 'port': modem.port}, modem)]
            else:
                devices = [(row, Sim800Modem(row['port'], row['baudrate'], row['command_timeout'],
                            inbox=TelephonyStore(self.journal.store.path, modem_id=row['id'], modem_name=row['name'])))
                           for row in read_modems()]
        self.devices = {row['id']: (row, driver) for row, driver in devices}
        self.modem = next((driver for _, driver in self.devices.values()), None)
        self.queue, self.recent = deque(), deque(maxlen=100)
        self.lock = threading.Lock()
        self._closed, self._thread = False, None
        self.running = {}
        self.monitor_started = False
        self.workers = set()
        self.turns = {'sms': {}, 'call': {}}
        self.turn = 0
        self.command_guard = None
        self.command_observer = None
        self._failure_code = None
        from modem_settings import branch_modems
        self.branch_modems = branch_modems

    @staticmethod
    def _public(call):
        return {k: v for k, v in call.items() if k not in {"phone", "deadline", "text", "segments", "locations", "source_id", "_before_submit", "_tried", "_modems"}} | {
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

    def _event(self, call):
        if call.get('managed') and self.command_observer is not None:
            try:
                self.command_observer(call['command_id'], call['status'], call.get('code'))
            except Exception:
                # Another port may be preparing its next physical command.
                # Stop every driver before releasing the admission lock.
                self._closed = True
                self._failure_code = 'accounting_unavailable'
                for _, driver in self.devices.values():
                    driver.request_stop()
                _audit('SIM800_STATUS', level='ERROR', details={
                    'state':'error', 'error_code':self._failure_code})
        if call.get("record_id"):
            try:
                self.journal.update(call["record_id"], status=call["status"], code=call.get("code"),
                                    segments=call.get("segments", []),
                                    **{k: call[k] for k in ('modem_id', 'modem_name', 'deleted_parts', 'skipped_parts') if k in call})
            except Exception:
                _audit("SIM800_INBOX_ERROR", level="ERROR", details={"error_code": "outgoing_write_failed"})
        _audit("SIM800_COMMAND", branch_id=call["branch_id"],
               details={"command_id": call["command_id"], "command_status": call["status"],
                        "error_code": call.get("code")})

    def add_call(self, phone, duration=None, branch_id=None, *, command_id=None, gate_id=None):
        try:
            phone = _phone(phone)
            seconds = _duration(duration if duration is not None else _setting("SIM800_CALL_DURATION", 3))
        except (ValueError, TypeError):
            return {"status": "error", "code": "invalid_call", "message": "Неверные параметры команды"}
        assigned = self.branch_modems(branch_id)
        with self.lock:
            if self._closed:
                return {"status": "error", "code": self._failure_code or "stopping", "message": "Очередь остановлена. Проверьте состояние сервиса"}
            if not self.dry_run and not self.enabled:
                return {"status": "error", "code": "modem_disabled", "message": "Модуль связи выключен"}
            if not self.dry_run and not self._candidates({'kind': 'call', '_modems': assigned}):
                return {"status": "error", "code": "port_not_configured", "message": "Модем для звонков не настроен"}
            self._cleanup_expired()
            if self._closed:
                return {'status':'error','code':self._failure_code or 'stopping','message':'Очередь остановлена. Проверьте состояние сервиса'}
            if command_id:
                for call in list(self.queue) + list(self.recent) + list(self.running.values()):
                    if call['command_id'] == command_id:
                        return {'status': 'success', 'delivery': call['status'], 'coalesced': True,
                                'command_id': command_id, 'confirmed': False, 'message': 'Запрос уже в обработке'}
            for call in list(self.queue) + list(self.running.values()):
                if (not command_id and call["phone"] == phone and
                        str(call.get('branch_id')) == str(branch_id) and
                        call.get('gate_id') == gate_id and not call.get("kind")):
                    return {"status": "success", "delivery": "queued", "coalesced": True,
                            "command_id": call["command_id"], "confirmed": False,
                            "message": "Запрос уже в обработке"}
            if len(self.queue) + len(self.running) >= self.max_calls_in_queue:
                return {"status": "error", "code": "queue_full", "message": "Очередь команд переполнена"}
            call = {"phone": phone, "duration": seconds, "branch_id": branch_id, "_modems": assigned,
                    "command_id": command_id or uuid.uuid4().hex, 'gate_id': gate_id, 'managed': bool(command_id),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "deadline": time.monotonic() + self.call_queue_timeout,
                    "status": "simulated" if self.dry_run else "queued", "confirmed": False}
            try:
                call['record_id'] = self.journal.add('call', 'outgoing', phone=phone, duration=seconds,
                    status=call['status'], action='gate', branch_id=branch_id, modem_id=None, modem_name=None)
            except Exception:
                return {'status': 'error', 'code': 'journal_unavailable', 'message': 'Не удалось сохранить вызов'}
            self._event(call)
            if self._failure_code:
                return {'status':'error','code':self._failure_code,'message':'Не удалось сохранить состояние команды'}
            if self.dry_run:
                self.recent.append(call)
            else:
                self.queue.append(call)
                self._start_workers()
            return {"status": "success", "delivery": call["status"], "accepted": True,
                    "command_id": call["command_id"], "confirmed": False,
                    "message": "Тестовая команда выполнена" if self.dry_run else "Запрос добавлен в очередь"}

    def add_manual(self, kind, phone, *, text='', duration=20, record_id=None, locations=None, source_id=None,
                   sensitive=False, priority=False, command_id=None, before_submit=None, target_modem=None, branch_id=None):
        # Admission is authorized by TelephonyService; shares the gate hardware worker.
        assigned = self.branch_modems(branch_id)
        with self.lock:
            if self._closed or (not self.dry_run and not self.enabled):
                return {"status": "error", "code": "modem_disabled", "message": "Модем выключен или останавливается"}
            if not self.dry_run and not self._candidates({'kind': kind, 'target_modem': target_modem, '_modems': assigned}):
                return {"status": "error", "code": "port_not_configured", "message": "Нет модема для этой операции"}
            self._cleanup_expired()
            if self._closed:
                return {'status':'error','code':self._failure_code or 'stopping','message':'Очередь остановлена. Проверьте состояние сервиса'}
            if len(self.queue) + len(self.running) >= self.max_calls_in_queue:
                return {"status": "error", "code": "queue_full", "message": "Очередь модема заполнена"}
            call = {"kind": kind, "phone": phone, "text": text, "duration": duration,
                    "_before_submit": before_submit,
                    "locations": locations, "source_id": source_id, "target_modem": target_modem,
                    "record_id": record_id, 'sensitive': sensitive, "branch_id": branch_id, "_modems": assigned,
                    "command_id": command_id or uuid.uuid4().hex,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "deadline": time.monotonic() + self.call_queue_timeout,
                    "status": "simulated" if self.dry_run else "queued", "confirmed": False}
            self._event(call)
            if self.dry_run:
                self.recent.append(call)
            else:
                if priority:
                    self.queue.appendleft(call)
                else:
                    self.queue.append(call)
                self._start_workers()
            return {"status": "success", "delivery": call["status"], "command_id": call["command_id"],
                    "message": "Имитация без отправки" if self.dry_run else "Принято в очередь"}

    @property
    def processing(self):
        return bool(self.workers)

    def _candidates(self, command):
        kind = 'sms' if command.get('kind') in {'sms', 'sms_delete'} else 'call'
        target = command.get('target_modem')
        return [key for key, (row, driver) in self.devices.items() if driver.port and
                (command.get('_modems') is None or key in command['_modems']) and
                key not in command.get('_tried', ()) and (target is None or key == target) and
                (row[kind + '_enabled'] or command.get('kind') == 'sms_delete')]

    def _start_workers(self):
        count = min(len(self.devices), len(self.queue) + len(self.running))
        while len(self.workers) < count:
            self._thread = threading.Thread(target=self._process_queue, name='sim800-command', daemon=True)
            self.workers.add(self._thread)
            self._thread.start()

    def _take(self):
        for call in self.queue:
            available = [key for key in self._candidates(call) if key not in self.running]
            if not available:
                continue
            kind = 'sms' if call.get('kind') in {'sms', 'sms_delete'} else 'call'
            def rank(key):
                row, driver = self.devices[key]
                busy = hasattr(driver, 'available_now') and not driver.available_now()
                return busy, -row[kind + '_priority'], self.turns[kind].get(key, 0)
            key = min(available, key=rank)
            self.turn += 1
            self.turns[kind][key] = self.turn
            self.queue.remove(call)
            self.running[key] = call
            call.update(status='processing', modem_id=key, modem_name=self.devices[key][0]['name'])
            return call, self.devices[key][1]
        return None

    def _execute(self, modem, call):
        def check_assignment():
            assigned = self.branch_modems(call.get('branch_id'))
            call['_modems'] = assigned
            if assigned is not None and call['modem_id'] not in assigned:
                raise ModemError('modem_reassigned')
        check_assignment()
        record = call.get('record_id')
        if record and not self.journal.update(record, status='processing', modem_id=call['modem_id'], modem_name=call['modem_name']):
            raise ModemError('journal_record_missing')
        if call.get('kind') == 'sms_delete':
            result = modem.delete_archived_sms(call['locations'], not_after=call['deadline'])
            self.journal.update(call['source_id'], sim_cleanup_at=datetime.now(timezone.utc).isoformat())
            return result
        if call.get('kind') == 'sms':
            def progress(segments):
                call['segments'] = segments
                if record:
                    self.journal.update(record, segments=segments)
            kwargs = {'progress': progress, 'not_after': call['deadline']}
            if call.get('_before_submit') is not None or call.get('branch_id') is not None:
                def before_submit():
                    check_assignment()
                    if call.get('_before_submit') is not None and not call['_before_submit']():
                        raise ModemError('authorization_expired')
                kwargs['before_submit'] = before_submit
            return modem.send_sms(call['phone'], call['text'], **kwargs)
        kwargs = {'not_after': call['deadline']}
        if call.get('managed') or call.get('branch_id') is not None:
            def before_dial():
                check_assignment()
                if not call.get('managed'):
                    return
                if self.command_guard is None or not self.command_guard(call['command_id']):
                    raise ModemError('authorization_expired')
                self.command_observer(call['command_id'], 'started', None)
            kwargs['before_dial'] = before_dial
        return modem.call(call['phone'], call['duration'], **kwargs)

    def _process_queue(self):
        while True:
            with self.lock:
                self._cleanup_expired()
                item = None if self._closed else self._take()
                if item is None:
                    self.workers.discard(threading.current_thread())
                    return
            call, driver = item
            try:
                update = {'status': 'command_sent', **self._execute(driver, call)}
            except ModemError as error:
                update = {'status': 'unknown' if error.uncertain else
                          ('expired' if error.code == 'queue_expired' else 'failed'), 'code': error.code}
            except Exception:
                update = {'status': 'unknown', 'code': 'unexpected_modem_error'}
            with self.lock:
                del self.running[call['modem_id']]
                call.setdefault('_tried', set()).add(call['modem_id'])
                segments = update.get('segments', call.get('segments', ()))
                # Never retransmit even one accepted or uncertain segment after failover.
                attempted = any(s.get('status') in {'unknown', 'submitting', 'network_accepted'} for s in segments)
                if attempted and update['status'] == 'failed':
                    update['status'] = 'partial' if any(s.get('status') == 'network_accepted' for s in segments) else 'unknown'
                    update['delivery'] = update['status']
                safe_failure = update['status'] == 'failed' and update.get('code') not in {
                    'authorization_expired', 'stopping', 'journal_record_missing', 'queue_expired'}
                if safe_failure and not self._closed and self._candidates(call) and call['deadline'] > time.monotonic():
                    call['status'] = 'queued'
                    self.queue.appendleft(call)
                    continue
                call.update(update)
                if call.get('sensitive'):
                    call['text'] = ''
                    call['phone'] = ''
                self.recent.append(call)
                self._event(call)

    def public_devices(self):
        return [{'id': key, 'name': row['name'], 'busy': key in self.running,
                 'status': {**getattr(driver, 'last_status', {}), 'sms_poll': getattr(driver, 'last_sms_status', {})}}
                for key, (row, driver) in self.devices.items()]

    def status(self):
        with self.lock:
            devices = self.public_devices()
            active = list(self.running.values())
            default_status = next((dev['status'] for dev in devices if dev['status'].get('state') == 'ok'),
                                  devices[0]['status'] if devices else {})
            return {'enabled': self.enabled, 'dry_run': self.dry_run, 'error_code': self._failure_code,
                    'stopping': self._closed, 'modem': default_status, 'modems': devices,
                    'queue_length': len(self.queue), 'max_queue': self.max_calls_in_queue,
                    'active': self._public(active[0]) if active else None, 'active_commands': [self._public(c) for c in active],
                    'queue': [self._public(c) for c in self.queue], 'recent': [self._public(c) for c in self.recent]}

    def probe(self, key):
        with self.lock:
            if self._closed or not self.enabled or not self.monitor_started:
                raise ValueError('Модуль связи выключен')
            if key not in self.devices:
                raise ValueError('Сохраните модем и перезапустите сервис')
            if key in self.running:
                return {'state': 'busy'}
            driver = self.devices[key][1]
        return driver.diagnostics()

    def shutdown(self, timeout=10):
        with self.lock:
            self._closed = True
            for call in self.queue:
                call.update(status='cancelled', code='shutdown')
                self.recent.append(call)
                self._event(call)
            self.queue.clear()
            workers = list(self.workers)
        for _, driver in self.devices.values():
            driver.request_stop()
        deadline = time.monotonic() + max(0, timeout)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=max(0, deadline - time.monotonic()))
        finished = not any(worker.is_alive() for worker in workers)
        if finished:
            for _, driver in self.devices.values():
                if hasattr(driver, 'close_within'):
                    if not driver.close_within(deadline - time.monotonic()):
                        finished = False
                else:
                    driver.close()
        if not finished:
            _audit('SIM800_SHUTDOWN_TIMEOUT', level='WARNING')
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


def open_gate(phone: str, duration=None, branch_id=None, *, command_id=None, gate_id=None) -> dict:
    """Enqueue a command; success means acceptance, never physical confirmation."""
    return _get_queue().add_call(phone, duration=duration, branch_id=branch_id,
                               command_id=command_id, gate_id=gate_id)


def get_call_queue_status() -> dict:
    return _get_queue().status()


def read_sms(storage=None) -> dict:
    queue = _get_queue()
    if not queue.enabled or not queue.modem:
        return {"status": "error", "code": "modem_disabled", "messages": []}
    try:
        return queue.modem.read_sms(storage or str(_setting("SIM800_SMS_STORAGE", "SM")), cleanup=False)
    except (ModemError, ValueError) as exc:
        return {"status": "error", "code": getattr(exc, "code", "invalid_storage"), "messages": []}


def shutdown(timeout=10):
    global _shutdown_requested
    with _queue_lock:
        _shutdown_requested = True
        queue = _queue
    return queue.shutdown(timeout) if queue else True


def monitor_status(stop_event, interval=30):
    """Runtime-owned status worker; never starts on import, never dials."""
    queue = _get_queue()
    if not queue.enabled:
        _audit("SIM800_STATUS", details={"state": "disabled"})
        return
    if hasattr(queue, 'devices'):
        queue.monitor_started = True
        devices = [(row['name'], driver) for row, driver in queue.devices.values() if driver.port]
    else:
        devices = [('SIM800C', queue.modem)]
    if len(devices) == 1:
        _monitor_modem(stop_event, interval, *devices[0])
        return
    workers = [threading.Thread(target=_monitor_modem, args=(stop_event, interval, name, driver),
               name='sim800-monitor-' + name, daemon=True) for name, driver in devices]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()


def _monitor_modem(stop_event, interval, name, driver):
    print(f"SIM800: modem={name} port={driver.port} baudrate={driver.baudrate}", flush=True)
    next_sms = 0.0
    while not stop_event.is_set():
        result = driver.diagnostics()
        if stop_event.is_set():
            break
        _audit("SIM800_STATUS", details={"modem": name, **result})
        next_status = time.monotonic() + interval
        while not stop_event.is_set() and time.monotonic() < next_status:
            driver.poll_events()
            if time.monotonic() >= next_sms or driver.sms_pending:
                driver.sms_pending = False
                storages = driver.pending_sms_storages.copy()
                driver.pending_sms_storages.difference_update(storages)
                full_scan = time.monotonic() >= next_sms
                # A notice gets priority; fallback scans still cover both stores.
                priority = sorted(storages)
                if full_scan:
                    storages.update(driver.sms_storages)
                states = dict(driver.last_sms_status.get('storages', {}))
                for storage in priority + sorted(storages.difference(priority)):
                    try:
                        driver.read_sms(storage)
                        states[storage] = dict(driver.last_sms_status)
                    except Exception:
                        states[storage] = {"state": "error", "error_code": "sms_read_failed",
                            "checked_at": datetime.now(timezone.utc).isoformat()}
                        _audit("SIM800_INBOX_ERROR", level="ERROR", details={"modem": name, "error_code": "sms_read_failed"})
                driver.last_sms_status = {
                    'state': 'ok' if all(s.get('state') == 'ok' for s in states.values()) else 'partial',
                    'checked_at': datetime.now(timezone.utc).isoformat(), 'storages': states,
                    **{key: sum(s.get(key, 0) for s in states.values()) for key in
                       ('parts_read', 'deleted_parts', 'parse_errors', 'cleanup_errors')}}
                if full_scan:
                    next_sms = time.monotonic() + 3
            stop_event.wait(0.1)
