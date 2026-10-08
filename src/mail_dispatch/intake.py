"""Step 1 of SPEC §4.7 and the parsing half of step 2: what arrives before any field is read.

The content type is checked before the body is read; the body is read against
`MAX_REQUEST_BYTES` whether or not it declares its length; then the bytes must be JSON in UTF-8
without a byte-order mark and without a key repeated within an object (§3).
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request

from .errors import ApiError

_MEDIA_TYPE = "application/json"
_OWS = " \t"


def check_content_type(request: Request) -> None:
    """`application/json`, optionally with `charset=utf-8` and nothing else (§3)."""
    values = request.headers.getlist("content-type")
    if len(values) != 1 or not accepts_content_type(values[0]):
        raise ApiError(
            "INVALID_REQUEST", "the content type must be application/json; charset=utf-8"
        )


def accepts_content_type(value: str) -> bool:
    media_type, *parameters = value.split(";")
    if media_type.strip(_OWS).lower() != _MEDIA_TYPE:
        return False
    seen_charset = False
    for parameter in parameters:
        parameter = parameter.strip(_OWS)
        if not parameter:
            continue  # RFC 9110 allows an empty parameter between semicolons
        name, equals, raw = parameter.partition("=")
        if not equals or name.lower() != "charset" or seen_charset:
            return False
        if len(raw) >= 2 and raw[0] == raw[-1] == '"':
            raw = raw[1:-1]
        if raw.lower() != "utf-8":
            return False
        seen_charset = True
    return True


async def read_body(request: Request, limit: int) -> bytes:
    """The whole body, or REQUEST_TOO_LARGE as soon as it is known to exceed `limit`."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise _too_large(limit, int(declared))
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > limit:
            # Without Content-Length, the number of bytes read until reading stopped (§4.7).
            raise _too_large(limit, received)
        chunks.append(chunk)
    return b"".join(chunks)


def _too_large(limit: int, actual: int) -> ApiError:
    return ApiError(
        "REQUEST_TOO_LARGE",
        "the request body exceeds the limit",
        limit_bytes=limit,
        actual_bytes=actual,
        limit_source="max_request_bytes",
    )


class _RepeatedKey(ValueError):
    pass


def _object_without_repeats(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _RepeatedKey(key)
        result[key] = value
    return result


def _reject_constant(name: str) -> Any:
    # json accepts NaN and Infinity, which are not JSON.
    raise ValueError(name)


def parse_json(body: bytes) -> Any:
    """The parsed document; a BOM, bytes that are not UTF-8, invalid JSON or a repeated key fail.

    Strings may still hold lone surrogates from `\\ud800` escapes: whether a string is valid
    Unicode is checked by the walk over the shape, in schema order (§4.7).
    """
    if body.startswith(b"\xef\xbb\xbf"):
        raise ApiError("INVALID_REQUEST", "the body starts with a byte-order mark")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise ApiError("INVALID_REQUEST", "the body is not valid UTF-8") from None
    try:
        return json.loads(
            text, object_pairs_hook=_object_without_repeats, parse_constant=_reject_constant
        )
    except _RepeatedKey:
        raise ApiError("INVALID_REQUEST", "a key is repeated within an object") from None
    except (ValueError, RecursionError):
        raise ApiError("INVALID_REQUEST", "the body is not valid JSON") from None
