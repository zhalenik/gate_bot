"""Explicit administrator connection wizard; separate from the GET-only CRM reader.

Only POST /auth exists here. No constructor/import performs a request. Credentials
are used for one user-submitted call and are neither logged nor stored.
Official contract: https://developers.yclients.com/ru/ (auth_request_2fa).
"""
import re
from urllib.parse import urlparse
import requests
from storage import config


def account_profile(data):
    """The documented auth response's display fields, never tokens or avatars."""
    if not isinstance(data,dict):return {}
    profile={}
    if isinstance(data.get('id'),(int,str)) and not isinstance(data['id'],bool) and re.fullmatch(r'[1-9][0-9]{0,19}',str(data['id'])):
        profile['id']=str(data['id'])
    for key in ('name','email','login','phone'):
        value=data.get(key)
        if isinstance(value,str) and not any(ord(c)<32 for c in value):
            profile[key]=value.strip()[:254]
    return profile


class YClientsConnector:
    def __init__(self, session=None, partner_token=None, base_url=None):
        self.session = session or requests.Session()
        self.partner_token = config.YC_PARTNER_TOKEN if partner_token is None else partner_token
        self.base_url = (base_url or config.YC_BASE_URL).rstrip('/')
        url = urlparse(self.base_url)
        if url.scheme != 'https' or url.hostname not in {'api.yclients.ru','api.yclients.com'} or url.path != '/api/v1' or url.query or url.fragment or url.username or url.password or url.port not in {None,443}:
            raise ValueError('YCLIENTS требует официальный HTTPS API')

    def authorize(self, login, password, code=None, challenge=None):
        if not isinstance(login,str) or not login.strip() or len(login)>254 or not isinstance(password,str) or not password or len(password)>1024:
            raise ValueError('Введите логин и пароль YCLIENTS')
        if not self.partner_token:raise ValueError('Не настроен партнёрский токен интеграции YCLIENTS')
        payload={'login':login.strip(),'password':password}
        if code or challenge:
            if not isinstance(challenge,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',challenge) or not isinstance(code,str) or not re.fullmatch(r'[0-9]{4,10}',code):
                raise ValueError('Введите код двухфакторной защиты из текущего запроса')
            payload['2fa']={'uuid':challenge,'secret':code}
        try:
            response=self.session.post(self.base_url+'/auth',json=payload,
                headers={'Authorization':'Bearer '+self.partner_token,'Accept':'application/vnd.yclients.v2+json','Content-Type':'application/json'},
                timeout=(5,20),allow_redirects=False)
            if response.status_code not in {200,201}:
                raise ValueError('YCLIENTS не подтвердил авторизацию (HTTP '+str(response.status_code)+')')
            result=response.json()
        except requests.RequestException:
            raise ValueError('Не удалось связаться с YCLIENTS. Повторите подключение позже') from None
        except (TypeError,ValueError) as error:
            if isinstance(error,ValueError) and str(error).startswith('YCLIENTS не подтвердил'):raise
            raise ValueError('YCLIENTS вернул некорректный ответ авторизации') from None
        if not isinstance(result,dict) or result.get('success') is False:raise ValueError('YCLIENTS не подтвердил авторизацию')
        data=result.get('data',result)
        if not isinstance(data,dict):raise ValueError('YCLIENTS вернул некорректный ответ авторизации')
        if response.status_code==201 and isinstance(data.get('user_token'),str) and data['user_token']:
            return {'user_token':data['user_token'],'requires_2fa':False,'account':account_profile(data)}
        if response.status_code==200 and result.get('success') is True and data.get('flow')=='login' and isinstance(data.get('uuid'),str):
            return {'requires_2fa':True,'challenge':data['uuid']}
        raise ValueError('YCLIENTS вернул неподдерживаемый ответ авторизации')

    def check_permissions(self, branch_id, credentials):
        """Read only the selected branch's permissions and one records page.

        API errors become stable diagnostics; neither records nor provider text
        leave this method. A successful empty records page alone cannot establish
        that the account is allowed to read phone numbers.
        """
        branch = str(branch_id)
        if not branch.isdigit():
            raise ValueError('Некорректный филиал YCLIENTS')
        token = credentials.get('user_token')
        partner = credentials.get('partner_token') or self.partner_token
        checks = []

        def result(code, message):
            return {'status': 'success' if code == 'ready' else 'error',
                    'connected': code == 'ready', 'code': code,
                    'checks': checks, 'message': message}

        if not token or not partner:
            return result('credentials_missing', 'Для проверки нужен партнёрский токен сервиса и подключённый аккаунт YCLIENTS.')
        headers = {'Authorization': 'Bearer ' + partner + ', User ' + token,
                   'Accept': 'application/vnd.yclients.v2+json'}

        def read(path, params=None):
            try:
                response = self.session.get(self.base_url + '/' + path, headers=headers,
                    params=params or {}, timeout=(5, 20), allow_redirects=False, verify=True)
                if response.status_code != 200:
                    return None, {401: 'authorization_failed', 403: 'access_denied',
                                  404: 'branch_unavailable', 429: 'rate_limited'}.get(response.status_code, 'provider_unavailable')
                payload = response.json()
                if not isinstance(payload, dict) or payload.get('success') is not True:
                    return None, 'invalid_response'
                return payload.get('data'), None
            except requests.RequestException:
                return None, 'transport_error'
            except (ValueError, TypeError):
                return None, 'invalid_response'

        permissions, error = read('user/permissions/' + branch)
        if error or not isinstance(permissions, dict):
            code = error or 'invalid_response'
            checks.append({'key': 'branch', 'state': 'error', 'code': code})
            return result(code, {
                'authorization_failed': 'YCLIENTS не принял токен. Подключите аккаунт заново.',
                'access_denied': 'Пользователю запрещён доступ к этому филиалу. Проверьте филиал и права в YCLIENTS.',
                'branch_unavailable': 'Филиал не найден или недоступен этому пользователю.',
                'rate_limited': 'YCLIENTS временно ограничил число запросов. Повторите проверку позже.',
            }.get(code, 'Не удалось проверить права YCLIENTS. Подключение пока не подтверждено.'))
        checks.append({'key': 'branch', 'state': 'success', 'code': 'verified'})
        required = {
            'records': [('timetable', 'timetable_access'), ('record_form', 'record_form_access')],
            'contacts': [('timetable', 'timetable_phones_access'), ('record_form', 'record_form_client_access')],
        }
        for key, fields in required.items():
            granted = all(isinstance(permissions.get(group), dict) and
                          permissions[group].get(field) is True for group, field in fields)
            checks.append({'key': key, 'state': 'success' if granted else 'error',
                           'code': 'verified' if granted else 'permission_missing'})
        timetable = permissions.get('timetable', {})
        restricted = any(timetable.get(key) not in (0, '0') for key in ('master_id', 'position_id'))
        checks.append({'key': 'all_staff', 'state': 'error' if restricted else 'success',
                       'code': 'staff_restricted' if restricted else 'verified'})
        if any(item['state'] == 'error' for item in checks):
            return result('permissions_missing', 'В YCLIENTS разрешите просмотр записей всех сотрудников и данных клиентов, включая телефоны в журнале записи. Затем повторите подключение или проверку.')
        # The scheduler reads embedded clients in records. Access to the entire
        # client directory is an additional capability, not a prerequisite for it.
        directory = permissions.get('clients', {})
        directory_access = isinstance(directory, dict) and all(directory.get(key) is True for key in
            ('clients_access', 'client_phones_access', 'clients_phones_email_access', 'clients_card_phone_access'))
        checks.append({'key': 'client_directory', 'state': 'success' if directory_access else 'warning',
                       'code': 'verified' if directory_access else 'permission_missing'})
        from timeutil import now
        day = str(now().date())
        records, error = read('records/' + branch, {'start_date': day, 'end_date': day, 'count': 1, 'page': 1})
        valid = not error and isinstance(records, list) and all(isinstance(row, dict) and
                    (row.get('company_id') is None or str(row['company_id']) == branch) for row in records)
        checks.append({'key': 'records_api', 'state': 'success' if valid else 'error',
                       'code': 'verified' if valid else error or 'invalid_response'})
        if not valid:
            return result(error or 'invalid_response', 'Права получены, но чтение записей филиала не подтверждено. Проверьте доступ к API и повторите проверку.')
        from link_manager import normalize_phone
        if any(isinstance(row.get('client'), dict) and row['client'].get('id') and not normalize_phone(row['client'].get('phone')) for row in records):
            checks.append({'key': 'contacts', 'state': 'error', 'code': 'contacts_unconfirmed'})
            return result('contacts_unconfirmed', 'В проверенной записи нет доступного телефона клиента. Проверьте телефон в карточке и права просмотра телефонов в журнале YCLIENTS.')
        return result('ready', 'YCLIENTS подключён: филиал, записи и клиентские контакты доступны для чтения.')
