"""Environment configuration. Importing it never starts bots or integrations."""
import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_DIR / ".env"
load_dotenv(ENV_FILE, override=False, encoding="utf-8-sig")

def _project_path(value):
    """Keep local state and certificates stable when Cursor changes the cwd."""
    path = Path(value)
    return str((path if path.is_absolute() else PROJECT_DIR / path).resolve())

def env_bool(name, default=False):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"1", "true", "yes", "on"}

def env_int(name, default, minimum=1, maximum=100000):
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} is out of range")
    return value

DATA_DIR = _project_path(os.getenv("GATEBOT_DATA_DIR", PROJECT_DIR / "data"))
LOG_DIR = _project_path(os.getenv("GATEBOT_LOG_DIR", Path(DATA_DIR) / "logs"))
ENVIRONMENT = os.getenv("GATEBOT_ENV", "development")
TIMEZONE = os.getenv("GATEBOT_TIMEZONE", "Europe/Moscow")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = env_int("PORT", 5000, maximum=65535)
CONSOLE_DEBUG_MESSAGES = env_bool("CONSOLE_DEBUG_MESSAGES")
SESSION_COOKIE_SECURE = env_bool("SESSION_COOKIE_SECURE", ENVIRONMENT == "production")

def _secret_key():
    supplied = os.getenv("SECRET_KEY", "")
    if supplied:
        if len(supplied) < 32:
            raise ValueError("SECRET_KEY must contain at least 32 characters")
        return supplied
    if ENVIRONMENT == "production":
        raise ValueError("SECRET_KEY is required in production")
    # A separate private file keeps development sessions valid across restarts.
    path = Path(DATA_DIR) / "session.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        value = path.read_text(encoding="ascii").strip()
        if len(value) < 32:
            raise ValueError("Invalid session key file")
        return value
    value = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(value)
    return value

SECRET_KEY = _secret_key()
timeout = env_int("GATE_COOLDOWN_SECONDS", 21, maximum=300)
call_queue_timeout = env_int("SIM800_QUEUE_TIMEOUT", 40, maximum=300)
max_calls_in_queue = env_int("SIM800_MAX_QUEUE", 4, maximum=100)
SIM800_ENABLED = env_bool("SIM800_ENABLED")
GATE_DRY_RUN = env_bool("GATE_DRY_RUN", True)
SIM800_PORT = os.getenv("SIM800_PORT", "")
SIM800_BAUDRATE = env_int("SIM800_BAUDRATE", 115200, maximum=921600)
SIM800_COMMAND_TIMEOUT = float(os.getenv("SIM800_COMMAND_TIMEOUT", "5"))
SIM800_CALL_DURATION = float(os.getenv("SIM800_CALL_DURATION", "3"))
SIM800_SMS_STORAGE = os.getenv("SIM800_SMS_STORAGE", "SM")

BOT_ENABLED = env_bool("BOT_ENABLED")
BOT_MODE = os.getenv("BOT_MODE", "polling")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
BOT_DEBUG_MESSAGES = False
WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL", "")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
MAX_BOT_ENABLED = env_bool("MAX_BOT_ENABLED")
MAX_BOT_MODE = os.getenv("MAX_BOT_MODE", "polling")
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "")
MAX_BOT_USERNAME = os.getenv("MAX_BOT_USERNAME", "")
MAX_API_BASE_URL = os.getenv("MAX_API_BASE_URL", "https://platform-api2.max.ru")
MAX_CA_BUNDLE = os.getenv("MAX_CA_BUNDLE", "")
if MAX_CA_BUNDLE:
    MAX_CA_BUNDLE = _project_path(MAX_CA_BUNDLE)
MAX_WEBHOOK_SECRET = os.getenv("MAX_WEBHOOK_SECRET", "")

YCLIENTS_ENABLED = env_bool("YCLIENTS_ENABLED")
YC_BASE_URL = os.getenv("YC_BASE_URL", "https://api.yclients.ru/api/v1")
YC_PARTNER_TOKEN = os.getenv("YC_PARTNER_TOKEN", "")
# A fallback credential is only usable for the one explicitly named branch.
YC_USER_TOKEN = os.getenv("YC_USER_TOKEN", "")
YC_COMPANY_ID = os.getenv("YC_COMPANY_ID", "")
YC_WEBHOOK_SECRET = os.getenv("YC_WEBHOOK_SECRET", "")
YC_WEBHOOK_URL = BASE_URL + "/webhook/yclients"
YC_SEND_MESSAGES = False  # Immutable policy: no YCLIENTS write operations exist.
ACCESS_BEFORE_VISIT_HOURS = env_int("ACCESS_BEFORE_VISIT_HOURS", 2, minimum=0, maximum=24)
ACCESS_UPD_PERIOD = env_int("ACCESS_UPD_PERIOD", 15, maximum=1440)
SCHEDULER_ENABLED = env_bool("SCHEDULER_ENABLED")
SCHEDULER_DUPLICATE_CHECK = True

OUTBOUND_ENABLED = env_bool("OUTBOUND_ENABLED")
SMTP_ENABLED = env_bool("SMTP_ENABLED")
MAILING_SMTP_SERVER = os.getenv("MAILING_SMTP_SERVER", "")
MAILING_SMTP_PORT = env_int("MAILING_SMTP_PORT", 465, maximum=65535)
MAILING_SMTP_USER = os.getenv("MAILING_SMTP_USER", "")
MAILING_SMTP_PASSWORD = os.getenv("MAILING_SMTP_PASSWORD", "")
MAILING_SMTP_FROM = os.getenv("MAILING_SMTP_FROM", "GateBot")
