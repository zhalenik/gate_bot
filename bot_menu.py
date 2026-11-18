"""Shared timing for one editable bot menu; contains no platform wire schema."""
import hashlib
import json
import time
from timeutil import parse_datetime


def fingerprint(text, keyboard):
    return hashlib.sha256(json.dumps([text, keyboard], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def next_refresh(service, platform, user_id):
    stamp = time.time()
    due = stamp + 60
    for section in service.menu_for_identity(platform, user_id):
        if section.get('retry_after'):
            due = min(due, stamp + section['retry_after'] + .2)
        if section.get('active') and section.get('access_expires'):
            due = min(due, max(stamp + .5, parse_datetime(section['access_expires']).timestamp() + .1))
    return due
