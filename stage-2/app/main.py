"""Pocketful API - Stage 1.

FastAPI application: 16 endpoints per POCKETFUL_SPEC.md section 3, in-memory
state behind a single ``threading.Lock()``, no external database.
"""

from __future__ import annotations

import os
import sys

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .errors import AppError
from .routers import auth, authorizations, operations, system
from .store import InvariantViolation

app = FastAPI(
    title="Pocketful API - Stage 2",
    version=__version__,
    description=(
        "In-memory wallet & payments service with payment authorizations. Amounts "
        "are integer minor units. Sum of all balances always equals the seeded "
        "total; negative balances are impossible; available = total - held."
    ),
)


# ----------------------------------------------------------------------
# error handling - one shape for every failure
# ----------------------------------------------------------------------


@app.exception_handler(AppError)
async def app_error(request: Request, exc: AppError) -> JSONResponse:
    """Registered on its own handler, NOT via ``Exception``.

    Starlette routes a dedicated handler through ``ExceptionMiddleware``,
    which returns the response normally. A catch-all ``Exception`` handler
    goes through ``ServerErrorMiddleware``, which re-raises after building
    the response - so an ``AppError`` registered only as ``Exception`` would
    escape as an unhandled exception instead of becoming its 4xx.
    """
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


@app.exception_handler(InvariantViolation)
async def invariant_broken(request: Request, exc: InvariantViolation) -> JSONResponse:
    """I1-I4 must never be silently broken; surface it loudly as a 500."""
    return JSONResponse(
        status_code=500,
        content={
            "detail": f"invariant violated: {exc}",
            "code": "invariant_violation",
            "error": {"code": "invariant_violation", "message": str(exc)},
        },
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    raise exc


@app.exception_handler(RequestValidationError)
async def validation_failed(request: Request, exc: RequestValidationError) -> JSONResponse:
    problems = [
        {
            "field": ".".join(str(p) for p in err.get("loc", ())[1:]) or "body",
            "message": err.get("msg", "invalid"),
            "type": err.get("type", "value_error"),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "detail": "; ".join(f"{p['field']}: {p['message']}" for p in problems) or "invalid request",
            "code": "validation_error",
            "error": {"code": "validation_error", "message": "request body failed validation", "problems": problems},
            "problems": problems,
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_failed(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": exc.detail if isinstance(exc.detail, str) else "request failed",
            "code": f"http_{exc.status_code}",
            "error": {
                "code": f"http_{exc.status_code}",
                "message": exc.detail if isinstance(exc.detail, str) else "request failed",
            },
        },
    )


app.include_router(system.router)
app.include_router(auth.router)
app.include_router(operations.router)
app.include_router(authorizations.router)


def main() -> None:
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    host = os.environ.get("HOST", "0.0.0.0")
    print(f"pocketful stage-2 listening on {host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info"))


if __name__ == "__main__":
    main()
