"""Delivery preference shared by branch enrolment and individual gate QR codes."""
from storage.sql_documents import read_document, write_document


def settings(identity, db, branch_id, channel=None):
    key = str(branch_id)
    branches = read_document(db, 'branches.json')
    branch = branches.get(key)
    if branch is None:
        raise ValueError('Филиал не найден')
    connected = bool(branch.get('yclients_enabled', True) and
                     identity.integration_credentials(key, 'chatpush').get('token'))
    if channel is not None:
        if channel not in ('sms', 'chatpush'):
            raise ValueError('Выберите SMS или сервис рассылки')
        if channel == 'chatpush' and not connected:
            raise ValueError('Сначала подключите сервис рассылки')
        branch['qr_code_channel'] = channel
        write_document(db, 'branches.json', branches)
    return {'available': connected, 'channel': 'chatpush' if connected and branch.get('qr_code_channel') == 'chatpush' else 'sms'}
