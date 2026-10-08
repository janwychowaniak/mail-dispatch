"""The error envelope of SPEC §5.3 and the closed set of error codes of §5.5 [D31]."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Final, Literal

from fastapi.responses import JSONResponse

ErrorCode = Literal[
    "INVALID_REQUEST",
    "INVALID_ADDRESS",
    "INVALID_HEADER",
    "INVALID_CONTENT",
    "REQUEST_TOO_LARGE",
    "TOO_MANY_RECIPIENTS",
    "MESSAGE_TOO_LARGE",
    "NOT_FOUND",
    "METHOD_NOT_ALLOWED",
    "INTERNAL_ERROR",
    "UPSTREAM_UNREACHABLE",
    "UPSTREAM_TIMEOUT",
    "UPSTREAM_TLS",
    "UPSTREAM_AUTH",
    "UPSTREAM_REJECTED",
    "UPSTREAM_TRANSIENT",
    "UPSTREAM_ERROR",
]

HTTP_STATUS: Final[dict[ErrorCode, int]] = {
    "INVALID_REQUEST": 400,
    "INVALID_ADDRESS": 400,
    "INVALID_HEADER": 400,
    "INVALID_CONTENT": 400,
    "REQUEST_TOO_LARGE": 413,
    "TOO_MANY_RECIPIENTS": 400,
    "MESSAGE_TOO_LARGE": 413,
    "NOT_FOUND": 404,
    "METHOD_NOT_ALLOWED": 405,
    "INTERNAL_ERROR": 500,
    "UPSTREAM_UNREACHABLE": 502,
    "UPSTREAM_TIMEOUT": 504,
    "UPSTREAM_TLS": 502,
    "UPSTREAM_AUTH": 502,
    "UPSTREAM_REJECTED": 502,
    "UPSTREAM_TRANSIENT": 503,
    "UPSTREAM_ERROR": 502,
}


class ApiError(Exception):
    """An error that ends the handling of a request and leaves through the envelope.

    `fields` are the extra members of `error{}` — `field`, `index`, the limit triple, the
    conversation's `upstream` and the rest — in the order they are written.
    """

    def __init__(self, code: ErrorCode, message: str, **fields: object) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.fields = fields

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.code]


def new_dispatch_id() -> str:
    """A random identifier that cannot be derived from the content (§3)."""
    return str(uuid.uuid4())


def error_body(dispatch_id: str, error: ApiError) -> dict[str, object]:
    return {
        "ok": False,
        "dispatch_id": dispatch_id,
        "error": {"code": error.code, "message": error.message, **error.fields},
    }


def error_response(
    dispatch_id: str, error: ApiError, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(error_body(dispatch_id, error), status_code=error.status, headers=headers)
