"""Identify the consenting user from the current YCLIENTS Marketplace installation."""
from datetime import datetime
import re


def _id(value):
    if type(value) not in (int, str):
        return None
    value = str(value)
    return value if re.fullmatch(r'[1-9][0-9]{0,14}', value) else None


def current_installer(data, branch_id, application_id):
    """Return only a Marketplace actor for the latest activation of this exact app/salon."""
    branch_id, application_id = _id(branch_id), _id(application_id)
    if not branch_id or not application_id or not isinstance(data, dict):
        return None
    connection = data.get('connection_status')
    if (not isinstance(connection, dict) or connection.get('status') != 'active'
            or _id(connection.get('salon_id')) != branch_id
            or _id(connection.get('marketplace_application_id')) != application_id):
        return None

    logs = data.get('logs')
    if not isinstance(logs, list) or not logs:
        return None
    by_time = {}
    for row in logs:
        if not isinstance(row, dict):
            return None
        if ('salon_id' in row and _id(row['salon_id']) != branch_id
                or 'marketplace_application_id' in row and _id(row['marketplace_application_id']) != application_id):
            return None
        stamp = row.get('changed_at')
        if not isinstance(stamp, str) or not re.fullmatch(
                r'[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:?[0-9]{2})?', stamp):
            return None
        try:
            changed = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        by_time.setdefault(changed, []).append(row)
    try:
        times = sorted(by_time, reverse=True)
    except TypeError:  # Mixed local and offset-aware timestamps cannot be ordered.
        return None
    latest = by_time[times[0]]
    activations = [row for row in latest if row.get('status_to') == 'active']
    if len(activations) != 1:
        return None
    activation = activations[0]
    if (len(latest) == 1 and activation.get('source') == 'marketplace'
            and activation.get('status_from') in ('uninstalled', 'pending')):
        return _id(activation.get('user_id'))
    if activation.get('source') != 'partner_api' or activation.get('status_from') != 'pending':
        return None

    # The callback's own user is not the person who granted access. Their
    # consent immediately precedes the callback, sometimes within the same second.
    preceding = [row for row in latest if row is not activation]
    if not preceding and len(times) > 1:
        preceding = by_time[times[1]]
    if len(preceding) != 1:
        return None
    consent = preceding[0]
    if (consent.get('source') == 'marketplace' and consent.get('status_from') == 'uninstalled'
            and consent.get('status_to') == 'pending'):
        return _id(consent.get('user_id'))
    return None
