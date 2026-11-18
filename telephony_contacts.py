"""Names from the local client snapshot. No network calls or identity linking."""
from telephony_store import chat_key


def contact_names(clients):
    names = {}
    for client in clients.values():
        phone, name = client.get('phone'), str(client.get('user_name') or '').strip()
        if not phone or not name:
            continue
        names.setdefault(chat_key(phone), set()).add(name)
    # Shared numbers can belong to several clients/branches: don't invent a match.
    return {key: ' / '.join(sorted(values, key=str.casefold)) for key, values in names.items()}
