"""Typed installation settings. Contains names/defaults only, never credentials."""
import ipaddress
import math
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from yclients_urls import normalize_yclients_base_url
from message_templates import PASS_TEMPLATE, CODE_TEMPLATE, validate_template


GROUPS = {
    'delivery': 'Уведомления',
    'hardware': 'Аппаратные средства',
    'bots': 'Чат-боты',
    'yclients': 'CRM',
    'development': 'Разработка',
}

BLOCKS = {
    'service': 'Сервис и веб-доступ', 'delivery': 'Отправка уведомлений',
    'telegram': 'Telegram Bot', 'max': 'MAX Bot', 'yclients': 'YCLIENTS',
    'schedule': 'Планировщик', 'hardware': 'Настройки аппаратных вызовов и СМС',
    'development': 'Разработка и тестовая регистрация',
}

BLOCK_GROUPS = {
    'service':'development','delivery':'delivery','telegram':'bots','max':'bots',
    'yclients':'yclients','schedule':'yclients','hardware':'hardware','development':'development',
}



def item(key, label, group, default='', kind='text', **extra):
    return {'key': key, 'label': label, 'group': group, 'default': default,
            'kind': kind, 'secret': False, 'help': '', **extra}


FIELDS = [
    item('GATEBOT_ENV','Окружение','service','development','select',options=['development','test','production'],help='Production отключает тестовую переадресацию SMS и требует явный ключ сессий.'),
    item('GATEBOT_TIMEZONE','Часовой пояс','service','Europe/Moscow','timezone'),
    item('PUBLIC_SITE_URL','Сайт GateBot для клиентов','service','','url',optional=True,help='Ссылка внизу страницы пропуска и настроек уведомлений.'),
    item('REQUESTS_CA_BUNDLE','Файл доверенных сертификатов Requests (.pem)','service','','path',optional=True,help='Путь внутри контейнера или Windows. Для стандартного набора сертификатов оставьте пустым.'),
    item('BASE_URL','Публичный адрес GateBot','service','http://127.0.0.1:5000','url',help='Адрес персональных ссылок и интеграций, например https://gatebot.example.com.'),
    item('HOST','Адрес прослушивания веб-сервера','service','127.0.0.1','host',help='В контейнере обычно 0.0.0.0. Это не публичный домен.'),
    item('PORT','Порт веб-сервера','service',5000,'int',minimum=1,maximum=65535),
    item('SECRET_KEY','Ключ сессий','service','','secret',secret=True,minimum_length=32,help='Смена завершит прежние веб-сессии. В production обязателен; пустой допустим только вне production.'),
    item('SESSION_COOKIE_SECURE','Cookie только через HTTPS','service',False,'bool'),
    item('GATEBOT_DATA_DIR','Каталог данных приложения','service','./data','path',help='Изменение не переносит файлы. Перед перезапуском подготовьте согласованную копию SQLite, журналов и ключей в новом каталоге.'),
    item('GATEBOT_LOG_DIR','Каталог диагностических журналов','service','./data/logs','path'),
    item('CLIENT_OUTBOUND_ENABLED','Уведомления клиентам','delivery',False,'bool'),
    item('ADMIN_OUTBOUND_ENABLED','Уведомления администраторам','delivery',False,'bool'),
    item('PASS_LINK_TEMPLATE','Сообщение со ссылкой на пропуск','delivery',PASS_TEMPLATE,'template',placeholder='link',help='Параметр {link} подставляет персональную ссылку.'),
    item('VERIFICATION_CODE_TEMPLATE','Сообщение с кодом подтверждения','delivery',CODE_TEMPLATE,'template',placeholder='code',help='Параметр {code} подставляет код.'),
    item('BOT_ENABLED','Запускать Telegram Bot','telegram',False,'bool'),
    item('BOT_TOKEN','Токен Telegram Bot','telegram','','secret',secret=True),
    item('TELEGRAM_PROXY_URL','Прокси Telegram Bot','telegram','','proxy',secret=True,help='HTTP(S) или SOCKS5 прокси VPN-контейнера. Используется для polling, ответов и уведомлений Telegram.'),
    item('BOT_USERNAME','Имя Telegram Bot','telegram'),
    item('BOT_MODE','Режим Telegram Bot','telegram','polling','select',options=['polling','webhook']),
    item('TELEGRAM_WEBHOOK_URL','Webhook URL Telegram','telegram','','url',optional=True,
         help='По умолчанию используется публичный HTTPS-адрес GateBot с путём /webhook/telegram.'),
    item('TELEGRAM_WEBHOOK_SECRET','Секрет webhook Telegram','telegram','','secret',secret=True,
         help='Оставьте пустым для автоматического секрета. При смене режима сохраните настройки и перезапустите сервис.'),
    item('MAX_BOT_ENABLED','Запускать MAX Bot','max',False,'bool'),
    item('MAX_BOT_TOKEN','Токен MAX Bot','max','','secret',secret=True),
    item('MAX_BOT_USERNAME','Имя MAX Bot','max'),
    item('MAX_BOT_MODE','Режим MAX Bot','max','polling','select',options=['polling','webhook']),
    item('MAX_API_BASE_URL','Адрес API MAX','max','https://platform-api2.max.ru','https_url'),
    item('MAX_CA_BUNDLE','Файл корневых сертификатов MAX','max','','path',optional=True,help='Проверка TLS остаётся включённой. Путь должен существовать на сервере после перезапуска.'),
    item('MAX_WEBHOOK_SECRET','Секрет webhook MAX','max','','secret',secret=True,
         help='GateBot регистрирует /webhook/max на публичном HTTPS-адресе, порт 443. Пустое поле включает автоматический секрет.'),
    item('YCLIENTS_ENABLED','Разрешить интеграцию YCLIENTS','yclients',False,'bool'),
    item('YC_BASE_URL','Адрес API YCLIENTS','yclients','https://api.yclients.ru/api/v1','yclients_url'),
    item('YC_PARTNER_TOKEN','Партнёрский токен YCLIENTS','yclients','','secret',secret=True),
    item('YC_WEBHOOK_SECRET','Секрет webhook записей YCLIENTS','yclients','','secret',secret=True),
    item('YC_MARKETPLACE_APPLICATION_ID','ID приложения в маркетплейсе','yclients','','identifier'),
    item('YC_MARKETPLACE_SYSTEM_USER_TOKEN','Токен системного пользователя приложения','yclients','','secret',secret=True,help='Общий системный пользователь приложения. Токены ручного подключения отдельных филиалов сохраняются только в их интеграциях.'),
    item('YC_MARKETPLACE_CONNECT_ENABLED','Разрешить установку из маркетплейса','yclients',False,'bool',help='Установка добавляет системного пользователя в выбранный филиал YCLIENTS. Включайте для принятого сценария подключения.'),
    item('SCHEDULER_ENABLED','Запускать планировщик','schedule',False,'bool'),
    item('ACCESS_BEFORE_VISIT_HOURS','Открывать доступ за часов до записи','schedule',2,'int',minimum=0,maximum=24,help='Бизнес-правило автоматически обеспечивает минимум два часа для заранее известных записей.'),
    item('ACCESS_UPD_PERIOD','Интервал чтения CRM, минут','schedule',15,'int',minimum=1,maximum=1440),
    item('GATE_COOLDOWN_SECONDS','Пауза между командами ворот, секунд','hardware',20,'int',minimum=1,maximum=300),
    item('GATE_DRY_RUN','Имитировать команды ворот','hardware',True,'bool'),
    item('SIM800_ENABLED','Использовать SIM800','hardware',False,'bool'),
    item('SIM800_CALL_DURATION','Длительность вызова, секунд','hardware',3,'float',minimum=0.1,maximum=300),
    item('SIM800_SMS_STORAGE','Память SMS','hardware','SM','select',options=['SM','ME','MT']),
    item('SIM800_QUEUE_TIMEOUT','Таймаут очереди модема, секунд','hardware',40,'int',minimum=1,maximum=300),
    item('SIM800_MAX_QUEUE','Максимум команд в очереди','hardware',4,'int',minimum=1,maximum=100),
    item('CONSOLE_DEBUG_MESSAGES','Подробная диагностика в терминале','development',False,'bool'),
    item('DEV_REGISTRATION_SMS_REDIRECT_ENABLED','Тестовая переадресация регистрационных SMS','development',False,'bool',help='Действует только при точном GATEBOT_ENV=development. В production всегда отключена.'),
    item('DEV_REGISTRATION_SMS_PREFIX','Префикс тестовых номеров','development','','phone_prefix'),
    item('DEV_REGISTRATION_SMS_PHONE','Получатель тестовых SMS','development','','phone'),
]
# Older .env and encrypted exports seed the first modem from these values.
# The inventory form owns these settings once an administrator saves the device.
MODEM_SEEDS = [
    item('SIM800_PORT','Исходный порт SIM800C','hardware','','path',optional=True),
    item('SIM800_BAUDRATE','Исходная скорость SIM800C','hardware',115200,'int',minimum=1,maximum=921600),
    item('SIM800_COMMAND_TIMEOUT','Исходный таймаут AT-команды','hardware',5,'float',minimum=0.1,maximum=300),
]
BY_KEY = {field['key']: field for field in (*FIELDS,*MODEM_SEEDS)}
SMTP_ENV_KEYS = {'SMTP_ENABLED','MAILING_SMTP_SERVER','MAILING_SMTP_PORT','MAILING_SMTP_SECURITY',
                 'MAILING_SMTP_USER','MAILING_SMTP_PASSWORD','MAILING_SMTP_FROM','MAILING_SMTP_FROM_EMAIL'}


def default_values(environment=None):
    values={key:encode(field, field['default']) for key,field in BY_KEY.items()}
    if environment:
        values.update({key:str(value) for key,value in environment.items() if key in BY_KEY})
        for key in ('CLIENT_OUTBOUND_ENABLED', 'ADMIN_OUTBOUND_ENABLED'):
            if key not in environment and 'OUTBOUND_ENABLED' in environment:
                values[key] = str(environment['OUTBOUND_ENABLED'])
    if 'SESSION_COOKIE_SECURE' not in (environment or {}):
        values['SESSION_COOKIE_SECURE']='true' if values['GATEBOT_ENV']=='production' else 'false'
    if 'GATEBOT_LOG_DIR' not in (environment or {}):
        values['GATEBOT_LOG_DIR']=values['GATEBOT_DATA_DIR'].rstrip('/\\')+'/logs'
    return values


def encode(field, value):
    kind=field['kind'];key=field['key']
    if kind == 'template':
        return validate_template(value, field['placeholder'])
    if kind=='bool':
        if type(value) is bool:return 'true' if value else 'false'
        if isinstance(value,str) and value.lower() in {'true','false','1','0','yes','no','on','off'}:
            return 'true' if value.lower() in {'true','1','yes','on'} else 'false'
        raise ValueError('Укажите да или нет: '+key)
    if kind in {'int','float'}:
        if isinstance(value,bool) or not isinstance(value,(int,float,str)):
            raise ValueError('Укажите число: '+key)
        try:number=float(value)
        except (ValueError,TypeError,OverflowError):raise ValueError('Укажите число: '+key) from None
        if not math.isfinite(number) or not field['minimum']<=number<=field['maximum'] or (kind=='int' and not number.is_integer()):
            raise ValueError('Число вне допустимого диапазона: '+key)
        return str(int(number)) if kind=='int' else str(number)
    if not isinstance(value,str) or len(value)>8192 or any(ord(c)<32 or ord(c)==127 for c in value):
        raise ValueError('Некорректное значение: '+key)
    if kind!='secret':value=value.strip()
    if kind=='secret':
        if value and (not value.isascii() or any(c.isspace() for c in value) or len(value)<field.get('minimum_length',1)):
            raise ValueError('Некорректный секрет: '+key)
    elif kind=='proxy' and value:
        try:
            proxy=urlsplit(value)
            valid=proxy.scheme in {'http','https','socks5'} and proxy.hostname and proxy.port and proxy.path in {'','/'} and not proxy.query and not proxy.fragment
        except ValueError:
            valid=False
        if not valid or any(c.isspace() for c in value):
            raise ValueError('Укажите полный адрес прокси и порт')
    elif kind=='select' and value not in field['options']:
        raise ValueError('Неизвестный вариант: '+key)
    elif kind=='timezone':
        try:ZoneInfo(value)
        except (ValueError,ZoneInfoNotFoundError):raise ValueError('Неизвестный часовой пояс') from None
    elif kind in {'url','https_url','yclients_url'}:
        if not value and field.get('optional'):return ''
        try:parsed=urlsplit(value);port=parsed.port
        except ValueError:raise ValueError('Некорректный URL: '+key) from None
        if parsed.scheme not in ({'https'} if kind!='url' else {'http','https'}) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Нужен URL без пароля, query и fragment: '+key)
        if kind=='yclients_url':
            return normalize_yclients_base_url(value)
        value=value.rstrip('/')
    elif kind=='host':
        try:ipaddress.ip_address(value)
        except ValueError:
            if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?',value) or '..' in value:raise ValueError('Некорректный адрес сервера')
    elif kind=='path' and not value and not field.get('optional'):
        raise ValueError('Укажите путь: '+key)
    elif kind=='identifier' and value and (not value.isascii() or not value.isdigit() or int(value)<1):
        raise ValueError('Укажите положительный ID приложения')
    elif kind=='phone' and value and not re.fullmatch(r'\+[1-9][0-9]{6,14}',value):
        raise ValueError('Телефон должен быть в международном формате: '+key)
    elif kind=='phone_prefix' and value and not re.fullmatch(r'\+[1-9][0-9]{2,13}',value):
        raise ValueError('Префикс должен начинаться с кода страны')
    return value


def decode(field, value):
    if field['kind']=='bool':return str(value).lower() in {'true','1','yes','on'}
    if field['kind']=='int':return int(value)
    if field['kind']=='float':return float(value)
    return value


def validate_effective(values):
    normalized={key:encode(field,values.get(key,field['default'])) for key,field in BY_KEY.items()}
    if normalized['GATEBOT_ENV']=='production' and len(normalized['SECRET_KEY'])<32:
        raise ValueError('Для production сначала задайте SECRET_KEY длиной не менее 32 символов')
    from bot_webhooks import webhook_url,validate_secret
    for platform,prefix,key in [('telegram','BOT','TELEGRAM_WEBHOOK_SECRET'),('max','MAX_BOT','MAX_WEBHOOK_SECRET')]:
        if normalized[prefix+'_ENABLED']=='true' and normalized[prefix+'_MODE']=='webhook':
            webhook_url(platform,normalized['BASE_URL'],normalized['TELEGRAM_WEBHOOK_URL'] if platform=='telegram' else '')
            validate_secret(platform,normalized[key])
            if not normalized[prefix+'_TOKEN']:
                raise ValueError('Укажите токен для регистрации webhook '+platform)
    return normalized
