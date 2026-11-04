# Контейнер, TrueNAS и Nginx Proxy Manager

Этот документ описывает подготовленный следующий контур. Первичная приёмка выполняется на Windows в polling. Dockerfile/Compose и YAML-шаблон не являются подтверждением запуска на конкретном TrueNAS или публикации образа.

## Что перенесено из опыта Mail Cloud

GitHub как источник, будущий GHCR image, постоянный `/state`, неизменяемые version tags, возможность отдельного `stable`, UID/GID 568, read-only filesystem контейнера, tmpfs `/tmp`, ротация stdout, явная процедура Stop/Start/rollback. Каталоги фотографий и порты другого проекта не переиспользуются.

## Локальная сборка

```text
docker compose build
docker compose up -d
docker compose logs --tail 100 gatebot
```

`compose.yaml` использует локальный образ `gatebot:local`, подключает `.env` и `./data`, публикует только loopback `127.0.0.1:30550`. Каталог данных должен быть доступен UID/GID 568. Не помещайте secrets в Dockerfile/build args. `.dockerignore` и явные COPY исключают runtime state.

## TrueNAS SCALE ElectricEel 24.10.2.3

Используется Custom App → Install via YAML. Основа — `examples/truenas.example.yaml`. Перед применением нужны:

- фактически опубликованный и проверенный version tag образа;
- отдельный dataset приложения и права записи для UID/GID 568;
- приватный env-файл с production SECRET_KEY, BASE_URL, token refs и нужными переключателями;
- внутренний порт, не конфликтующий с другими приложениями;
- фиксированный `/dev/serial/by-id/...` и правильный host group ID для устройства;
- внешний HTTPS через Nginx Proxy Manager.

Строка `REPLACE_WITH_TESTED_VERSION` намеренно не является существующим тегом. Начальный коммит не публикует контейнер автоматически. YAML не содержит реальные пути dataset, имена устройств или ключи владельца.

## Nginx Proxy Manager

Proxy Host направляет домен на внутренний адрес сервера и порт приложения. `BASE_URL` — точный внешний HTTPS URL, `SESSION_COOKIE_SECURE=true`. Не выставлять admin/http порт напрямую в Интернет. Перед включением внешнего доступа пройти checklist ролей, CSRF, backup/restore и целевого webhook режима.

В этой сборке нет потребности включать доверие произвольным `X-Forwarded-*`: ссылки строятся из `BASE_URL`, сессионные cookies задаются конфигурацией. При настройке real client IP для rate limiting потребуется явно ограничить доверенный reverse proxy, а не доверять всем заголовкам клиента.

## Polling → webhook

MAX официально предназначает long polling для разработки, production — webhook. Receiver-функция MAX присутствует как строительный блок, но runtime пока не связывает её с durable inbox/worker. Перед серверным production нужны authenticated ingress, дедупликация, очереди, подтверждение подписки, повторная доставка и проверка перезапуска. Существующие subscriptions в MAX/Telegram приложение не удаляет автоматически.

## Обновление

Сначала backup приватного состояния, затем неизменяемый image tag и проверка миграций на копии. `stable` присваивается только принятой версии. `pull_policy: always` не гарантирует автоматическое переключение уже работающего контейнера; на TrueNAS может требоваться остановка/запуск или пересоздание. Для rollback сохранить предыдущий tag и согласованную копию данных.
