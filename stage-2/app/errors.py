"""Error types and helpers.

Every failure the API returns carries the same shape so a client (or the
verifier) can branch on either a human message or a machine code::

    {
      "detail": "insufficient funds",
      "code": "insufficient_funds",
      "error": {"code": "insufficient_funds", "message": "insufficient funds", ...}
    }

Status codes used across the service (also documented in RUN.md):

* 400 - malformed request the schema could not even bind to
* 401 - missing / unknown bearer token, or wrong password
* 403 - authenticated but not permitted (operator-only endpoints, wrong actor)
* 404 - addressable resource does not exist (unknown handle, unknown request id)
* 409 - state conflict (illegal request transition, idempotency key reuse)
* 422 - semantically invalid input (validation, insufficient funds, bad amount)
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """An error that maps directly onto an HTTP response."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        **extra: Any,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "detail": self.message,
            "code": self.code,
            "error": {"code": self.code, "message": self.message, **self.extra},
        }
        return body


def validation_error(message: str, code: str = "validation_error", **extra: Any) -> AppError:
    return AppError(422, code, message, **extra)


def not_found(message: str, code: str = "not_found", **extra: Any) -> AppError:
    return AppError(404, code, message, **extra)


def forbidden(message: str, code: str = "forbidden", **extra: Any) -> AppError:
    return AppError(403, code, message, **extra)


def unauthorized(message: str, code: str = "unauthorized", **extra: Any) -> AppError:
    return AppError(401, code, message, **extra)


def conflict(message: str, code: str = "conflict", **extra: Any) -> AppError:
    return AppError(409, code, message, **extra)


def insufficient_funds(handle: str, available: int, required: int) -> AppError:
    return AppError(
        422,
        "insufficient_funds",
        f"insufficient funds: {handle} has {available} minor units, needs {required}",
        handle=handle,
        available=available,
        required=required,
    )
