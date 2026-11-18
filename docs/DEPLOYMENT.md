# Контейнер, TrueNAS и Nginx Proxy Manager

Первичная приёмка выполняется на Windows в polling. Образ `gatebot:0.2.0-alpha.14` собран локально для `linux/amd64`; запуск на конкретном TrueNAS, домен и доставка Push принимаются отдельно. Образ пока не опубликован в контейнерном registry.

## Испытания на bot.zhvakin.space

Для этого домена подготовлен отдельный `compose.server.yaml`. Он использует тот же образ, отдельную приватную `.env.server`, HTTPS `BASE_URL`, secure cookies и сеть `gatebot_proxy`. Windows `.env` не меняется. `Dockerfile` включает каталог `resources`, необходимый редактору палитры. Обычный `compose.yaml` сохраняет локальный сценарий.

На сервере создайте `.env.server` из `examples/server.env.example`, задайте собственный `SECRET_KEY` и нужные глобальные credentials. Пример выключает внешние отправки, CRM, ботов и физический модем для первого запуска. Для испытания Push включите `OUTBOUND_ENABLED=true` после проверки существующих настроек других каналов. Серверная среда для этого этапа явно `development`: тестовое подключение тарифов доступно, реальные платежи отсутствуют. Перед промышленным запуском переключите её в `production`.

Сохранённые системные параметры имеют приоритет над Compose. Перед переносом сбросьте к источнику «Окружение» платформенные overrides `GATEBOT_DATA_DIR`, `GATEBOT_LOG_DIR`, `HOST`, `PORT`, `BASE_URL`, `SESSION_COOKIE_SECURE`, `SIM800_PORT`, `MAX_CA_BUNDLE`. Тогда локальная `.env` продолжает задавать Windows-параметры, а `.env.server` и Compose — серверные. Проверьте также окружение и исходящие переключатели: старое сохранённое значение не должно неожиданно включить другой режим. Не удаляйте зашифрованные системные настройки и их ключ.

Подготовьте согласованную копию состояния при остановленном runtime: весь `data` вместе с SQLite и `integration.key`, приватными логотипами/офертами и телефонией; отдельно пару `private/system-config/settings.enc` + `settings.key`. Эти каталоги на сервере должны быть доступны UID/GID 568. Для переноса SQLite с работающего процесса нужен штатный SQLite backup, а не копирование одного файла без WAL. Не запускайте две копии бота с одним polling-токеном или одним модемом.

```sh
docker compose --env-file .env.server -f compose.server.yaml config --quiet
docker compose --env-file .env.server -f compose.server.yaml build
docker compose --env-file .env.server -f compose.server.yaml up -d
docker compose --env-file .env.server -f compose.server.yaml logs --tail 100 gatebot
```

В Nginx Proxy Manager подключите контейнер прокси к сети `gatebot_proxy`, затем создайте Proxy Host `bot.zhvakin.space` → scheme `http`, hostname `gatebot`, port `5000`. Назначьте действующий сертификат для домена и включите Force SSL. Укажите DNS домена на сервер. Не включайте Cache Assets/CDN-кеширование для `/pwa/`, `/opn/`, `/preferences/`, `/open-gate` и клиентских API. Для PWA не нужны WebSocket и отдельный входящий Push webhook: браузер использует собственную push-службу, сервер делает исходящий HTTPS-запрос к ней.

Порт 30550 в этом Compose доступен только с localhost сервера; интернет-трафик идёт через NPM. Если NPM работает отдельно от Docker-сети, используйте существующий внутренний маршрут сервера и ограничьте доступ к порту сетевыми правилами вместо публикации его в Интернет. В TrueNAS аналогичные домен, secure cookie и данные задаются в YAML Custom App; имя dataset/устройства остаётся параметром конкретного сервера.

После запуска проверьте `/healthz`, вход администратора, палитру/бренд, персональную страницу и [мобильную приёмку PWA](PWA.md). VAPID и подписки хранятся в `/state/application.sqlite3` с шифрованием через `/state/integration.key`; постоянный volume обязателен. Локальная сборка не подтверждает доступность домена и Push на физическом телефоне.

## Перенос готового образа

Для `0.2.0-alpha.14` подготовлен локальный архив `private/releases/gatebot-0.2.0-alpha.14.tar`. Он содержит образ с тегами `gatebot:0.2.0-alpha.14` и `gatebot:server-test`; приватного состояния приложения в образе нет. Архив не хранится в Git. После копирования архива на Linux-сервер архитектуры amd64:

```sh
docker load --input gatebot-0.2.0-alpha.14.tar
docker compose --env-file .env.server -f compose.server.yaml up -d --no-build --pull never
```

Перед запуском подготовьте `.env.server`, постоянные каталоги, права и согласованную копию данных по инструкции выше. Архив образа не заменяет перенос данных и ключей.

## Что перенесено из опыта Mail Cloud

GitHub как источник, будущий GHCR image, постоянные `/state` и `/app/private/system-config`, неизменяемые version tags, возможность отдельного `stable`, UID/GID 568, read-only filesystem контейнера, tmpfs `/tmp`, ротация stdout, явная процедура Stop/Start/rollback. Каталоги фотографий и порты другого проекта не переиспользуются.

## Локальная сборка

```text
docker compose build
docker compose up -d
docker compose logs --tail 100 gatebot
```

`compose.yaml` использует локальный образ `gatebot:local`, подключает `.env`,
`./data:/state` и `./private/system-config:/app/private/system-config`, публикует
только loopback `127.0.0.1:30550`. До запуска создайте оба каталога на хосте и
дайте UID/GID 568 право записи; автоматически созданный root-каталог не подойдёт
процессу приложения. Второй volume сохраняет `settings.enc` и `settings.key`
при пересоздании контейнера и не зависит от изменяемого DATA_DIR. Не помещайте
secrets в Dockerfile/build args. `.dockerignore` и явные COPY исключают runtime state.

Сохранённые [системные параметры](SYSTEM_SETTINGS.md) имеют приоритет над `.env`
и `environment` Compose. При переносе с Windows проверьте их до остановки:
`GATEBOT_DATA_DIR=/state`, `GATEBOT_LOG_DIR=/state/logs`, `HOST=0.0.0.0`, `PORT=5000`,
Linux-путь COM/сертификатов и внешний HTTPS `BASE_URL`. Изменение DATA_DIR не
переносит базы: при остановленном runtime перенесите согласованные данные и ключ
интеграций в соответствующий host volume. Изменение SECRET_KEY завершает прежние
веб-сессии после запуска. Нельзя удалить bootstrap-файлы только ради сброса
одного параметра: сбросьте выбранное значение через системные настройки.

Для production обязательно задать `GATEBOT_ENV=production` и удалить приватные
`DEV_REGISTRATION_SMS_*`: локальное перенаправление кодов регистрации работает
только в явно выбранном development. Проверьте также сохранённые системные
параметры и значения, ожидающие перезапуска: старый override не должен вернуть
development поверх production env. Не переносите тестовые регистрации в
рабочую базу. Подробности — [OPERATIONS.md](OPERATIONS.md).

## TrueNAS SCALE ElectricEel 24.10.2.3

Используется Custom App → Install via YAML. Основа — `examples/truenas.example.yaml`. Перед применением нужны:

- фактически опубликованный и проверенный version tag образа;
- отдельный dataset приложения, каталоги state/system-config и права записи для UID/GID 568;
- приватный env-файл с production SECRET_KEY, BASE_URL, глобальными токенами ботов/партнёра/приложения и нужными переключателями; обычные пользовательские токены YCLIENTS хранятся только в зашифрованном подключении филиала в БД;
- внутренний порт, не конфликтующий с другими приложениями;
- фиксированный `/dev/serial/by-id/...` и правильный host group ID для устройства;
- внешний HTTPS через Nginx Proxy Manager.

Строка `REPLACE_WITH_TESTED_VERSION` намеренно не является существующим тегом. Начальный коммит не публикует контейнер автоматически. YAML не содержит реальные пути dataset, имена устройств или ключи владельца.

Шаблон содержит два обязательных mount: данные в `/state` и bootstrap-настройки
в `/app/private/system-config`. Для второго задайте отдельный постоянный host path;
его нельзя оставлять во временном слое контейнера. Даже если системные настройки
ещё не сохранялись, каталог должен быть доступен для последующего сохранения из UI.

Ручное подключение YCLIENTS выполняется в «Учётной записи YCLIENTS» отдельно для
каждого филиала. Для Marketplace заполните глобальные параметры приложения,
адреса HTTPS и выполните отдельную приёмку установки по [гайду](YCLIENTS_MARKETPLACE.md).
Не переносите старые `YC_USER_TOKEN`/`YC_COMPANY_ID` как способ авторизации: они
больше не используются. Реальная установка по умолчанию выключена; данные токена
системного пользователя не создают подключение филиала сами по себе.

## Nginx Proxy Manager

Proxy Host направляет домен на внутренний адрес сервера и порт приложения. `BASE_URL` — точный внешний HTTPS URL, `SESSION_COOKIE_SECURE=true`. Не выставлять admin/http порт напрямую в Интернет. Перед включением внешнего доступа пройти checklist ролей, CSRF, backup/restore и целевого webhook режима.

В этой сборке нет потребности включать доверие произвольным `X-Forwarded-*`: ссылки строятся из `BASE_URL`, сессионные cookies задаются конфигурацией. При настройке real client IP для rate limiting потребуется явно ограничить доверенный reverse proxy, а не доверять всем заголовкам клиента.

## Polling → webhook

MAX официально предназначает long polling для разработки, production — webhook. Receiver-функция MAX присутствует как строительный блок, но runtime пока не связывает её с durable inbox/worker. Перед серверным production нужны authenticated ingress, дедупликация, очереди, подтверждение подписки, повторная доставка и проверка перезапуска. Существующие subscriptions в MAX/Telegram приложение не удаляет автоматически.

## Обновление

Сначала backup приватного состояния, затем неизменяемый image tag и проверка миграций на копии. Копия включает application БД и `integration.key`, журнал телефонии/состояние ботов, а также согласованную пару `private/system-config/settings.enc` и `settings.key`. Потеря bootstrap-ключа или повреждение конфигурации останавливает запуск, а не возвращает незаметно настройки `.env`. `stable` присваивается только принятой версии. `pull_policy: always` не гарантирует автоматическое переключение уже работающего контейнера; на TrueNAS может требоваться остановка/запуск или пересоздание. Для rollback сохранить предыдущий tag и согласованную копию данных и настроек. Изменения Compose/YAML здесь подготовлены, но контейнер и доменные webhooks на сервере ещё не приняты.
