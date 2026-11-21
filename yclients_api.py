"""Strictly read-only YCLIENTS integration.

Policy is enforced at the request boundary; a configuration flag cannot enable
POST/PUT/PATCH/DELETE, SMS, registration, login or webhook provisioning.
Official contract: https://developers.yclients.com/ru/
"""
from datetime import datetime, timedelta
import re
import threading
import time
import requests
from storage import config
from branch_manager import BranchManager
from timeutil import now, pass_expiry, parse_datetime, timezone
import logger
from yclients_urls import normalize_yclients_base_url


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
    def __init__(self, session=None, base_url=None, branches=None, pace=_pace, credential_provider=None):
        self.session = session or requests.Session()
        self.base_url = normalize_yclients_base_url(base_url or config.YC_BASE_URL)
        self.branches = branches or BranchManager()
        self.pace = pace
        self.credential_provider = credential_provider

    def _credentials(self, branch_id):
        branch = self.branches.load_branches().get(str(branch_id))
        if not branch:
            raise YClientsError('Unknown branch')
        if branch.get('yclients_enabled',True) is False:
            raise YClientsReadOnlyError('YCLIENTS is not available for a standalone branch')
        if branch.get('yclients_marketplace_state') in {'uninstalled', 'frozen'}:
            raise YClientsError('YCLIENTS marketplace connection is inactive')
        saved = self.credential_provider(branch_id) if self.credential_provider else {}
        user_token = saved.get('user_token')
        partner_token = saved.get('partner_token') or config.YC_PARTNER_TOKEN
        if not partner_token or not user_token:
            raise YClientsError('Missing credentials for branch')
        return {'Accept': 'application/vnd.yclients.v2+json', 'Authorization': f'Bearer {partner_token}, User {user_token}'}

    def request(self, method, path, branch_id, params=None):
        if method.upper() != 'GET':
            raise YClientsReadOnlyError('YCLIENTS is read-only: only allowlisted GET operations are permitted')
        branch = str(branch_id)
        if not branch.isdigit() or not re.fullmatch(rf'(records/{re.escape(branch)}|client/{re.escape(branch)}/[0-9]+|clients/{re.escape(branch)}|staff/{re.escape(branch)})', path):
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

    def find_clients_by_phone(self, branch_id, phone, page_size=100):
        """Read the documented GET collection, then match the complete phone.

        The official clients GET is deprecated but still documented with phone,
        page and count. Its phone filter is substring-based. Never replace this
        operation with the newer POST search or return a partial page as proof.
        https://developers.yclients.com/ru/ — Clients: deprecated client list.
        """
        from link_manager import normalize_phone

        if isinstance(branch_id, bool) or not re.fullmatch(r'[1-9][0-9]{0,15}', str(branch_id)):
            raise YClientsError('Invalid branch identity for client search')
        if type(page_size) is not int or not 1 <= page_size <= 200:
            raise YClientsError('Invalid client search page size')

        def full_phone(value):
            # YCLIENTS documents both integer and string phones. A masked value
            # must not become a plausible number by dropping its non-digits.
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise YClientsError('CRM client phone is missing or masked')
            value = str(value).strip()
            if (not re.fullmatch(r'[+0-9\s().-]{7,64}', value) or
                    not re.fullmatch(r'\+?[0-9]{7,15}', re.sub(r'[\s().-]', '', value))):
                raise YClientsError('CRM client phone is missing or masked')
            normalized = normalize_phone(value)
            if not re.fullmatch(r'\+[1-9][0-9]{6,14}', normalized):
                raise YClientsError('CRM client phone is missing or masked')
            return normalized

        if not isinstance(phone, str):
            raise YClientsError('Invalid phone for client search')
        target = full_phone(phone)
        matches, seen = [], set()
        expected_total, total_present = None, None
        for page in range(1, 1001):
            payload = self.request('GET', f'clients/{branch_id}', branch_id,
                                   {'phone': target.lstrip('+'), 'page': page, 'count': page_size})
            items = payload.get('data')
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise YClientsError('Invalid clients collection')
            if len(items) > page_size:
                raise YClientsError('CRM returned an oversized client page')
            for item in items:
                if item.get('company_id') is not None and str(item['company_id']) != str(branch_id):
                    raise YClientsError('Cross-branch client in CRM response')
                client_id = item.get('id')
                if isinstance(client_id, bool) or not re.fullmatch(r'[1-9][0-9]{0,15}', str(client_id)):
                    raise YClientsError('Invalid client identity in search response')
                client_id = int(client_id)
                if client_id in seen:
                    raise YClientsError('CRM pagination repeated a client; incomplete search')
                seen.add(client_id)
                actual_phone = full_phone(item.get('phone'))
                if actual_phone == target:
                    matches.append({'id': client_id, 'phone': actual_phone,
                                    'name': item.get('name') if isinstance(item.get('name'), str) else '',
                                    'email': item.get('email') if isinstance(item.get('email'), str) else ''})
            meta = payload.get('meta')
            if meta is None or meta == []:
                meta = {}
            if not isinstance(meta, dict):
                raise YClientsError('Invalid client pagination metadata')
            if 'page' in meta and (isinstance(meta['page'], bool) or
                    not isinstance(meta['page'], (str, int)) or
                    not re.fullmatch(r'[1-9][0-9]*', str(meta['page'])) or int(meta['page']) != page):
                raise YClientsError('CRM client pagination returned the wrong page')
            total = meta.get('total_count')
            present = total is not None
            if total_present is None:
                total_present = present
            elif total_present != present:
                raise YClientsError('Client pagination metadata changed; search not applied')
            if present:
                if isinstance(total, bool) or not isinstance(total, (str, int)) or not re.fullmatch(r'[0-9]+', str(total)):
                    raise YClientsError('Invalid client pagination total')
                total = int(total)
                if expected_total is None:
                    expected_total = total
                elif expected_total != total:
                    raise YClientsError('CRM clients changed during pagination; search not applied')
                if len(seen) > total:
                    raise YClientsError('CRM client total contradicts returned rows')
                if len(seen) == total:
                    return matches
                if not items:
                    raise YClientsError('CRM returned an incomplete client page')
            elif len(items) < page_size:
                return matches
        raise YClientsError('Client pagination limit exceeded; search not applied')


def _notice_cutoff(record, visit, expiry):
    for key in ('seance_length', 'length'):
        duration = str(record.get(key, ''))
        if duration.isascii() and duration.isdigit() and len(duration) <= 8 and int(duration) > 0:
            return min(visit.timestamp() + int(duration) + 1800, expiry.timestamp())
    return min(visit.timestamp() + 3600, expiry.timestamp())


def appointment_days(at, duration_hours):
    """Include prior visits carrying over midnight and tomorrow's activation window."""
    first = datetime.fromtimestamp(at.timestamp()-duration_hours*3600, at.tzinfo).date()
    last = max(at.date()+timedelta(days=1),(at+timedelta(hours=max(2,config.ACCESS_BEFORE_VISIT_HOURS))).date())
    return (first+timedelta(days=offset) for offset in range((last-first).days+1))


def eligible_clients(records, branch_id, at=None, branch_timezone=None, *, lead_seconds=0, pass_duration_hours=8):
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
        expiry = pass_expiry({'pass_duration_hours': pass_duration_hours}, visit)
        # An early-morning appointment opens access on the preceding evening.
        # A small scheduler lead absorbs the local outbox dispatch interval.
        if visit - timedelta(hours=max(2, config.ACCESS_BEFORE_VISIT_HOURS), seconds=lead_seconds) <= at < expiry:
            key = str(client['id'])
            if key not in clients or parse_datetime(clients[key]['_access_expires']).timestamp() < expiry.timestamp():
                clients.setdefault(key, {}).update({field: client[field] for field in ('id', 'name', 'phone', 'email') if field in client})
                clients[key]['_access_expires'] = expiry.isoformat()
            # YCLIENTS record durations are seconds. An old booking may still
            # grant access today while a reminder for it is no longer timely.
            clients[key]['_notice_until'] = max(clients[key].get('_notice_until', 0), _notice_cutoff(record, visit, expiry))
    return clients


def get_clients_with_appointments_today(branch_id):
    branch = BranchManager().get_branch(branch_id)
    if not branch:
        raise YClientsError('Unknown branch')
    client = YClientsClient()
    at = now(branch.timezone)
    records = (r for day in appointment_days(at,branch.pass_duration_hours) for r in client.records_for_day(branch_id,day))
    return list(eligible_clients(records, branch_id, at, branch.timezone, pass_duration_hours=branch.pass_duration_hours))


def check_client_appointments(client_id, branch_id):
    return str(client_id) in get_clients_with_appointments_today(branch_id)


def get_client_data(company_id, client_id):
    return YClientsClient().get_client(company_id, client_id)


def send_message(*args, **kwargs):
    raise YClientsReadOnlyError('Sending messages through YCLIENTS is prohibited')
