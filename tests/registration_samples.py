"""Select the currently displayed document editions as an offline test participant."""
from legal_documents import ADMIN_DOCS, CLIENT_DOCS, current


def agreements(identity, *, client=False):
    with identity.product.connect() as db:
        return {item['kind']: item['version'] for item in current(db, CLIENT_DOCS if client else ADMIN_DOCS)}
