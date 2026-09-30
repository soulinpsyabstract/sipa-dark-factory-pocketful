"""System + test-harness endpoints (spec section 3, "System & Testing")."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from ..dependencies import store
from ..schemas import FixtureIn
from ..store import now_iso

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness probe. Deliberately does not take the lock."""
    return {"status": "ok"}


@router.get("/")
def index() -> dict[str, Any]:
    return {
        "service": "pocketful-stage-1",
        "status": "ok",
        "endpoints": [
            "GET  /health",
            "POST /_test/reset",
            "GET  /_test/export",
            "POST /_test/import",
            "POST /auth/signup",
            "POST /auth/login",
            "GET  /me",
            "POST /payments",
            "POST /requests",
            "POST /requests/{id}/pay",
            "POST /requests/{id}/decline",
            "POST /requests/{id}/cancel",
            "GET  /requests",
            "POST /splits",
            "GET  /activity",
            "POST /settlements",
        ],
    }


@router.post("/_test/reset")
def reset(payload: FixtureIn | None = None) -> JSONResponse:
    """Wipe all state and seed it from ``payload``.

    Body: ``{"users": [{"handle","email","display_name","password","balance",
    "is_operator"}], "balances": {handle: int}, "seeded_total": int}``.

    ``seeded_total`` is optional - it defaults to the sum of the seeded
    balances. If it *is* supplied it must equal that sum, otherwise the
    request is rejected 422 rather than installing a state in which
    invariant I1 is already false.
    """
    fixture: dict[str, Any] = payload.model_dump(exclude_none=True) if payload else {}

    def work() -> dict[str, Any]:
        store._install_fixture(fixture)
        snapshot = store._export()
        return {
            "status": "ok",
            "action": "reset",
            "seeded_total": snapshot["seeded_total"],
            "balance_sum": snapshot["balance_sum"],
            "handles": sorted(store.users),
            "balances": snapshot["balances"],
            "users": snapshot["users"],
            "invariants": snapshot["invariants"],
            "reset_at": now_iso(),
        }

    return JSONResponse(status_code=200, content=store.transaction(work))


@router.get("/_test/export")
def export() -> JSONResponse:
    """Full state dump, including the live invariant report."""

    def work() -> dict[str, Any]:
        snapshot = store._export()
        return {
            "status": "ok",
            **snapshot,
            "exported_at": now_iso(),
        }

    return JSONResponse(status_code=200, content=store.transaction(work))


@router.post("/_test/import")
def import_state(payload: Any = Body(default=None)) -> JSONResponse:
    """Replace state with a payload produced by ``/_test/export``.

    Also accepts a bare fixture (``{"users": [...], "seeded_total": n}``), in
    which case requests/activity/idempotency are cleared exactly as on reset.
    """
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "import payload must be a JSON object",
                "code": "validation_error",
                "error": {
                    "code": "validation_error",
                    "message": "import payload must be a JSON object",
                },
            },
        )

    def work() -> dict[str, Any]:
        store._import(payload)
        snapshot = store._export()
        return {
            "status": "ok",
            "action": "import",
            "seeded_total": snapshot["seeded_total"],
            "balance_sum": snapshot["balance_sum"],
            "handles": sorted(store.users),
            "balances": snapshot["balances"],
            "invariants": snapshot["invariants"],
            "imported_at": now_iso(),
        }

    return JSONResponse(status_code=200, content=store.transaction(work))
