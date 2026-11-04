"""ChatPush integration boundary; the supplied project contains no real adapter.

Never report success for a stub. Provider URL, contract and tenant credentials
must be verified before an outbound implementation is enabled.
"""
def send_link(phone, link, branch_id=None, **kwargs):
    return {'status': 'error', 'skipped': True, 'message': 'ChatPush не настроен: требуется действующий API-контракт провайдера.'}


def send_message(*args, **kwargs):
    return send_link('', '', **kwargs)
