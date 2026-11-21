"""Small, escaped HTML onboarding letter without credentials or bearer links."""
from html import escape


def render_welcome(details, base_url):
    base = escape(base_url.rstrip('/'), quote=True)
    name = escape(str(details.get('name') or 'Администратор'))
    branch = escape(str(details.get('branch_name') or 'Ваш филиал'))
    crm = ('<li><b>Подключите YCLIENTS.</b> В разделе «Интеграции» проверьте подключение и права '
           'пользователя. GateBot выдаёт клиенту пропуск перед записью и отправляет персональную '
           'ссылку по выбранным клиентом каналам. Здесь же можно подключить сервис рассылки.</li>'
           if details.get('yclients_enabled') else
           '<li><b>Добавьте клиентов.</b> В таблице клиентов укажите имя и телефон, выдайте '
           'постоянный или временный пропуск и передайте клиенту его персональную ссылку.</li>')
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8">
<body style="margin:0;background:#EFF0E8;color:#0C0F14;font-family:Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="24"><tr><td align="center">
<table role="presentation" width="100%" style="max-width:600px;background:#FFFFFF;border-radius:24px" cellpadding="28">
<tr><td style="background:#C1BCDC;border-radius:24px 24px 0 0"><div style="font-size:24px;font-weight:bold">GateBot</div>
<h1 style="font-size:28px;margin-bottom:8px">Здравствуйте, {name}!</h1><p style="margin:0">{branch} — ваш кабинет готов.</p></td></tr>
<tr><td><p>Вы управляете доступом к воротам из браузера. Начните с трёх шагов:</p>
<ol style="padding-left:22px;line-height:1.7"><li><b>Настройте ворота.</b> В «Настройках» добавьте кнопки ворот, их названия и номера, а также контакт администратора.</li>
{crm}<li><b>Включите удобные уведомления.</b> В «Уведомлениях» выберите события и каналы, подключите Telegram или MAX. В ботах можно управлять пропусками.</li></ol>
<p style="margin:28px 0"><a href="{base}/admin/login" style="display:inline-block;background:#D3482B;color:#FFFFFF;text-decoration:none;padding:15px 24px;border-radius:14px;font-weight:bold">Открыть кабинет</a></p>
<p style="line-height:1.6">Ваш логин — email, указанный при регистрации. Если пароль забыт, восстановите его по SMS на странице входа.</p>
<p style="line-height:1.6">Подробные инструкции собраны в разделе <a href="{base}/admin/help" style="color:#0C0F14">«Помощь»</a>. Кнопка «Поддержка» в кабинете открывает чат с командой сервиса.</p>
<p style="color:#50535A;font-size:13px">В подписке можно проверить срок действия и расход квоты. Пароли и SMS-коды никому не передавайте.</p>
</td></tr></table></td></tr></table></body></html>'''
