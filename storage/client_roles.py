"""Directory roles. Administrator privileges belong to identity accounts."""

def directory_role(card, membership=None):
    if (membership and membership.get('is_admin')) or (membership is None and card.get('admin_role_active')):
        return 'admin'
    if card.get('is_temporary'):
        return 'guest'
    return 'employee' if card.get('client_role') == 'employee' else 'client'
