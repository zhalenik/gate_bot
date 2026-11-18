"""Decode received SMS PDUs, including concatenated UCS-2 messages.

No serial I/O, persistence or SMS deletion takes place in this module.
See docs/SIM800C.md for the supported subset of 3GPP TS 23.040/23.038.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Iterable


class PDUDecodeError(ValueError):
    """Malformed PDU. Error messages deliberately contain no message contents."""


class UnsupportedPDU(PDUDecodeError):
    """A valid-looking encoding or TPDU type outside the supported subset."""


_GSM7 = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_EXT = {10: "\f", 20: "^", 40: "{", 41: "}", 47: "\\",
        60: "[", 61: "~", 62: "]", 64: "|", 101: "€"}


@dataclass(frozen=True)
class SMSPart:
    sender: str
    service_center: str
    timestamp: datetime
    alphabet: str
    dcs: int
    text: str | None
    payload: bytes
    storage_index: int | None = None
    reference: int | None = None
    reference_bits: int | None = None
    total: int = 1
    sequence: int = 1
    extra_headers: bytes = b""


@dataclass(frozen=True)
class SMSMessage:
    sender: str
    timestamp: datetime
    alphabet: str
    text: str | None
    parts: tuple[SMSPart, ...]
    total_parts: int
    missing_parts: tuple[int, ...]
    conflicting_parts: tuple[int, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_parts and not self.conflicting_parts

    def as_dict(self) -> dict:
        """Private inbox data: never put this object into general application logs."""
        return {
            "sender": self.sender, "timestamp": self.timestamp.isoformat(),
            "alphabet": self.alphabet, "text": self.text, "complete": self.complete,
            "total_parts": self.total_parts,
            "received_parts": len({p.sequence for p in self.parts}),
            "missing_parts": list(self.missing_parts),
            "conflicting_parts": list(self.conflicting_parts),
            "storage_indices": [p.storage_index for p in self.parts
                                if p.storage_index is not None],
            "parts": [{"sequence": p.sequence, "text": p.text,
                       "storage_index": p.storage_index,
                       **({"data_hex": p.payload.hex()} if p.alphabet == "8bit" else {})}
                      for p in self.parts],
        }


def _gsm7(data: bytes, count: int, start_bit: int = 0) -> str:
    if count < 0 or start_bit + count * 7 > len(data) * 8:
        raise PDUDecodeError("Truncated GSM-7 user data")
    packed = int.from_bytes(data, "little")
    chars, escaped = [], False
    for i in range(count):
        code = (packed >> (start_bit + i * 7)) & 127
        if escaped:
            chars.append(_EXT.get(code, _GSM7[code] if code != 27 else " "))
            escaped = False
        elif code == 27:
            escaped = True
        else:
            chars.append(_GSM7[code])
    if escaped:
        raise PDUDecodeError("Truncated GSM-7 escape sequence")
    return "".join(chars)


def _address(data: bytes, toa: int, digits: int | None = None) -> str:
    if (toa >> 4) & 7 == 5:
        if digits is None:
            raise UnsupportedPDU("Alphanumeric service centre address")
        return _gsm7(data, digits * 4 // 7)
    nibbles = "".join("0123456789*#abcF"[n] for b in data for n in (b & 15, b >> 4))
    if digits is None:
        nibbles = nibbles.rstrip("F")
    else:
        if digits > len(nibbles) or any(c != "F" for c in nibbles[digits:]):
            raise PDUDecodeError("Invalid address length or padding")
        nibbles = nibbles[:digits]
    if "F" in nibbles:
        raise PDUDecodeError("Invalid address digits")
    return ("+" if (toa >> 4) & 7 == 1 else "") + nibbles


def _timestamp(data: bytes) -> datetime:
    def bcd(value: int) -> int:
        if value >> 4 > 9 or value & 15 > 9:
            raise PDUDecodeError("Invalid SMS timestamp")
        return (value & 15) * 10 + (value >> 4)

    year, month, day, hour, minute, second = (bcd(b) for b in data[:6])
    quarters = bcd(data[6] & 0xF7)
    offset = quarters * 15 * (-1 if data[6] & 8 else 1)
    try:
        return datetime((1900 if year >= 90 else 2000) + year, month, day,
                        hour, minute, second, tzinfo=timezone(timedelta(minutes=offset)))
    except ValueError as exc:
        raise PDUDecodeError("Invalid SMS timestamp") from exc


def _alphabet(dcs: int) -> str:
    if dcs & 0xC0 in (0, 0x40):
        if dcs & 0x20:
            raise UnsupportedPDU("Compressed SMS is not supported")
        coding = (dcs >> 2) & 3
        if coding == 3:
            raise UnsupportedPDU("Reserved SMS alphabet")
        return ("gsm7", "8bit", "ucs2")[coding]
    if dcs & 0xF0 in (0xC0, 0xD0):
        return "gsm7"
    if dcs & 0xF0 == 0xE0:
        return "ucs2"
    if dcs & 0xF0 == 0xF0:
        return "8bit" if dcs & 4 else "gsm7"
    raise UnsupportedPDU("Unsupported SMS data coding scheme")


def decode_deliver(pdu_hex: str, storage_index: int | None = None) -> SMSPart:
    """Decode one SMS-DELIVER including its leading SMSC-length octet.

    UCS-2 is decoded as big-endian UTF-16, not the terminal's locale/UTF-8.
    UDH bytes are removed BEFORE decoding; GSM-7 fill bits are respected.
    """
    compact = re.sub(r"\s+", "", pdu_hex)
    if not compact or len(compact) % 2 or len(compact) > 1024 or not re.fullmatch(r"[\da-fA-F]+", compact):
        raise PDUDecodeError("Invalid PDU hex")
    data, pos = bytes.fromhex(compact), 0

    def take(size: int) -> bytes:
        nonlocal pos
        if size < 0 or pos + size > len(data):
            raise PDUDecodeError("Truncated PDU")
        value, pos = data[pos:pos + size], pos + size
        return value

    def byte() -> int:
        return take(1)[0]

    smsc = take(byte())
    service_center = _address(smsc[1:], smsc[0]) if smsc else ""
    first = byte()
    if first & 3 != 0:
        raise UnsupportedPDU("Only received SMS-DELIVER PDUs are decoded")
    address_len, toa = byte(), byte()
    if not 1 <= address_len <= 20:
        raise PDUDecodeError("Invalid sender address length")
    sender = _address(take((address_len + 1) // 2), toa, address_len)
    byte()  # TP-PID
    dcs = byte()
    alphabet = _alphabet(dcs)
    timestamp = _timestamp(take(7))
    udl = byte()
    length = (udl * 7 + 7) // 8 if alphabet == "gsm7" else udl
    if length > 140:
        raise PDUDecodeError("SMS user data exceeds 140 octets")
    ud = take(length)
    if pos != len(data):
        raise PDUDecodeError("Unexpected bytes after SMS user data")
    header_len, concat, extras = 0, None, bytearray()
    if first & 0x40:
        if not ud or ud[0] + 1 > len(ud):
            raise PDUDecodeError("Truncated SMS user data header")
        header_len, at = ud[0] + 1, 1
        while at < header_len:
            if at + 2 > header_len:
                raise PDUDecodeError("Truncated SMS information element")
            identifier, size = ud[at], ud[at + 1]
            end = at + 2 + size
            if end > header_len:
                raise PDUDecodeError("Truncated SMS information element")
            value = ud[at + 2:end]
            if identifier in (0, 8):
                expected = 3 if identifier == 0 else 4
                if size != expected or concat is not None:
                    raise PDUDecodeError("Invalid or duplicate concatenation header")
                ref = int.from_bytes(value[:-2], "big")
                total, seq = value[-2:]
                if not 1 <= seq <= total:
                    raise PDUDecodeError("Invalid SMS segment number")
                concat = (ref, 8 if identifier == 0 else 16, total, seq)
            else:
                if identifier in (0x24, 0x25) and alphabet == "gsm7":
                    raise UnsupportedPDU("National-language shift tables are not supported")
                extras.extend(ud[at:end])
            at = end
    payload = ud[header_len:]
    if alphabet == "gsm7":
        header_septets = (header_len * 8 + 6) // 7
        text = _gsm7(ud, udl - header_septets, header_septets * 7)
    elif alphabet == "ucs2":
        try:
            text = payload.decode("utf-16-be")
        except UnicodeDecodeError as exc:
            raise PDUDecodeError("Invalid UCS-2 user data") from exc
    else:
        text = None  # Binary data has no implicit character set.
    reference, bits, total, seq = concat or (None, None, 1, 1)
    return SMSPart(sender, service_center, timestamp, alphabet, dcs, text, payload,
                   storage_index, reference, bits, total, seq, bytes(extras))


def assemble_messages(parts: Iterable[SMSPart], max_span: timedelta = timedelta(hours=24),
                      *, require_same_smsc: bool = False) -> list[SMSMessage]:
    """Join unordered segments. Never claim incomplete/conflicting sets are whole.

    A sender can reuse a concatenation reference. Different senders, encodings,
    UDHs and widely separated timestamps never get joined. A repeated sequence
    with a NEW service-centre timestamp starts another transmission, even when
    its text is identical. Only same-timestamp copies can be retransmissions.
    TS 23.040 allows segments to arrive through different SMSCs; callers can
    opt into stricter SMSC separation for a specific deployment.
    """
    if max_span <= timedelta(0):
        raise ValueError("max_span must be positive")
    groups: dict[tuple, list[list[SMSPart]]] = {}
    singletons = []
    for part in sorted(parts, key=lambda p: p.timestamp):
        if part.reference is None:
            singletons.append([part])
            continue
        key = (part.sender, part.service_center if require_same_smsc else None,
               part.reference, part.reference_bits,
               part.total, part.dcs, part.extra_headers)
        batches = groups.setdefault(key, [])
        previous = [p for p in batches[-1] if p.sequence == part.sequence] if batches else []
        repeated_sequence = previous and all(p.timestamp != part.timestamp for p in previous)
        if not batches or part.timestamp - batches[-1][0].timestamp > max_span or repeated_sequence:
            batches.append([])
        batches[-1].append(part)
    result = []
    for batch in singletons + [batch for batches in groups.values() for batch in batches]:
        ordered = sorted(batch, key=lambda p: (p.sequence, p.timestamp, p.storage_index or 0))
        by_seq: dict[int, SMSPart] = {}
        conflicts = set()
        for part in ordered:
            previous = by_seq.setdefault(part.sequence, part)
            if (previous.text, previous.payload) != (part.text, part.payload):
                conflicts.add(part.sequence)
        first = ordered[0]
        missing = tuple(i for i in range(1, first.total + 1) if i not in by_seq)
        text = None
        if not missing and not conflicts and first.alphabet != "8bit":
            text = "".join(by_seq[i].text or "" for i in range(1, first.total + 1))
        result.append(SMSMessage(first.sender, min(p.timestamp for p in ordered),
                                 first.alphabet, text, tuple(ordered), first.total,
                                 missing, tuple(sorted(conflicts))))
    return sorted(result, key=lambda m: m.timestamp)
