"""Composition of product services, shared by HTTP, bots and the sole runtime."""
from pathlib import Path
from analytics_service import AnalyticsService
from billing_service import BillingService
from identity_service import IdentityService
from product_notifications import ProductNotifications
from storage.product_store import ProductStore
from storage.sql_documents import is_sql_active
from telephony_store import TelephonyStore
from support_service import SupportService
from branch_desk import BranchDesk


class ApplicationServices:
    def __init__(self, links, branches):
        self.links, self.branches = links, branches
        directory = Path(links.links_file).parent
        self.store = ProductStore(directory / 'application.sqlite3')
        self.billing = BillingService(self.store)
        self.analytics = AnalyticsService(self.store)
        self.identity = IdentityService(self.store)
        from pwa_service import PwaService
        self.pwa = PwaService(self.identity)
        from yclients_client_sync import YClientsClientSync
        from yclients_client_sync_jobs import YClientsClientSyncJobs
        self.yclients_client_sync = YClientsClientSync(self.identity, branches)
        from branch_join import BranchJoin
        self.branch_join = BranchJoin(self.identity, links, self.yclients_client_sync)
        self.yclients_client_jobs = YClientsClientSyncJobs(self.identity, self.yclients_client_sync)
        if is_sql_active(directory):
            self.identity.import_legacy()
            self.identity.activate_pending_manual_registrations(self.billing)
        self.notifications = ProductNotifications(self.identity, links, branches, self.billing,
                                                  telephony=TelephonyStore(directory / 'telephony.json'), pwa=self.pwa)
        self.identity.sender = self.notifications.send
        self.identity.event_sink = self.notifications.event
        self.identity.delivery_status = self.notifications.effect_status
        self.identity.welcome_sink = self.notifications.welcome
        self.support = SupportService(self.identity)
        self.desk = BranchDesk(self.identity, links, branches, self.notifications)
        self.notifications.support = self.support
        self.gate_qr = None

    def provider(self, auth, access=None):
        from yclients_connection import YClientsConnector
        def check_yclients(branch, credentials):
            return YClientsConnector().check_permissions(branch, credentials)
        if access is not None and self.gate_qr is None:
            from gate_qr_service import GateQRService
            self.gate_qr = GateQRService(self.identity, self.links, self.branches, access)
        return {'service': self.identity, 'auth': auth, 'links': self.links, 'branches': self.branches,
                'billing': self.billing, 'analytics': self.analytics,
                'gate_qr': self.gate_qr, 'branch_join': self.branch_join, 'support': self.support,
                'pwa': self.pwa, 'desk': self.desk, 'access': access,
                'notifications': self.notifications, 'check_yclients': check_yclients,
                'yclients_client_sync': self.yclients_client_sync, 'yclients_client_jobs': self.yclients_client_jobs,
                'authorize_yclients': lambda login, password, code=None, challenge=None:
                    YClientsConnector().authorize(login, password, code, challenge)}

    def attach(self, access):
        access.billing, access.identity, access.event_sink = self.billing, self.identity, self.notifications.event
        access.support = self.support
        access.desk = self.desk
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS access_command_routes(command_id TEXT PRIMARY KEY, branch_id TEXT NOT NULL, button_id TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS access_request_auth(request_id TEXT PRIMARY KEY, token TEXT NOT NULL, platform TEXT, user_id TEXT)')

    def index_clients(self):
        for token, data in self.links.load_links().items():
            created = data.get('created')
            if not created:
                continue  # Unknown historical timestamps are not new clients today.
            try:
                self.analytics.record_client(token, data['branch_id'], data.get('person_id') or token,
                                             created_at=created, source='migration')
            except (ValueError, TypeError):
                continue
