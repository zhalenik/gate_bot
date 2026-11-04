"""Aware business time, including deterministic handling of legacy naive dates."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os

def timezone(name=None):
    return ZoneInfo(name or os.getenv("GATEBOT_TIMEZONE", "Europe/Moscow"))

def now(tz=None):
    return datetime.now(timezone(tz))

def parse_datetime(value, tz=None):
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=timezone(tz)) if result.tzinfo is None else result

def end_of_day(value=None, tz=None):
    value = value or now(tz)
    return value.astimezone(timezone(tz)).replace(hour=23, minute=59, second=59, microsecond=999999)
