"""Durable time of a pass change, independent of contact and notification edits."""
from datetime import timezone

from timeutil import now, parse_datetime


def _moment(value):
    if value in (None, ''):
        return None
    try:
        return parse_datetime(value).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def changed_at(client):
    """Old records retain their known creation time; unknown history stays unknown."""
    value=_moment(client.get('pass_changed_at')) or _moment(client.get('created'))
    return value.isoformat() if value else None


def _rights(client):
    expiry=client.get('access_expires')
    parsed=_moment(expiry)
    return (str(client.get('branch_id') or ''), client.get('gate_access') is True,
            bool(client.get('admin_blocked')), client.get('admin_role_active') is True,
            parsed.isoformat() if parsed else expiry or None,
            client.get('access_source') or 'manual', bool(client.get('is_temporary')))


def stamp_pass_changes(links, previous, *, at=None):
    current=_moment(at or now())
    for token, client in links.items():
        if not isinstance(client,dict):
            continue
        old=previous.get(token)
        if not isinstance(old,dict):
            # Importing historical clients must not place them at the top as
            # if an administrator had just issued fresh access.
            value=changed_at(client)
            if value:client['pass_changed_at']=value
            continue
        if _rights(client)!=_rights(old):
            changed=current
            expiry=_moment(old.get('access_expires'))
            # The timer may catch an expiry later. Record when access actually
            # expired, not when the next successful scheduler cycle ran.
            if (old.get('gate_access') is True and client.get('gate_access') is False and
                    not old.get('admin_blocked') and not client.get('admin_blocked') and
                    old.get('admin_role_active') is not True and client.get('admin_role_active') is not True and
                    expiry is not None and expiry<=current and
                    _moment(client.get('access_expires'))==expiry and
                    _rights({**old,'gate_access':False})==_rights(client)):
                changed=expiry
            client['pass_changed_at']=changed.isoformat()
        elif 'pass_changed_at' in old:
            client['pass_changed_at']=old['pass_changed_at']
        else:
            client.pop('pass_changed_at',None)
