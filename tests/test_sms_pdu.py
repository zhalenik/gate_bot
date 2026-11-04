"""Synthetic wire-format fixtures; no production phone numbers or messages."""
from dataclasses import replace
from datetime import timedelta

import pytest

from sms_pdu import PDUDecodeError, UnsupportedPDU, assemble_messages, decode_deliver


# SMSC omitted; SMS-DELIVER from synthetic +12345; 2026-11-05 12:34:56 +03.
HEADER = "000405912143F5000862115021436521"
GOLDEN_CYRILLIC = HEADER + "0C041F04400438043204350442"


def deliver(text="", *, udh=b"", dcs=8, user_data=None, udl=None,
            timestamp="62115021436521", sender="05912143F5"):
    """Build only the enclosing wire fields; assertions verify independent values."""
    body = user_data if user_data is not None else udh + text.encode("utf-16-be")
    length = len(body) if udl is None else udl
    return ("00" + ("44" if udh else "04") + sender + "00" + f"{dcs:02X}" +
            timestamp + f"{length:02X}" + body.hex())


def test_golden_cyrillic_big_endian_and_timestamp():
    part = decode_deliver(GOLDEN_CYRILLIC, storage_index=7)
    assert part.text == "Привет"
    assert part.sender == "+12345"
    assert part.timestamp.isoformat() == "2026-11-05T12:34:56+03:00"
    assert part.storage_index == 7
    assert assemble_messages([part])[0].complete


@pytest.mark.parametrize("bits,first_text,second_text", [(8, "Я" * 67, " конец"), (16, "Ж" * 66, " продолжение")])
def test_long_cyrillic_headers_and_out_of_order_parts(bits, first_text, second_text):
    prefix = bytes.fromhex("0500037A02" if bits == 8 else "060804123402")
    first = decode_deliver(deliver(first_text, udh=prefix + b"\x01"), 8)
    second = decode_deliver(deliver(second_text, udh=prefix + b"\x02"), 2)
    message = assemble_messages([second, first])[0]
    assert message.text == first_text + second_text
    assert message.complete and not message.missing_parts
    assert first.reference_bits == bits
    assert first.reference == (0x7A if bits == 8 else 0x1234)
    assert [p.storage_index for p in message.parts] == [8, 2]


def test_70_cyrillic_characters_fit_single_sms():
    part = decode_deliver(deliver("Ю" * 70))
    assert part.text == "Ю" * 70 and part.total == 1
    with pytest.raises(PDUDecodeError):
        decode_deliver(deliver("Ю" * 71))


def test_missing_and_conflicting_parts_are_never_reported_as_complete():
    first = decode_deliver(deliver("Начало", udh=bytes.fromhex("0500037A0301")), 1)
    third = decode_deliver(deliver("Конец", udh=bytes.fromhex("0500037A0303")), 3)
    partial = assemble_messages([third, first])[0]
    assert partial.missing_parts == (2,) and not partial.complete and partial.text is None
    second = decode_deliver(deliver("Середина", udh=bytes.fromhex("0500037A0302")), 2)
    conflict = replace(second, payload=b"different", text="Другое", storage_index=4)
    invalid = assemble_messages([first, second, third, conflict])[0]
    assert invalid.conflicting_parts == (2,) and invalid.text is None and not invalid.complete


def test_identical_retransmission_is_joined_once_but_indices_are_retained():
    p1 = decode_deliver(deliver("До", udh=bytes.fromhex("0500037A0201")), 1)
    p2 = decode_deliver(deliver("После", udh=bytes.fromhex("0500037A0202")), 2)
    message = assemble_messages([p1, p2, replace(p1, storage_index=9)])[0]
    assert message.text == "ДоПосле" and message.complete
    assert message.as_dict()["storage_indices"] == [1, 9, 2]


def test_reused_reference_is_separated_by_sender_and_time_window():
    first = decode_deliver(deliver("А", udh=bytes.fromhex("0500037A0201")))
    second = decode_deliver(deliver("Б", udh=bytes.fromhex("0500037A0202")))
    other_sender = replace(second, sender="+54321")
    much_later = replace(second, timestamp=second.timestamp + timedelta(days=2))
    messages = assemble_messages([first, other_sender, much_later])
    assert len(messages) == 3 and all(not m.complete for m in messages)


def test_segments_can_arrive_via_different_smscs_per_3gpp():
    p1 = decode_deliver(deliver("А", udh=bytes.fromhex("0500037A0201")))
    p2 = decode_deliver(deliver("Б", udh=bytes.fromhex("0500037A0202")))
    p2 = replace(p2, service_center="+98765")
    assert assemble_messages([p1, p2])[0].text == "АБ"
    assert len(assemble_messages([p1, p2], require_same_smsc=True)) == 2


def test_multiple_information_elements_do_not_become_cyrillic_characters():
    # Concatenation IE plus 16-bit application port IE; UDH has 12 bytes total.
    header = bytes.fromhex("0B00037A01010504C000C001")
    message = decode_deliver(deliver("Текст", udh=header))
    assert message.text == "Текст" and message.extra_headers.hex() == "0504c000c001"


def test_gsm7_golden_vector():
    # Known packed GSM-7 value for "hellohello", independent of an encoder.
    pdu = deliver(dcs=0, user_data=bytes.fromhex("E8329BFD4697D9EC37"), udl=10)
    assert decode_deliver(pdu).text == "hellohello"


def test_gsm7_udh_fill_bit_and_extension_characters():
    header = bytes.fromhex("0500037A0101")
    septets = [72, 101, 108, 108, 111, 32, 27, 20, 32, 27, 101]  # Hello ^ €
    packed = int.from_bytes(header, "little")
    for i, value in enumerate(septets):
        packed |= value << (49 + i * 7)  # 48 header bits + 1 fill bit.
    user_data = packed.to_bytes(16, "little")
    pdu = deliver(dcs=0, udh=header, user_data=user_data, udl=18)
    assert decode_deliver(pdu).text == "Hello ^ €"


def test_alphanumeric_sender_and_negative_timezone():
    codes = [66, 65, 78, 75]  # BANK
    address = sum(code << (7 * i) for i, code in enumerate(codes)).to_bytes(4, "little")
    pdu = deliver("Код", sender="07D0" + address.hex(), timestamp="62115021436529")
    part = decode_deliver(pdu)
    assert part.sender == "BANK"
    assert part.timestamp.utcoffset() == timedelta(hours=-3)


def test_empty_and_binary_payloads_remain_distinct():
    assert decode_deliver(deliver()).text == ""
    binary = decode_deliver(deliver(dcs=4, user_data=b"\x00\xff"))
    assert binary.text is None and binary.payload == b"\x00\xff"
    assert assemble_messages([binary])[0].as_dict()["parts"][0]["data_hex"] == "00ff"


@pytest.mark.parametrize("pdu", ["", "0", "QQ", GOLDEN_CYRILLIC[:-2], GOLDEN_CYRILLIC + "00",
                                  deliver(udh=b"\xff"), deliver(udh=bytes.fromhex("0500037A0200")),
                                  deliver(user_data=b"\x04"), deliver(timestamp="62FF5021436521")])
def test_malformed_pdus_raise_explicit_errors(pdu):
    with pytest.raises(PDUDecodeError):
        decode_deliver(pdu)


@pytest.mark.parametrize("dcs", [0x20, 0x0C, 0x80])
def test_unsupported_compression_or_coding_does_not_guess_text(dcs):
    with pytest.raises(UnsupportedPDU):
        decode_deliver(deliver(dcs=dcs))


def test_submit_message_is_not_misread_as_incoming_deliver():
    with pytest.raises(UnsupportedPDU):
        decode_deliver("0001" + GOLDEN_CYRILLIC[4:])
