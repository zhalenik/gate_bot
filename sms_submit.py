"""SMS-SUBMIT PDU encoding; no I/O. Unicode, 16-bit concatenation reference."""
import re
import secrets


def encode_submit(phone, text, reference=None):
    if not isinstance(phone, str) or not re.fullmatch(r'\+?[1-9][0-9]{6,14}', phone):
        raise ValueError('Введите телефон с кодом страны')
    if not isinstance(text, str) or not text.strip() or len(text) > 17850:
        raise ValueError('Введите текст SMS; максимум 255 частей')
    try:
        encoded = text.encode('utf-16-be')
    except UnicodeEncodeError:
        raise ValueError('Некорректный Unicode в SMS') from None
    chunks = [encoded]
    if len(encoded) > 140:
        chunks, current = [], bytearray()
        for char in text:
            unit = char.encode('utf-16-be')
            if len(current) + len(unit) > 132:  # 140 minus 7-byte UDH, even UTF-16 length
                chunks.append(bytes(current))
                current.clear()
            current.extend(unit)
        chunks.append(bytes(current))
    if len(chunks) > 255:
        raise ValueError('SMS превышает предел протокола: 255 частей')
    reference = secrets.randbelow(65536) if reference is None else reference
    if type(reference) is not int or not 0 <= reference <= 65535:
        raise ValueError('Invalid concatenation reference')
    digits = phone.lstrip('+')
    padded = digits + ('F' if len(digits) % 2 else '')
    address = bytes.fromhex(''.join(padded[i+1] + padded[i] for i in range(0, len(padded), 2)))
    result = []
    for sequence, chunk in enumerate(chunks, 1):
        multi = len(chunks) > 1
        header = bytes([6, 8, 4, reference >> 8, reference & 255, len(chunks), sequence]) if multi else b''
        ud = header + chunk
        tpdu = bytes([0x41 if multi else 1, 0, len(digits), 0x91 if phone.startswith('+') else 0x81]) + address + bytes([0, 8, len(ud)]) + ud
        result.append({'pdu': (b'\x00' + tpdu).hex().upper(), 'tpdu_length': len(tpdu),
                       'sequence': sequence, 'total': len(chunks)})
    return result
