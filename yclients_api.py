"""Strictly read-only YCLIENTS integration.

Policy is enforced at the request boundary; a configuration flag cannot enable
POST/PUT/PATCH/DELETE, SMS, registration, login or webhook provisioning.
Official contract: https://developers.yclients.com/ru/
"""
from datetime import timedelta
import os
import re
import threading
import time
from urllib.parse import urlparse
import requests
from storage import config
from branch_manager import BranchManager
from timeutil import now, end_of_day, parse_datetime, timezone
import logger


class YClientsError(RuntimeError):
    pass


class YClientsReadOnlyError(YClientsError):
    pass


_rate_lock = threading.Lock()
_next_read = 0.0


def _pace():
    global _next_read
    with _rate_lock:
        delay = max(0, _next_read - time.monotonic())
        if delay:
            time.sleep(delay)
        _next_read = time.monotonic() + 0.35  # Below both 5/sec and 200/min limits.


class YClientsClient:
    def __init__(self, session=None, base_url=None, branches=None, pace=_pace):
        self.session = session or requests.Session()
        self.base_url = (base_url or config.YC_BASE_URL).rstrip('/')
        url = urlparse(self.base_url)
        if url.scheme != 'https' or url.hostname not in {'api.yclients.ru', 'api.yclients.com'} or url.path != '/api/v1' or url.query or url.fragment or url.username is not None or url.password is not None or url.port not in {None, 443}:
            raise ValueError('YCLIENTS requires its official HTTPS API host')
        self.branches = branches or BranchManager()
        self.pace = pace

    def _credentials(self, branch_id):
        branch = self.branches.load_branches().get(str(branch_id))
        if not branch:
            raise YClientsError('Unknown branch')
        env_name = branch.get('yclients_user_token_env', f'YC_USER_TOKEN_{branch_id}')
        if not re.fullmatch(r'[A-Z][A-Z0-9_]*', str(env_name)):
            raise YClientsError('Invalid credential reference')
        user_token = os.getenv(env_name, '')
        if not user_token and str(config.YC_COMPANY_ID) == str(branch_id):
            user_token = config.YC_USER_TOKEN
        if not config.YC_PARTNER_TOKEN or not user_token:
            raise YClientsError('Missing credentials for branch')
        return {'Accept': 'application/vnd.yclients.v2+json', 'Authorization': f'Bearer {config.YC_PARTNER_TOKEN}, User {user_token}'}

    def request(self, method, path, branch_id, params=None):
        if method.upper() != 'GET':
            raise YClientsReadOnlyError('YCLIENTS is read-only: only allowlisted GET operations are permitted')
        branch = str(branch_id)
        if not branch.isdigit() or not re.fullmatch(rf'(records/{re.escape(branch)}|client/{re.escape(branch)}/[0-9]+|staff/{re.escape(branch)})', path):
            raise YClientsReadOnlyError('This YCLIENTS endpoint is not allowed')
        if not config.YCLIENTS_ENABLED:
            raise YClientsError('YCLIENTS integration is disabled')
        headers = self._credentials(branch_id)
        try:
            self.pace()
            response = self.session.get(self.base_url + '/' + path, headers=headers, params=params or {}, timeout=(5, 20), allow_redirects=False)
            if response.status_code != 200:
                raise YClientsError(f'YCLIENTS returned HTTP {response.status_code}')
            payload = response.json()
        except requests.RequestException as error:
            raise YClientsError('YCLIENTS connection failed: ' + type(error).__name__) from None
        except ValueError:
            raise YClientsError('YCLIENTS returned invalid JSON') from None
        if not isinstance(payload, dict) or payload.get('success') is not True:
            raise YClientsError('YCLIENTS rejected the read request')
        logger.event('CRM_READ', branch_id=branch_id, details={'resource': path.split('/')[0], 'outcome': 'success'})
        return payload

    def records_for_day(self, branch_id, day, page_size=100):
        records, seen = [], set()
        expected_total = None
        total_present = None
        for page in range(1, 1001):
            payload = self.request('GET', f'records/{branch_id}', branch_id, {'start_date': str(day), 'end_date': str(day), 'page': page, 'count': page_size, 'with_deleted': 0})
            items = payload.get('data')
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise YClientsError('Invalid records collection')
            for item in items:
                if item.get('company_id') is not None and str(item['company_id']) != str(branch_id):
                    raise YClientsError('Cross-branch record in CRM response')
                identity = item.get('id')
                if identity is None:
                    raise YClientsError('Missing record identity')
                if str(identity) in seen:
                    raise YClientsError('CRM pagination repeated a record; incomplete snapshot')
                seen.add(str(identity))
                records.append(item)
            meta = payload.get('meta') or {}
            total = meta.get('total_count') if isinstance(meta, dict) else None
            present = total is not None
            if total_present is None:
                total_present = present
            elif present != total_present:
                raise YClientsError('Pagination metadata changed; snapshot not applied')
            if present:
                if isinstance(total, bool) or not isinstance(total, (str, int)) or not str(total).isdigit():
                    raise YClientsError('Invalid pagination metadata')
                total = int(total)
                if expected_total is None:
                    expected_total = total
                elif total != expected_total:
                    raise YClientsError('CRM collection changed during pagination; snapshot not applied')
                if len(records) > total:
                    raise YClientsError('CRM total contradicts returned records')
                if len(records) == total:
                    return records
                if not items:
                    raise YClientsError('CRM returned an incomplete page')
            elif len(items) < page_size:
                return records
        raise YClientsError('Pagination limit exceeded; no changes applied')

    def get_client(self, branch_id, client_id):
        if not str(client_id).isdigit():
            raise YClientsError('Invalid client identity')
        data = self.request('GET', f'client/{branch_id}/{client_id}', branch_id).get('data')
        if not isinstance(data, dict) or str(data.get('id')) != str(client_id):
            raise YClientsError('Invalid client identity in response')
        if data.get('company_id') is not None and str(data['company_id']) != str(branch_id):
            raise YClientsError('Cross-branch client in response')
        return {'id': client_id, 'name': data.get('name', ''), 'phone': data.get('phone', ''), 'email': data.get('email', '')}


def eligible_clients(records, branch_id, at=None, branch_timezone=None):
    at = (at or now(branch_timezone)).astimezone(timezone(branch_timezone))
    clients = {}
    for record in records:
        if record.get('deleted') in {True, 1, '1'} or str(record.get('attendance', '')) == '-1':
            continue
        if record.get('company_id') is not None and str(record['company_id']) != str(branch_id):
            raise YClientsError('Cross-branch record')
        client = record.get('client')
        if not isinstance(client, dict) or not str(client.get('id', '')).isdigit():
            continue
        try:
            visit = parse_datetime(record.get('datetime') or record.get('date'), branch_timezone).astimezone(timezone(branch_timezone))
        except (ValueError, TypeError):
            raise YClientsError('Invalid appointment date; no changes applied') from None
        if visit.date() == at.date() and visit - timedelta(hours=config.ACCESS_BEFORE_VISIT_HOURS) <= at <= end_of_day(visit, branch_timezone):
            clients[str(client['id'])] = {key: client[key] for key in ('id', 'name', 'phone', 'email') if key in client}
    return clients


def get_clients_with_appointments_today(branch_id):
    branch = BranchManager().get_branch(branch_id)
    if not branch:
        raise YClientsError('Unknown branch')
    client = YClientsClient()
    return list(eligible_clients(client.records_for_day(branch_id, now(branch.timezone).date()), branch_id, branch_timezone=branch.timezone))


def check_client_appointments(client_id, branch_id):
    return str(client_id) in get_clients_with_appointments_today(branch_id)


def get_client_data(company_id, client_id):
    return YClientsClient().get_client(company_id, client_id)


def send_message(*args, **kwargs):
    raise YClientsReadOnlyError('Sending messages through YCLIENTS is prohibited')
