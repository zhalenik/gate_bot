import pytest
import requests

from max_api import MaxApiClient, MaxApiError


class Response:
    def __init__(self, body=None, status=200, headers=None):
        self.body = {} if body is None else body
        self.status_code = status
        self.headers = headers or {}
        self.content = b"json"

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(transport, **kwargs):
    return MaxApiClient(
        "unit-test-max-token", session=transport, sleep=lambda _: None,
        monotonic=lambda: 1.0, **kwargs,
    )


def test_send_uses_native_max_contract_and_raw_authorization():
    transport = Transport(Response({"message": {"body": {"mid": "test-message"}}}))
    keyboard = [{"type": "inline_keyboard", "payload": {"buttons": [[{"type": "request_contact", "text": "Номер"}]]}}]
    api = client(transport, ca_bundle="custom-trust.pem")
    api.send_message(chat_id=-12, text="Проверка", attachments=keyboard)
    method, url, request = transport.calls[0]
    assert method == "POST" and url == "https://platform-api2.max.ru/messages"
    assert request["headers"]["Authorization"] == "unit-test-max-token"
    assert request["params"] == {"chat_id": -12}
    assert request["json"] == {"text": "Проверка", "attachments": keyboard}
    assert request["verify"] == "custom-trust.pem"
    assert request["allow_redirects"] is False
    assert "access_token" not in request["params"]


def test_callback_id_is_query_and_notification_is_body():
    transport = Transport(Response({"success": True}))
    client(transport).answer_callback("callback-id", notification="Принято", chat_id=-12)
    request = transport.calls[0][2]
    assert request["params"] == {"callback_id": "callback-id"}
    assert request["json"] == {"notification": "Принято"}


def test_polling_marker_is_opaque_and_read_timeout_exceeds_long_poll():
    transport = Transport(Response({"updates": [], "marker": 987654321098765432}))
    result = client(transport).get_updates(marker=987654321098765430, timeout=50)
    request = transport.calls[0][2]
    assert request["params"]["marker"] == 987654321098765430
    assert request["params"]["types"] == "bot_started,message_created,message_callback"
    assert request["timeout"][1] > 50
    assert result["marker"] == 987654321098765432


def test_initial_poll_omits_marker_instead_of_telegram_offset():
    transport = Transport(Response({"updates": [], "marker": None}))
    client(transport).get_updates()
    assert "marker" not in transport.calls[0][2]["params"]
    assert "offset" not in transport.calls[0][2]["params"]


def test_error_never_contains_body_token_or_original_network_exception():
    secret = "sensitive-body-value"
    transport = Transport(Response({"code": "access.denied", "message": secret}, 403))
    with pytest.raises(MaxApiError) as caught:
        client(transport).get_me()
    assert secret not in str(caught.value)
    assert "unit-test-max-token" not in str(caught.value)
    assert caught.value.status_code == 403
    transport = Transport(requests.ConnectionError(secret))
    with pytest.raises(MaxApiError) as caught:
        client(transport, retries=0).send_message(user_id=12, text="Test")
    assert secret not in str(caught.value)


def test_ambiguous_post_timeout_is_not_retried():
    transport = Transport(requests.Timeout("private request details"), Response())
    with pytest.raises(MaxApiError, match="network_error"):
        client(transport, retries=3).send_message(user_id=12, text="Test")
    assert len(transport.calls) == 1


def test_reads_retry_server_errors_and_rate_limit_rejections():
    transport = Transport(Response({}, 503), Response({}, 429, {"Retry-After": "0"}), Response({"user_id": 1}))
    assert client(transport, retries=2).get_me()["user_id"] == 1
    assert len(transport.calls) == 3


def test_tls_failure_is_actionable_and_never_disables_verification():
    transport = Transport(requests.exceptions.SSLError("certificate detail"))
    with pytest.raises(MaxApiError, match="tls_trust_failed"):
        client(transport).get_me()
    assert transport.calls[0][2]["verify"] is True
    with pytest.raises(ValueError, match="cannot be disabled"):
        client(Transport(), ca_bundle=False)


@pytest.mark.parametrize("url", ["http://platform-api2.max.ru", "https://example.com?token=x", "https://name:pass@example.com"])
def test_invalid_base_url_does_not_transmit_token(url):
    with pytest.raises(ValueError):
        client(Transport(), base_url=url)


def test_schema_failure_is_not_treated_as_empty_success():
    transport = Transport(Response({"updates": [], "marker": True}))
    with pytest.raises(MaxApiError, match="invalid_updates"):
        client(transport).get_updates()


def test_message_limits_and_ambiguous_recipient_fail_before_http():
    api = client(Transport())
    with pytest.raises(ValueError):
        api.send_message(chat_id=1, user_id=2, text="Test")
    with pytest.raises(ValueError):
        api.send_message(user_id=2, text="я" * 4001)
