"""Offline modem transcripts and concurrency tests. Never opens a real port."""
from collections import deque
import json
import threading
import time

import pytest

from sim800c import CallQueue, ModemError, Sim800Modem, _parse_cmgl


GOLDEN_PDU = "000405912143F50008621150214365210C041F04400438043204350442"


class FakeSerial:
    def __init__(self, responses=None, chunk=3, **kwargs):
        self.responses = responses or {}
        self.commands = []
        self.incoming = bytearray()
        self.chunk = chunk
        self.closed = False
        self.dial_started = threading.Event()
        self.read_wait = threading.Event()

    @property
    def in_waiting(self):
        return len(self.incoming)

    def write(self, data):
        command = data.decode("ascii").rstrip("\r")
        self.commands.append(command)
        if command.startswith("ATD"):
            self.dial_started.set()
        response = self.responses.get(command, "\r\n" + command + "\r\nOK\r\n")
        if isinstance(response, Exception):
            raise response
        self.incoming.extend(response.encode("ascii"))
        return len(data)

    def read(self, count):
        count = min(count, self.chunk, len(self.incoming))
        result = bytes(self.incoming[:count])
        del self.incoming[:count]
        if not result:
            self.read_wait.wait(0.001)
        return result

    def close(self):
        self.closed = True


def fake_modem(responses=None, timeout=0.1, chunk=3):
    port = FakeSerial(responses=responses, chunk=chunk)
    modem = Sim800Modem("FAKE", command_timeout=timeout, serial_factory=lambda **kwargs: port, startup_delay=0)
    return modem, port


def cmgl(pdu=GOLDEN_PDU, index=1, status=0):
    raw = bytes.fromhex(pdu)
    length = len(raw) - 1 - raw[0]
    return f"+CMGL: {index},{status},,{length}\r\n{pdu}\r\n"


def test_real_call_commands_and_no_false_physical_confirmation():
    modem, port = fake_modem()
    try:
        result = modem.call("10001", duration=0.1)
        assert port.commands == ["AT", "ATE0", "AT+CMEE=2", "AT+CLCC=1", "AT+CLIP=1", "AT+CMGF=0", 'AT+CPMS="SM","SM","SM"', "AT+CNMI=2,1,0,0,0", "AT+CPMS=?", "ATD10001;", "ATH"]
        assert result["delivery"] == "command_sent" and result["confirmed"] is False
    finally:
        modem.close()
    assert port.closed


@pytest.mark.parametrize("reply", ["\r\nERROR\r\n", "\r\n+CMS ERROR: 500\r\n", "\r\n+CME ERROR: 10\r\n"])
def test_command_errors_are_not_accepted_as_success(reply):
    modem, port = fake_modem({"AT": reply, "ATQ0V1": reply})
    with pytest.raises(ModemError, match="command_rejected") as error:
        modem.call("10001", duration=0.1)
    assert error.value.uncertain is False
    assert "ATD10001;" not in port.commands and port.closed


def test_dial_timeout_attempts_hangup_but_never_redials():
    modem, port = fake_modem({"ATD10001;": ""}, timeout=0.015)
    with pytest.raises(ModemError, match="command_timeout") as error:
        modem.call("10001", duration=0.1)
    assert error.value.uncertain
    assert port.commands.count("ATD10001;") == 1
    assert port.commands[-1] == "ATH" and port.closed


def test_missing_hangup_ack_is_an_unknown_result():
    modem, port = fake_modem({"ATH": ""}, timeout=0.015)
    with pytest.raises(ModemError, match="hangup_unconfirmed") as error:
        modem.call("10001", duration=0.1)
    assert error.value.uncertain and port.closed


def test_write_error_is_sanitized_and_still_attempts_hangup():
    modem, port = fake_modem({"ATD10001;": OSError("PRIVATE SERIAL ERROR")})
    with pytest.raises(ModemError) as error:
        modem.call("10001", duration=0.1)
    assert str(error.value) == "serial_write_failed" and error.value.uncertain
    assert "ATH" in port.commands


def test_sms_reader_handles_fragmented_lines_and_preserves_messages():
    response = "\r\n" + cmgl() + "+CMTI: \"SM\",9\r\n" + cmgl(index=2, status=1) + "OK\r\n"
    modem, port = fake_modem({"AT+CMGL=4,1": response}, chunk=1)
    try:
        result = modem.read_sms()
    finally:
        modem.close()
    assert result["segments_count"] == 2 and not result["partial"]
    assert [m["text"] for m in result["messages"]] == ["Привет", "Привет"]
    assert all(not command.startswith(("AT+CMGD", "AT+CMGS")) for command in port.commands)
    assert 'AT+CPMS="SM"' in port.commands and "AT+CMGF=0" in port.commands
    assert "AT+CMGL=4,1" in port.commands  # mode=1 preserves unread/read state.


def test_empty_inbox_is_success_and_invalid_storage_has_no_io():
    modem, port = fake_modem()
    with pytest.raises(ValueError):
        modem.read_sms('SM";ATD10001;')
    assert port.commands == []
    try:
        assert modem.read_sms()["messages"] == []
    finally:
        modem.close()


def test_bad_sms_does_not_hide_valid_messages_or_leak_raw_data():
    lines = ["+CMGL: 4,0,,2", "00FF", *cmgl(index=5).splitlines(), "+CMGL: 6,0,,27"]
    parts, errors = _parse_cmgl(lines)
    assert len(parts) == 1 and parts[0].storage_index == 5
    assert errors == [{"storage_index": 4, "code": "invalid_pdu"},
                      {"storage_index": 6, "code": "missing_pdu"}]
    assert "00FF" not in json.dumps(errors)


def test_complete_call_and_sms_read_are_serialized():
    modem, port = fake_modem()
    results = []
    call = threading.Thread(target=lambda: results.append(modem.call("10001", 0.15)))
    inbox = threading.Thread(target=lambda: results.append(modem.read_sms()))
    call.start()
    assert port.dial_started.wait(1)
    inbox.start()
    call.join(2)
    inbox.join(2)
    modem.close()
    assert not call.is_alive() and not inbox.is_alive() and len(results) == 2
    assert port.commands.index("ATH") < port.commands.index('AT+CPMS="SM"')


def test_request_expired_while_waiting_for_serial_lock_never_dials():
    modem, port = fake_modem()
    with pytest.raises(ModemError, match="queue_expired"):
        modem.call("10001", 0.1, not_after=time.monotonic() - 1)
    assert port.commands == []


class BlockingModem:
    port = "FAKE"

    def __init__(self, fail=False):
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = []
        self.fail, self.closed = fail, False

    def call(self, phone, duration, *, not_after=None):
        self.calls.append(phone)
        self.started.set()
        assert self.release.wait(2)
        if self.fail:
            raise ModemError("command_timeout", uncertain=True)
        return {"delivery": "command_sent", "confirmed": False}

    def request_stop(self):
        self.release.set()

    def close(self):
        self.closed = True


def test_dry_run_and_disabled_mode_never_open_hardware():
    modem = BlockingModem()
    dry = CallQueue(modem, enabled=False, dry_run=True, max_calls=4, ttl=40)
    result = dry.add_call("10001", duration=0.1, branch_id="test")
    assert result["delivery"] == "simulated" and not result["confirmed"]
    assert not modem.calls and not dry.processing
    assert "10001" not in json.dumps(dry.status())
    assert dry.shutdown()
    disabled = CallQueue(BlockingModem(), enabled=False, dry_run=False, max_calls=4, ttl=40)
    assert disabled.add_call("10001", duration=0.1)["code"] == "modem_disabled"
    disabled.shutdown()


@pytest.mark.parametrize("phone", ["", "10001;ATH", "10001\rATD10002;", "#21#", "１２３４５"])
def test_dial_number_cannot_inject_at_commands(phone):
    queue = CallQueue(BlockingModem(), enabled=True, dry_run=False, max_calls=4, ttl=40)
    assert queue.add_call(phone, duration=0.1)["code"] == "invalid_call"
    assert not queue.modem.calls
    queue.shutdown()


def test_queue_coalesces_active_call_and_expires_pending_command():
    modem = BlockingModem()
    queue = CallQueue(modem, enabled=True, dry_run=False, max_calls=2, ttl=40)
    try:
        first = queue.add_call("10001", duration=0.1)
        assert modem.started.wait(1)
        duplicate = queue.add_call("10001", duration=0.1)
        assert duplicate["coalesced"] and duplicate["command_id"] == first["command_id"]
        assert queue.add_call("10002", duration=0.1)["status"] == "success"
        assert queue.add_call("10003", duration=0.1)["code"] == "queue_full"
        with queue.lock:
            queue.queue[0]["deadline"] = time.monotonic() - 1
        modem.release.set()
        queue._thread.join(1)
        assert modem.calls == ["10001"]
        assert {c["status"] for c in queue.status()["recent"]} == {"command_sent", "expired"}
    finally:
        queue.shutdown()


def test_unknown_delivery_is_not_retried_automatically():
    modem = BlockingModem(fail=True)
    queue = CallQueue(modem, enabled=True, dry_run=False, max_calls=4, ttl=40)
    try:
        queue.add_call("10001", duration=0.1)
        assert modem.started.wait(1)
        modem.release.set()
        queue._thread.join(1)
        assert modem.calls == ["10001"]
        assert queue.status()["recent"][0]["status"] == "unknown"
    finally:
        queue.shutdown()


def test_graceful_shutdown_hangs_up_active_call_and_cancels_pending():
    modem, port = fake_modem()
    queue = CallQueue(modem, enabled=True, dry_run=False, max_calls=4, ttl=40)
    queue.add_call("10001", duration=30)
    assert port.dial_started.wait(1)
    queue.add_call("10002", duration=0.1)
    assert queue.shutdown(timeout=1)
    assert "ATH" in port.commands and "ATD10002;" not in port.commands and port.closed
    assert queue.add_call("10003", duration=0.1)["code"] == "stopping"
    assert any(c["status"] == "cancelled" for c in queue.status()["recent"])


def test_shutdown_before_first_command_cannot_be_undone_by_late_request(monkeypatch):
    import sim800c
    monkeypatch.setattr(sim800c, "_queue", None)
    monkeypatch.setattr(sim800c, "_shutdown_requested", False)
    monkeypatch.setenv("SIM800_ENABLED", "true")
    monkeypatch.setenv("GATE_DRY_RUN", "false")
    monkeypatch.setenv("SIM800_PORT", "FAKE")
    assert sim800c.shutdown()
    assert sim800c.open_gate("10001", duration=0.1)["code"] == "stopping"
    assert sim800c.get_call_queue_status()["stopping"] is True
    assert not sim800c._queue.processing and sim800c._queue.modem._serial is None


def test_diagnostics_parses_status_without_dialing_or_private_fields():
    modem, port = fake_modem({
        "AT+CPIN?": "\r\n+CPIN: READY\r\nOK\r\n",
        "AT+CREG?": '\r\n+CREG: 0,5,"PRIVATE","LOCATION"\r\nOK\r\n',
        "AT+CSQ": "\r\n+CSQ: 18,0\r\nOK\r\n",
    })
    try:
        assert modem.diagnostics() == {"state": "ok", "sim": "READY",
            "registration": "roaming", "rssi": 18, "signal_dbm": -77}
        assert not any(c.startswith("ATD") for c in port.commands)
    finally:
        modem.close()


def test_diagnostics_timeout_names_safe_stage(capsys):
    modem, port = fake_modem({"AT": "", "ATQ0V1": ""}, timeout=0.015)
    assert modem.diagnostics() == {"state": "error", "error_code": "command_timeout"}
    assert "stage=AT" in capsys.readouterr().out
    assert port.closed


def test_diagnostics_unknown_signal_is_not_a_fake_dbm():
    modem, port = fake_modem({"AT+CSQ": "\r\n+CSQ: 99,99\r\nOK\r\n"})
    try:
        assert modem.diagnostics()["signal_dbm"] is None
    finally:
        modem.close()


def test_startup_recovers_lost_first_at_without_reopening_port():
    class WakingSerial(FakeSerial):
        def write(self, data):
            if data == b"AT\r" and not self.commands:
                self.commands.append("AT")
                return len(data)  # First command lost during wakeup/autobaud.
            return super().write(data)
    port = WakingSerial()
    modem = Sim800Modem("FAKE", command_timeout=0.015,
                        serial_factory=lambda **kw: port, startup_delay=0)
    try:
        assert modem.diagnostics()["state"] == "ok"
        assert port.commands[:2] == ["AT", "AT"]
        assert not port.closed
        assert not any(c.startswith("ATD") for c in port.commands)
    finally:
        modem.close()


def test_startup_recovers_suppressed_or_numeric_result_codes():
    modem, port = fake_modem({"AT": ""}, timeout=0.015)
    try:
        assert modem.diagnostics()["state"] == "ok"
        assert port.commands[:4] == ["AT", "AT", "AT", "ATQ0V1"]
        assert not any(c.startswith(("ATD", "AT&W", "AT+IPR", "AT&F")) for c in port.commands)
    finally:
        modem.close()


def test_stop_interrupts_startup_grace_without_sending_commands():
    modem, port = fake_modem()
    modem.startup_delay = 10
    worker = threading.Thread(target=modem.diagnostics)
    worker.start()
    modem.request_stop()
    worker.join(1)
    assert not worker.is_alive() and not port.commands
    modem.close()


def test_incoming_ring_is_read_between_status_queries(capsys):
    modem, port = fake_modem()
    try:
        modem.diagnostics()
        capsys.readouterr()
        port.incoming.extend(b'\r\nRING\r\n+CLIP: "PRIVATE",145\r\n')
        for _ in range(4):
            modem.poll_events(timeout=0.01)
        output = capsys.readouterr().out
        assert "notice=RING" in output and "PRIVATE" not in output
        assert not any(c.startswith("ATA") for c in port.commands)
    finally:
        modem.close()


def test_call_state_notice_never_prints_recipient(capsys):
    Sim800Modem._report_notice('+CLCC: 1,0,3,0,0,"PRIVATE",145')
    output = capsys.readouterr().out
    assert "call_state=alerting" in output and "direction=outgoing" in output
    assert "PRIVATE" not in output
