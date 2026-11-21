"""Plain-text service messages. Only the documented placeholder is expanded."""
import os

PASS_TEMPLATE = 'Ваш пропуск GateBot: {link}'
CODE_TEMPLATE = 'GateBot: {code}. Код действует 5 минут. Никому не сообщайте.'


def validate_template(value, placeholder):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1024:
        raise ValueError('Введите текст шаблона длиной до 1024 символов')
    marker = '{' + placeholder + '}'
    remainder = value.replace(marker, '')
    if marker not in value or '{' in remainder or '}' in remainder:
        raise ValueError('Допустим только обязательный параметр ' + marker)
    if any(ord(ch) < 32 and ch != '\n' or ord(ch) == 127 for ch in value):
        raise ValueError('Недопустимые символы в шаблоне')
    return value.strip()


def verification_code(code):
    return validate_template(os.getenv('VERIFICATION_CODE_TEMPLATE', CODE_TEMPLATE), 'code').replace('{code}', str(code))


def pass_link(url):
    return validate_template(os.getenv('PASS_LINK_TEMPLATE', PASS_TEMPLATE), 'link').replace('{link}', url)
