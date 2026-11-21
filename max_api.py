"""Small MAX HTTP adapter, based on the official API (verified 2026-11-16).

Nothing connects at import time. The transport is injectable for offline tests.
See docs/MAX.md for the wire contract and the required certificate trust setup.
"""
from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests


DEFAULT_API_BASE_URL = "https://platform-api2.max.ru"
UPDATE_TYPES = ("bot_started", "message_created", "message_callback")
# Never copy arbitrary response text into diagnostics, even if it resembles an
# identifier. Unknown provider codes retain the HTTP status and become api_error.
REMOTE_ERROR_CODES = frozenset({
    "not.found", "chat.denied", "proto.payload", "attachment.not.ready",
    "access.denied",
})
LOCAL_ERROR_CODES = frozenset({
    "api_error", "tls_trust_failed", "ca_bundle_invalid", "connect_timeout",
    "read_timeout", "timeout", "proxy_error", "dns_error", "connection_error",
    "network_error", "token_invalid_format", "invalid_json", "invalid_response",
    "invalid_subscriptions", "invalid_updates", "retry_exhausted", "active_webhook",
})
REQUEST_STAGES = {
    "/me": "get_me", "/subscriptions": "subscriptions", "/updates": "polling",
    "/messages": "send_message", "/answers": "answer_callback",
}


def _contains_exception(error: BaseException, expected: type[BaseException]) -> bool:
    """Inspect wrapped urllib3/socket causes without rendering private messages."""
    pending = [error]
    seen: set[int] = set()
    while pending and len(seen) < 32:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, expected):
            return True
        nested = [current.__cause__, current.__context__, getattr(current, "reason", None), *current.args]
        pending.extend(item for item in nested if isinstance(item, BaseException))
    return False


def _request_error_code(error: requests.exceptions.RequestException) -> str:
    if isinstance(error, requests.exceptions.SSLError):
        return "tls_trust_failed"
    if isinstance(error, requests.exceptions.ConnectTimeout):
        return "connect_timeout"
    if isinstance(error, requests.exceptions.ReadTimeout):
        return "read_timeout"
    if isinstance(error, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(error, requests.exceptions.ProxyError):
        return "proxy_error"
    if isinstance(error, requests.exceptions.InvalidHeader):
        return "token_invalid_format"
    if _contains_exception(error, socket.gaierror):
        return "dns_error"
    if isinstance(error, requests.exceptions.ConnectionError):
        return "connection_error"
    return "network_error"


class MaxApiError(RuntimeError):
    """An error safe to log: no response body, request URL, token, or contact."""

    def __init__(
        self, code: str, *, status_code: int | None = None,
        retryable: bool = False, retry_after: float | None = None,
        stage: str | None = None,
    ) -> None:
        self.code = code if isinstance(code, str) and code in LOCAL_ERROR_CODES | REMOTE_ERROR_CODES else "api_error"
        self.status_code = status_code if type(status_code) is int and 100 <= status_code <= 599 else None
        self.stage = stage if stage in REQUEST_STAGES.values() else None
        self.retryable = retryable
        self.retry_after = retry_after
        status = f" (HTTP {self.status_code})" if self.status_code is not None else ""
        super().__init__(f"MAX API: {self.code}{status}")

    def diagnostic(self) -> dict[str, Any]:
        return {"code": self.code, "http_status": self.status_code,
                "stage": self.stage, "retryable": bool(self.retryable)}


class MaxApiClient:
    def __init__(
        self, token: str, *, base_url: str = DEFAULT_API_BASE_URL,
        ca_bundle: str | Path | None = None, timeout: float = 15.0,
        session: Any = None, retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not token or not isinstance(token, str):
            raise ValueError("MAX_BOT_TOKEN is required")
        url = urlsplit(base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.path not in ("", "/")):
            raise ValueError("MAX_API_BASE_URL must be an HTTPS origin")
        if ca_bundle is False:
            raise ValueError("TLS verification cannot be disabled")
        if not 0 < timeout <= 120:
            raise ValueError("MAX API timeout must be in (0, 120] seconds")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._verify = str(ca_bundle) if ca_bundle else True
        self.timeout = timeout
        # requests.request creates a separate Session per request. This also
        # permits the polling and outbox threads to use the adapter concurrently.
        self._transport = session if session is not None else requests
        self._retries = max(0, min(int(retries), 4))
        self._sleep = sleep
        self._monotonic = monotonic
        self._rate_lock = threading.Lock()
        self._next_request = 0.0
        self._next_answer: dict[str, float] = {}

    def _wait_for_slot(self, *, answer_key: str | None = None) -> None:
        with self._rate_lock:
            now = self._monotonic()
            target = max(now, self._next_request)
            if answer_key is not None:
                target = max(target, self._next_answer.get(answer_key, 0.0))
                self._next_answer[answer_key] = target + 0.5
                if len(self._next_answer) > 10000:
                    self._next_answer = {k: v for k, v in self._next_answer.items() if v > now}
            self._next_request = target + 1 / 30
        if target > now:
            self._sleep(target - now)

    @staticmethod
    def _retry_after(response: Any) -> float | None:
        try:
            value = float(response.headers.get("Retry-After", ""))
            return min(max(value, 0.0), 30.0)
        except (TypeError, ValueError):
            return None

    def _request(
        self, method: str, endpoint: str, *, params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None, read_timeout: float | None = None,
        answer_key: str | None = None, retry_limit: int | None = None,
    ) -> dict[str, Any]:
        retries = self._retries if retry_limit is None else retry_limit
        stage = REQUEST_STAGES.get(endpoint)
        for attempt in range(retries + 1):
            self._wait_for_slot(answer_key=answer_key)
            try:
                response = self._transport.request(
                    method, self.base_url + endpoint,
                    params=params, json=body,
                    headers={"Authorization": self._token, "Content-Type": "application/json"},
                    timeout=(5.0, read_timeout or self.timeout),
                    verify=self._verify, allow_redirects=False,
                )
            except requests.exceptions.RequestException as error:
                code = _request_error_code(error)
                retryable = method == "GET" and code not in {"tls_trust_failed", "token_invalid_format"}
                if retryable and attempt < retries:
                    self._sleep(min(2 ** attempt, 10))
                    continue
                # A timed-out POST may already have been delivered. Do not retry
                # it automatically; that could send the same message twice.
                raise MaxApiError(code, retryable=retryable, stage=stage) from None
            except OSError:
                # requests raises OSError before TLS if the configured bundle
                # path is missing/unreadable. Never expose its local path.
                code = "ca_bundle_invalid" if self._verify is not True else "network_error"
                raise MaxApiError(code, stage=stage) from None

            status = response.status_code
            if 200 <= status < 300:
                if not response.content:
                    return {}
                try:
                    result = response.json()
                except (ValueError, TypeError):
                    raise MaxApiError("invalid_json", status_code=status, stage=stage) from None
                if not isinstance(result, dict):
                    raise MaxApiError("invalid_response", status_code=status, stage=stage)
                return result

            try:
                error = response.json()
                code = error.get("code", "api_error") if isinstance(error, dict) else "api_error"
            except (ValueError, TypeError):
                code = "api_error"
            if not isinstance(code, str) or code not in REMOTE_ERROR_CODES:
                code = "api_error"
            retry_after = self._retry_after(response)
            # MAX rejected rate-limited requests; reads are safe to retry after
            # server failures. Mutations with an ambiguous outcome are not.
            can_retry = status == 429 or (method == "GET" and status >= 500)
            if can_retry and attempt < retries:
                self._sleep(retry_after if retry_after is not None else min(2 ** attempt, 10))
                continue
            raise MaxApiError(
                code, status_code=status, retryable=can_retry, retry_after=retry_after, stage=stage,
            )
        raise MaxApiError("retry_exhausted", retryable=True, stage=stage)

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/me")

    def get_subscriptions(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/subscriptions")
        subscriptions = result.get("subscriptions")
        if not isinstance(subscriptions, list):
            raise MaxApiError("invalid_subscriptions", stage="subscriptions")
        return subscriptions

    def subscribe(self, url: str, secret: str) -> None:
        result = self._request("POST","/subscriptions",body={"url":url,"secret":secret,"update_types":list(UPDATE_TYPES)})
        if result.get("success") is not True:
            raise MaxApiError("api_error",stage="subscriptions")

    def unsubscribe(self, url: str) -> None:
        result = self._request("DELETE","/subscriptions",params={"url":url})
        if result.get("success") is not True:
            raise MaxApiError("api_error",stage="subscriptions")

    def get_updates(
        self, *, marker: int | None = None, timeout: int = 20,
        limit: int = 100, types: Sequence[str] = UPDATE_TYPES,
    ) -> dict[str, Any]:
        if not 0 <= timeout <= 90 or not 1 <= limit <= 1000:
            raise ValueError("Invalid MAX polling limits")
        params: dict[str, Any] = {"timeout": timeout, "limit": limit, "types": ",".join(types)}
        if marker is not None:
            if not isinstance(marker, int) or isinstance(marker, bool):
                raise ValueError("MAX marker must be an integer")
            params["marker"] = marker
        # The polling loop owns its interruptible retry/backoff. Do not chain
        # several long requests here after the supervisor has requested shutdown.
        result = self._request(
            "GET", "/updates", params=params,
            read_timeout=max(self.timeout, timeout + 5), retry_limit=0,
        )
        next_marker = result.get("marker")
        if (not isinstance(result.get("updates"), list)
                or (next_marker is not None and (not isinstance(next_marker, int) or isinstance(next_marker, bool)))):
            raise MaxApiError("invalid_updates", stage="polling")
        return result

    def send_message(
        self, *, text: str, chat_id: int | None = None, user_id: int | None = None,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if (chat_id is None) == (user_id is None):
            raise ValueError("Provide exactly one MAX recipient")
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError("MAX text exceeds 4000 characters")
        body: dict[str, Any] = {"text": text}
        if attachments is not None:
            body["attachments"] = attachments
        params = {"chat_id": chat_id} if chat_id is not None else {"user_id": user_id}
        return self._request("POST", "/messages", params=params, body=body)

    def edit_message(self, message_id, *, text, attachments=None):
        if not isinstance(message_id, str) or not message_id or not isinstance(text, str) or len(text) > 4000:
            raise ValueError('Invalid MAX message edit')
        body = {'text': text, 'notify': False}
        if attachments is not None:
            body['attachments'] = attachments
        return self._request('PUT', '/messages', params={'message_id': message_id}, body=body,
                             answer_key='menu:' + message_id)

    def answer_callback(
        self, callback_id: str, *, notification: str | None = None,
        message: dict[str, Any] | None = None, chat_id: int | str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(callback_id, str) or not callback_id.strip():
            raise ValueError("MAX callback_id is required")
        body: dict[str, Any] = {}
        if notification is not None:
            body["notification"] = notification
        if message is not None:
            body["message"] = message
        return self._request(
            "POST", "/answers", params={"callback_id": callback_id}, body=body,
            answer_key=str(chat_id) if chat_id is not None else "unknown",
        )
