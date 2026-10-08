"""One envelope for every error (SPEC §3, §5.3, case 21) and step 1 of §4.7 (case 10)."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from mail_dispatch.app import create_app
from mail_dispatch.intake import accepts_content_type
from mail_dispatch.settings import Settings

JSON = {"content-type": "application/json"}


def assert_envelope(response: Response, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/json"
    body = response.json()
    assert body["ok"] is False
    uuid.UUID(body["dispatch_id"])
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    error: dict[str, object] = body["error"]
    return error


@pytest.mark.parametrize("path", ["/", "/v1", "/v1/send/", "/v1/health/", "/docs", "/openapi.json"])
def test_unknown_path_is_not_found(client: TestClient, path: str) -> None:
    assert_envelope(client.get(path), 404, "NOT_FOUND")
    assert_envelope(client.post(path, headers=JSON, content=b"{}"), 404, "NOT_FOUND")


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("GET", "/v1/send", "POST"),
        ("PUT", "/v1/send", "POST"),
        ("OPTIONS", "/v1/send", "POST"),
        ("POST", "/v1/health", "GET"),
        ("DELETE", "/v1/health", "GET"),
    ],
)
def test_method_not_allowed_names_the_allowed_one(
    client: TestClient, method: str, path: str, allow: str
) -> None:
    response = client.request(method, path)
    assert_envelope(response, 405, "METHOD_NOT_ALLOWED")
    assert response.headers["allow"] == allow


def test_head_is_not_allowed(client: TestClient) -> None:
    response = client.head("/v1/health")
    assert response.status_code == 405
    assert response.headers["allow"] == "GET"


def test_every_envelope_has_a_fresh_dispatch_id(client: TestClient) -> None:
    first = client.get("/nowhere").json()["dispatch_id"]
    second = client.get("/nowhere").json()["dispatch_id"]
    assert first != second


def test_query_string_is_ignored(client: TestClient) -> None:
    error = assert_envelope(client.post("/v1/send?x=1"), 400, "INVALID_REQUEST")
    assert "content type" in str(error["message"])


@pytest.fixture
def exploding_client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)

    @app.get("/v1/explode")
    async def explode() -> None:
        raise RuntimeError("quoted content that must not reach the log")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_an_escaped_exception_is_enveloped(exploding_client: TestClient) -> None:
    error = assert_envelope(exploding_client.get("/v1/explode"), 500, "INTERNAL_ERROR")
    assert "quoted content" not in str(error["message"])
    assert set(error) == {"code", "message"}


@pytest.mark.parametrize(
    "value",
    [
        "application/json",
        "Application/JSON",
        "application/json; charset=utf-8",
        "application/json;charset=UTF-8",
        'application/json; charset="utf-8"',
        "application/json \t;\t charset=Utf-8",
        "application/json;",
    ],
)
def test_accepted_content_types(value: str) -> None:
    assert accepts_content_type(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "text/plain",
        "application/jsonx",
        "application/problem+json",
        "application/json; charset=latin-1",
        "application/json; charset=utf8",
        "application/json; charset = utf-8",
        "application/json; charset",
        "application/json; boundary=x",
        "application/json; charset=utf-8; charset=utf-8",
        "application/json; charset=utf-8; x=y",
        'application/json; charset="utf-8',
    ],
)
def test_refused_content_types(value: str) -> None:
    assert not accepts_content_type(value)


def test_missing_content_type_is_invalid(client: TestClient) -> None:
    assert_envelope(client.post("/v1/send", content=b"{}"), 400, "INVALID_REQUEST")


def test_wrong_content_type_is_invalid(client: TestClient) -> None:
    response = client.post("/v1/send", headers={"content-type": "text/plain"}, content=b"{}")
    assert_envelope(response, 400, "INVALID_REQUEST")


@pytest.fixture
def small_client() -> Iterator[TestClient]:
    settings = Settings(
        smtp_host="mail.example.org", smtp_ehlo_name="client.example.org", max_request_bytes=100
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        yield test_client


def test_body_over_the_limit_with_content_length(small_client: TestClient) -> None:
    """Case 10: the limit with its threshold, measured value and source."""
    error = assert_envelope(
        small_client.post("/v1/send", headers=JSON, content=b" " * 101), 413, "REQUEST_TOO_LARGE"
    )
    assert error["limit_bytes"] == 100
    assert error["actual_bytes"] == 101
    assert error["limit_source"] == "max_request_bytes"


def test_body_over_the_limit_without_content_length(small_client: TestClient) -> None:
    """Case 10: without Content-Length, the bytes read until reading stopped.

    The test client hands the body over in one piece, so here reading stops after all of it;
    over a socket it stops at the first chunk past the limit.
    """

    def chunks() -> Iterator[bytes]:
        for _ in range(10):
            yield b" " * 30

    response = small_client.post("/v1/send", headers=JSON, content=chunks())
    assert "content-length" not in response.request.headers
    error = assert_envelope(response, 413, "REQUEST_TOO_LARGE")
    assert error["limit_bytes"] == 100
    assert error["actual_bytes"] == 300
    assert error["limit_source"] == "max_request_bytes"


def test_body_at_the_limit_is_read(small_client: TestClient) -> None:
    error = assert_envelope(
        small_client.post("/v1/send", headers=JSON, content=b"{" + b" " * 99),
        400,
        "INVALID_REQUEST",
    )
    assert "JSON" in str(error["message"])


def test_content_type_is_checked_before_the_size(small_client: TestClient) -> None:
    """Case 10: a wrong Content-Type with a body over the limit is INVALID_REQUEST."""
    response = small_client.post(
        "/v1/send", headers={"content-type": "text/plain"}, content=b" " * 1000
    )
    assert_envelope(response, 400, "INVALID_REQUEST")


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (b"\xef\xbb\xbf{}", "byte-order mark"),
        (b'{"subject": "\xff"}', "UTF-8"),
        (b"{", "JSON"),
        (b"", "JSON"),
        (b'{"a": NaN}', "JSON"),
        (b'{"a": Infinity}', "JSON"),
        (b"[" * 100_000 + b"]" * 100_000, "JSON"),
        (b'{"subject": "a", "subject": "b"}', "repeated"),
        (b'{"headers": {"X-A": "1", "X-A": "2"}}', "repeated"),
        (b'{"to": [{"address": "a@example.org", "address": "b@example.org"}]}', "repeated"),
    ],
)
def test_body_that_does_not_parse(client: TestClient, body: bytes, fragment: str) -> None:
    error = assert_envelope(
        client.post("/v1/send", headers=JSON, content=body), 400, "INVALID_REQUEST"
    )
    assert fragment in str(error["message"])
    assert "field" not in error
