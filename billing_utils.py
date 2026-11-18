"""Shared billing date and identifier validation without service imports."""
from calendar import monthrange
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo


def epoch(value=None):
    if value is None:
        return datetime.now(timezone.utc).timestamp()
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError('Время должно содержать часовой пояс')
        return value.timestamp()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Некорректное время')
    return float(value)

def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None

def month_at(anchor, months, zone):
    value = datetime.fromtimestamp(anchor, ZoneInfo(zone))
    year, month = divmod(value.year * 12 + value.month - 1 + months, 12)
    return value.replace(year=year, month=month + 1,
        day=min(value.day, monthrange(year, month + 1)[1])).timestamp()

def identifier(value, label):
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError('Некорректное поле: ' + label)
    value = str(value)
    if not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise ValueError('Некорректное поле: ' + label)
    return value
