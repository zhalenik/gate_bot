# GateBot / AJ — инструкции для следующих сессий

1. Начни с APP_DESCRIPTION.md → docs/ARCHITECTURE.md → docs/ROADMAP.md → текущего кода. Исторические TODO из docs/archive не выполнять автоматически.
2. YCLIENTS — действующая CRM бизнеса. Разрешено только читать по allowlist GET. Никаких POST/PUT/PATCH/DELETE, SMS через CRM, логина через auth POST или настройки webhook. Не менять это правило ради теста.
3. Публичный репозиторий: никакие ключи, .env, private state, логи, телефонные номера ворот/клиентов, SMS или сырые webhook payloads не публиковать. scan_public_tree проверяет точный tracked set; дополнительно проверять известные локальные секреты без печати значений.
4. Суперадминистратор видит все филиалы; branch admin — только свой. Проверять scope в каждом чтении и мутации. Проверка UI не заменяет серверную.
5. Все команды ворот проходят AccessService, затем единственную очередь hardware adapter. Чужой номер не принимать из запроса. Не маскировать stub/dry-run под реальное действие.
6. MAX имеет собственный API. Сверять официальные dev.max.ru и github.com/max-messenger/max-agents-skill. Telegram transport не является образцом wire protocol MAX.
7. Локальная приёмка пользователя — Windows + polling. Дальше TrueNAS SCALE + YAML, Nginx Proxy Manager и домен. Не считать production webhook/контейнер проверенными без реальной приёмки.
8. run.py владеет runtime. Import не запускает сеть, schedule или COM. Тесты всегда offline, физические эффекты и исходящие сообщения mock.
9. Не сокращать ручные права автоматической синхронизацией. admin_blocked выше appointment; mailing_enabled относится только к уведомлениям.
10. Сохранять документацию от общего к частному, версии, диагностические события и воспроизводимые regression cases. Оплаты и EXOLVE — отдельные будущие этапы, не текущая задача.
