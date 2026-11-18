"""Opt-in SMTP notifications. No test message is sent on import or execution."""
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from pathlib import Path
import smtplib
from smtp_settings import SmtpSettings, email_address, smtp_connection
from storage import config
from link_manager import LinkManager
from branch_manager import BranchManager
from timeutil import parse_datetime
import logger

TEMPLATES_DIR = Path(__file__).parent / 'templates' / 'mailing_templates'


def load_template(template_name):
    if template_name not in {'base.html', 'access_link.html', 'branch_registration.html'}:
        raise ValueError('Unknown email template')
    return (TEMPLATES_DIR / template_name).read_text(encoding='utf-8')


def send_email(to_email, subject, html_content, from_email=None):
    if not config.OUTBOUND_ENABLED:
        return False
    if not email_address(to_email) or not isinstance(subject, str) or any(ch in subject for ch in '\r\n'):
        return False
    settings = SmtpSettings().effective()
    if not settings['enabled'] or not email_address(from_email or settings['from_email']):
        return False
    message = EmailMessage()
    message['From'] = formataddr((settings['from_name'], from_email or settings['from_email']))
    message['To'] = to_email
    message['Subject'] = subject
    message.set_content('Информация о доступе на парковку содержится в HTML-версии письма.')
    message.add_alternative(html_content, subtype='html')
    try:
        with smtp_connection(settings) as server:
            server.send_message(message)
        logger.event('SMTP_SENT', details={'recipient_ref': logger.reference(to_email)})
        return True
    except (OSError, ValueError, smtplib.SMTPException) as error:
        logger.event('SMTP_ERROR', level='ERROR', details={'error_type': type(error).__name__})
        return False


def send_email_with_base_template(to_email, subject, content_text, from_email=None):
    return send_email(to_email, subject, load_template('base.html').replace('{content}', escape(content_text)), from_email)


def send_access_link(token, to_email, links_file=None, branches_file=None):
    user = LinkManager(links_file).load_links().get(token)
    if not user:
        return False
    branch = BranchManager(branches_file).get_branch(user['branch_id'])
    if not branch:
        return False
    expiry = parse_datetime(user['access_expires']).strftime('%d.%m.%Y %H:%M %Z') if user.get('access_expires') else 'не ограничен'
    fields = {'name': user.get('user_name', ''), 'branch_name': branch.name, 'expires_at': expiry, 'link': config.BASE_URL + '/opn/' + token}
    html = load_template('access_link.html')
    for key, value in fields.items():
        html = html.replace('{' + key + '}', escape(str(value), quote=True))
    return send_email(to_email, 'Доступ на парковку — ' + branch.name, html)


def send_branch_registration_link(*args, **kwargs):
    # Registration is not implemented in the supplied baseline.
    return False
