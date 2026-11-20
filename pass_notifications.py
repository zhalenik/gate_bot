"""Delivery eligibility for client pass notices, including previously queued events."""
from timeutil import now


def timely_grant(event, client, *, at=None):
    if not client or event not in {'granted', 'access_granted'}:
        return False
    if client.get('access_source') != 'appointment':
        return True
    cutoff = client.get('appointment_notice_until')
    return type(cutoff) in (int, float) and (now().timestamp() if at is None else at) <= cutoff
